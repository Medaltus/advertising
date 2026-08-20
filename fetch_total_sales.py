#!/usr/bin/env python3
"""
Fetches total sales from the SP-API Sales & Traffic Report.
Builds an ASIN→brand map from the Advertising API spAdvertisedProduct report,
then splits salesAndTrafficByAsin by brand for per-brand total sales.

Usage (standalone):
  python3 fetch_total_sales.py
  python3 fetch_total_sales.py --config /path/to/config.json
"""

import json, hashlib, hmac, time, gzip, argparse, sys, re
import datetime as dt
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing dependency: run  pip3 install requests  and try again.")
    sys.exit(1)

SPAPI_HOST = "sellingpartnerapi-na.amazon.com"
REPORT_POLL_INTERVAL = 15
REPORT_POLL_TIMEOUT  = 900   # 15 min max

# SKU prefix → brand mapping (3-letter abbreviations in seller-sku column)
ABBREV_TO_BRAND = {
    "ALA": "Amala",          "CLC": "Cloud Cafe",      "COL": "Collagelee",
    "CRE": "The Creme Shop", "DEC": "dearcloud",        "ERA": "eraclea",
    "EVO": "evolis",         "HIL": "Hillside",          "HOL": "HighOnLove",
    "JBJ": "Just Bjorn",     "MIG": "MiGuard",           "PBJ": "PB & Jay",
    "SVA": "Skinuva",
}

# ── SigV4 signing ──────────────────────────────────────────────────────────────

def _sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

def sigv4_headers(method, path, qs, lwa_token, body, cfg):
    t  = dt.datetime.now(dt.timezone.utc)
    ad = t.strftime("%Y%m%dT%H%M%SZ")
    ds = t.strftime("%Y%m%d")

    canonical_headers = (
        f"host:{SPAPI_HOST}\n"
        f"x-amz-access-token:{lwa_token}\n"
        f"x-amz-date:{ad}\n"
    )
    signed_headers = "host;x-amz-access-token;x-amz-date"
    payload_hash   = hashlib.sha256(body.encode("utf-8")).hexdigest()

    canonical_request = "\n".join([
        method, path, qs,
        canonical_headers, signed_headers, payload_hash,
    ])
    credential_scope = f"{ds}/us-east-1/execute-api/aws4_request"
    string_to_sign   = "\n".join([
        "AWS4-HMAC-SHA256", ad, credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    signing_key = _sign(
        _sign(_sign(_sign(f"AWS4{cfg['aws_secret_access_key']}".encode("utf-8"), ds),
                    "us-east-1"), "execute-api"), "aws4_request",
    )
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 Credential={cfg['aws_access_key_id']}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "host":                 SPAPI_HOST,
        "x-amz-access-token":  lwa_token,
        "x-amz-date":          ad,
        "Authorization":       authorization,
        "Content-Type":        "application/json",
    }


# ── Auth ───────────────────────────────────────────────────────────────────────

def get_sp_access_token(cfg, refresh_token_key="sp_refresh_token"):
    # Use account-specific client credentials if available (e.g. hol_sp_client_id),
    # otherwise fall back to the primary sp_client_id/secret
    prefix = refresh_token_key.replace("sp_refresh_token", "").rstrip("_")  # e.g. "hol"
    client_id     = cfg.get(f"{prefix}_sp_client_id")     or cfg["sp_client_id"]     if prefix else cfg["sp_client_id"]
    client_secret = cfg.get(f"{prefix}_sp_client_secret") or cfg["sp_client_secret"] if prefix else cfg["sp_client_secret"]
    resp = requests.post(
        "https://api.amazon.com/auth/o2/token",
        data={
            "grant_type":    "refresh_token",
            "refresh_token": cfg[refresh_token_key],
            "client_id":     client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ── Rate-limit backoff ─────────────────────────────────────────────────────────
# GET_SALES_AND_TRAFFIC_REPORT has a tight SP-API quota. Calling it in a tight
# loop (e.g. once per day for a 30-day backfill) burns through that quota after
# just 2-3 requests, after which every subsequent call fails immediately with
# HTTP 429 "QuotaExceeded" — and previously that failure was NOT retried, so it
# just gave up and reported no data for that period. These wrappers retry on
# 429 with backoff (honoring Retry-After when Amazon sends it) instead of
# failing fast, so a burst of requests actually succeeds (slower) rather than
# silently coming back empty.
#
# IMPORTANT — global time budget: retrying is per-call, but a single run can
# make dozens of calls (2 monthly enrichment calls + up to 30 daily backfill
# calls, each potentially submit+poll+download). Without a shared budget, a
# still-rate-limited account makes EVERY one of those calls retry its full
# ~11 minutes before giving up — turning a step that used to fail in under a
# minute into one that can run for hours. _BUDGET_DEADLINE is a wall-clock
# cutoff shared across every call in the process; once it passes, calls stop
# retrying and just return whatever they last got, so the whole SP-API phase
# is bounded no matter how many months/days it's trying to fetch.
MAX_429_RETRIES = 6
_BUDGET_DEADLINE = None  # epoch seconds; None = no cap

# ── ASIN→brand map integrity ───────────────────────────────────────────────────
# A brand map that silently loses ASINs is the single most dangerous failure in this
# pipeline: unmapped sales are discarded, so revenue falls and TACOS rises, and the
# result still looks like a normal number. June 2026 read $242.41 instead of
# $159,976.50 this way. The map must retain at least this fraction of the previously
# cached ASINs to be trusted; a real catalogue does not shrink by 40% overnight.
ASIN_MAP_MIN_RETENTION = 0.6
# Populated when a map is rejected, so the workflow's verify step can fail the run
# instead of committing a quietly-degraded refresh.
ASIN_MAP_FAILURES = []

# Written next to the data files so verify_data_freshness.py can raise these AFTER the
# commit step. The scripts that call into here run with continue-on-error or cannot
# usefully abort mid-way, so recording and raising later is the only way a silent
# degradation becomes a red run rather than a green one serving bad numbers.
_INTEGRITY_MARKER = Path(__file__).parent / "data" / "data_integrity_failures.json"


def record_integrity_failure(message):
    """Append a data-integrity problem to the marker file. Never raises."""
    ASIN_MAP_FAILURES.append(message)
    try:
        existing = []
        if _INTEGRITY_MARKER.exists():
            try:
                blob = json.loads(_INTEGRITY_MARKER.read_text())
                existing = blob.get("failures") or []
            except Exception:
                existing = []
        if message not in existing:
            existing.append(message)
        _INTEGRITY_MARKER.parent.mkdir(parents=True, exist_ok=True)
        _INTEGRITY_MARKER.write_text(json.dumps({
            "written_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "failures": existing,
        }, indent=2))
    except Exception:
        pass


def set_sp_api_time_budget(seconds):
    """Call once at the start of a run to cap total time spent retrying
    across every SP-API call made afterward (submit/poll/download, for every
    month and every day). Pass None to remove the cap."""
    global _BUDGET_DEADLINE
    _BUDGET_DEADLINE = (time.time() + seconds) if seconds is not None else None


def budget_remaining():
    """Seconds left in the shared retry budget, or None if uncapped."""
    if _BUDGET_DEADLINE is None:
        return None
    return _BUDGET_DEADLINE - time.time()


def _retry_after_seconds(resp, attempt, base=20, cap=180):
    ra = resp.headers.get("Retry-After") if resp is not None else None
    if ra:
        try:
            return min(float(ra), cap)
        except ValueError:
            pass
    return min(base * (2 ** attempt), cap)


def _request_with_backoff(method, url, max_retries=MAX_429_RETRIES, **kwargs):
    resp = None
    for attempt in range(max_retries + 1):
        resp = requests.request(method, url, **kwargs)
        if resp.status_code == 429 and attempt < max_retries:
            remaining = budget_remaining()
            if remaining is not None and remaining <= 0:
                print(f"    ⏹  SP-API time budget used up — not retrying further "
                      f"({url.split('?')[0]})")
                return resp
            wait = _retry_after_seconds(resp, attempt)
            if remaining is not None:
                wait = max(0, min(wait, remaining))
            print(f"    ⏳ 429 rate limited ({url.split('?')[0]}) — "
                  f"waiting {wait:.0f}s before retry ({attempt + 1}/{max_retries})…")
            time.sleep(wait)
            continue
        return resp
    return resp


# ── Report flow ────────────────────────────────────────────────────────────────

def submit_sales_report(cfg, token, start_date, end_date, marketplace_id):
    """Submit Sales & Traffic report for date range. Returns reportId."""
    body = json.dumps({
        "reportType":    "GET_SALES_AND_TRAFFIC_REPORT",
        "marketplaceIds": [marketplace_id],
        "dataStartTime": start_date,
        "dataEndTime":   end_date,
        "reportOptions": {
            "dateGranularity": "DAY",
            "asinGranularity": "PARENT",
        },
    })
    path = "/reports/2021-06-30/reports"
    resp = _request_with_backoff(
        "POST",
        f"https://{SPAPI_HOST}{path}",
        headers=sigv4_headers("POST", path, "", token, body, cfg),
        data=body,
        timeout=30,
    )
    if resp.status_code == 425:
        import re
        m = re.search(r'duplicate of\s*[:\s]+([a-f0-9\-]+)', resp.text, re.IGNORECASE)
        if m:
            print(f"  ↩  Duplicate Sales report — reusing existing (reportId={m.group(1)})")
            return m.group(1)
    if not resp.ok:
        print(f"  Report submission error {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
    report_id = resp.json()["reportId"]
    print(f"  ✓ Submitted Sales & Traffic report (reportId={report_id})")
    return report_id


def poll_report(cfg, token, report_id):
    """Poll until DONE; return reportDocumentId."""
    deadline   = time.time() + REPORT_POLL_TIMEOUT
    start_time = time.time()
    last_msg   = time.time()
    print(f"  ⏳ Waiting for Sales & Traffic report…")

    while time.time() < deadline:
        path = f"/reports/2021-06-30/reports/{report_id}"
        resp = _request_with_backoff(
            "GET",
            f"https://{SPAPI_HOST}{path}",
            headers=sigv4_headers("GET", path, "", token, "", cfg),
            timeout=20,
        )
        resp.raise_for_status()
        data   = resp.json()
        status = data.get("processingStatus", "")

        if status == "DONE":
            elapsed = int(time.time() - start_time)
            print(f"  ✓ Report ready ({elapsed}s)")
            return data["reportDocumentId"]

        if status in ("FATAL", "CANCELLED"):
            raise RuntimeError(f"Report ended with status: {status}")

        if time.time() - last_msg >= 30:
            elapsed = int(time.time() - start_time)
            print(f"    Still waiting… ({elapsed}s, status={status})")
            last_msg = time.time()

        time.sleep(REPORT_POLL_INTERVAL)

    raise TimeoutError("Sales report not ready in time")


def download_report(cfg, token, doc_id):
    """Download and decompress the report JSON."""
    path = f"/reports/2021-06-30/documents/{doc_id}"
    resp = _request_with_backoff(
        "GET",
        f"https://{SPAPI_HOST}{path}",
        headers=sigv4_headers("GET", path, "", token, "", cfg),
        timeout=20,
    )
    resp.raise_for_status()
    doc_info = resp.json()

    # The presigned content URL is a storage host, not an SP-API endpoint —
    # it isn't subject to the same quota, so no backoff needed here.
    content = requests.get(doc_info["url"], timeout=120).content
    if doc_info.get("compressionAlgorithm") == "GZIP":
        content = gzip.decompress(content)

    return json.loads(content.decode("utf-8"))


# ── Parse portfolio sales ─────────────────────────────────────────────────────

def parse_daily_sales(report_data):
    """Returns {date_str: total_sales_usd} from salesAndTrafficByDate."""
    result = {}
    for day in report_data.get("salesAndTrafficByDate", []):
        date   = day.get("date", "")
        sales  = day.get("salesByDate", {}).get("orderedProductSales", {})
        amount = sales.get("amount", 0) or 0
        result[date] = round(amount, 2)
    return result


# ── ASIN→brand mapping via Advertising API ────────────────────────────────────

def _identify_brand(campaign_name, brands):
    """Match a campaign name to a brand by longest-prefix (case-insensitive)."""
    n = campaign_name.lower()
    for b in sorted(brands, key=len, reverse=True):
        if n.startswith(b.lower()):
            return b
    return None


def _ads_api_headers(access_token, client_id, profile_id):
    return {
        "Authorization":                   f"Bearer {access_token}",
        "Amazon-Advertising-API-ClientId": client_id,
        "Amazon-Advertising-API-Scope":    str(profile_id),
        "Content-Type":                    "application/json",
        "Accept":                          "application/json",
    }


def build_asin_brand_map(cfg, start_str, end_str, refresh_token_key="sp_refresh_token",
                        cache_name=None):
    """
    Builds a comprehensive ASIN→brand map using two sources:
    1. GET_MERCHANT_LISTINGS_ALL_DATA (all active listings, brand from title)
    2. Advertising API spAdvertisedProduct (fallback for any gaps)
    Returns {asin: brand_name}.
    """
    # Map all brands including excluded ones so we can identify and subtract their sales
    brands      = cfg.get("brands", []) + cfg.get("excluded_brands", [])
    marketplace = cfg.get("marketplace_id", "ATVPDKIKX0DER")
    sp_token    = get_sp_access_token(cfg, refresh_token_key)
    asin_brand  = {}
    listings_ok = False   # did Source 1 (the comprehensive one) actually deliver?

    # ── Source 1: Merchant listings report (comprehensive) ────────────────────
    try:
        body = json.dumps({
            "reportType":    "GET_MERCHANT_LISTINGS_ALL_DATA",
            "marketplaceIds": [marketplace],
        })
        path = "/reports/2021-06-30/reports"
        resp = requests.post(
            f"https://{SPAPI_HOST}{path}",
            headers=sigv4_headers("POST", path, "", sp_token, body, cfg),
            data=body, timeout=30,
        )
        if resp.status_code == 425:
            m = re.search(r'duplicate of\s*[:\s]+([a-f0-9\-]+)', resp.text, re.IGNORECASE)
            report_id = m.group(1) if m else None
        elif resp.ok:
            report_id = resp.json()["reportId"]
        else:
            raise RuntimeError(f"Listings report submission failed: {resp.text[:200]}")

        # Poll
        deadline = time.time() + 300
        while time.time() < deadline:
            rs = requests.get(
                f"https://{SPAPI_HOST}/reports/2021-06-30/reports/{report_id}",
                headers=sigv4_headers("GET", f"/reports/2021-06-30/reports/{report_id}", "", sp_token, "", cfg),
                timeout=20,
            ).json()
            if rs.get("processingStatus") == "DONE":
                break
            if rs.get("processingStatus") in ("FATAL", "CANCELLED"):
                raise RuntimeError(f"Listings report failed: {rs}")
            time.sleep(10)

        doc_path = f"/reports/2021-06-30/documents/{rs['reportDocumentId']}"
        doc = requests.get(
            f"https://{SPAPI_HOST}{doc_path}",
            headers=sigv4_headers("GET", doc_path, "", sp_token, "", cfg),
            timeout=20,
        ).json()
        content = requests.get(doc["url"], timeout=120).content
        if doc.get("compressionAlgorithm") == "GZIP":
            content = gzip.decompress(content)
        text = content.decode("utf-8", errors="replace").lstrip("﻿")
        lines = [l for l in text.split("\n") if l.strip()]

        if lines:
            headers_row = lines[0].split("\t")
            # Find column indices
            try:
                asin_col  = headers_row.index("asin1")
                title_col = headers_row.index("item-name")
            except ValueError:
                asin_col  = next((i for i, h in enumerate(headers_row) if "asin" in h.lower()), None)
                title_col = next((i for i, h in enumerate(headers_row) if "item-name" in h.lower() or "title" in h.lower()), None)

            # SKU column for abbreviation-based brand lookup
            sku_col = next((i for i, h in enumerate(headers_row) if h.lower() in ("seller-sku", "sku")), None)

            asin_title = {}
            if asin_col is not None and title_col is not None:
                for line in lines[1:]:
                    cols = line.split("\t")
                    if len(cols) <= max(asin_col, title_col):
                        continue
                    asin  = cols[asin_col].strip()
                    title = cols[title_col].strip()
                    if not asin:
                        continue

                    if title:
                        asin_title[asin] = title

                    # 1. Try SKU prefix first (most reliable)
                    brand = None
                    if sku_col is not None and sku_col < len(cols):
                        sku_prefix = cols[sku_col].strip().upper()[:3]
                        brand = ABBREV_TO_BRAND.get(sku_prefix)

                    # 2. Fall back to title prefix matching
                    if not brand and title:
                        brand = _identify_brand(title, brands)

                    if brand:
                        asin_brand[asin] = brand

            # Write ASIN→title map for use by fetch_ads_data.py.
            #
            # MERGE, never replace. This was a full overwrite keyed by nothing, which is
            # the same defect as the asin_brand_cache incident one level down: the cache
            # filename got parameterised per marketplace, this sibling write did not. CA
            # uses entirely different ASINs (B0H3CLS4XW vs B07RCJDFN4), so a CA build
            # replaced the file with a map that had zero overlap with the US ASINs
            # fetch_ads_data.py looks up, blanking every US product title.
            if asin_title:
                try:
                    titles_path = Path(__file__).parent / "asin_titles.json"
                    merged = {}
                    if titles_path.exists():
                        try:
                            prev = json.loads(titles_path.read_text())
                            if isinstance(prev, dict):
                                merged.update(prev)
                        except Exception:
                            pass
                    merged.update(asin_title)
                    titles_path.write_text(json.dumps(merged))
                except Exception:
                    pass

        listings_ok = bool(asin_brand)
        print(f"  ✓ Listings report: {len(asin_brand)} ASINs mapped from {len(lines)-1} listings")

    except Exception as e:
        print(f"  ⚠ Listings report failed, falling back to ads data: {e}")

    # ── Source 2: Advertising API spAdvertisedProduct (fills gaps) ────────────
    try:
        ADS_BASE    = "https://advertising-api.amazon.com"
        profile_ids = cfg.get("profile_ids", [])

        resp = requests.post(
            "https://api.amazon.com/auth/o2/token",
            data={"grant_type":"refresh_token","refresh_token":cfg["refresh_token"],
                  "client_id":cfg["client_id"],"client_secret":cfg["client_secret"]},
            headers={"Content-Type":"application/x-www-form-urlencoded"}, timeout=20,
        )
        resp.raise_for_status()
        ads_token = resp.json()["access_token"]

        for profile_id in profile_ids:
            hdrs = _ads_api_headers(ads_token, cfg["client_id"], profile_id)
            payload = {
                "name": "ASIN Brand Map Supplement",
                "startDate": start_str, "endDate": end_str,
                "configuration": {
                    "adProduct": "SPONSORED_PRODUCTS",
                    "groupBy": ["advertiser"],
                    "columns": ["advertisedAsin", "campaignName", "spend"],
                    "reportTypeId": "spAdvertisedProduct",
                    "timeUnit": "SUMMARY",
                    "format": "GZIP_JSON",
                },
            }
            r = requests.post(
                f"{ADS_BASE}/reporting/reports",
                headers={**hdrs, "Content-Type": "application/vnd.createasyncreportrequest.v3+json"},
                json=payload, timeout=30,
            )
            if r.status_code == 425:
                m = re.search(r'duplicate of\s*[:\s]+([a-f0-9\-]+)', r.text, re.IGNORECASE)
                rid = m.group(1) if m else None
            elif r.ok:
                rid = r.json()["reportId"]
            else:
                continue

            deadline2 = time.time() + 300
            while time.time() < deadline2:
                rs2 = requests.get(f"{ADS_BASE}/reporting/reports/{rid}", headers=hdrs, timeout=20).json()
                if rs2.get("status") == "COMPLETED": break
                if rs2.get("status") in ("FAILED","CANCELLED"): rid = None; break
                time.sleep(10)

            if not rid: continue
            # Poll one more time to ensure we have the final response with URL
            rs2 = requests.get(f"{ADS_BASE}/reporting/reports/{rid}", headers=hdrs, timeout=20).json()
            url = rs2.get("url")
            if not url:
                print(f"  ⚠ No URL in ads supplement report response: {rs2}")
                continue
            rows = json.loads(gzip.decompress(requests.get(url, timeout=60).content))
            added = 0
            for row in rows:
                asin = row.get("advertisedAsin","").strip()
                if asin and asin not in asin_brand and (row.get("spend") or 0) > 0:
                    brand = _identify_brand(row.get("campaignName",""), brands)
                    if brand:
                        asin_brand[asin] = brand
                        added += 1
            if added:
                print(f"  ✓ Ads supplement: +{added} ASINs from profile {profile_id}")

    except Exception as e:
        print(f"  ⚠ Ads supplement failed: {e}")

    print(f"  ✓ Final ASIN→brand map: {len(asin_brand)} ASINs → {len(set(asin_brand.values()))} brands")
    # Cache the map — one file per (account token, MARKETPLACE).
    #
    # This used to derive the name from refresh_token_key alone, ignoring the
    # cache_name the caller passed in. Canada uses the same refresh token as the US,
    # so the Canadian map was written straight over asin_brand_cache.json. Every
    # subsequent US month then loaded a CA-only map, failed to attribute a single US
    # ASIN, discarded it all as "unmapped", and total sales collapsed — June 2026 went
    # from $159,976.50 to $242.41 on both dashboards.
    if not cache_name:
        cache_name = ("asin_brand_cache.json" if refresh_token_key == "sp_refresh_token"
                      else f"asin_brand_cache_{refresh_token_key.replace('sp_refresh_token','')}.json")
    cache_path = Path(__file__).parent / cache_name

    # ── Plausibility gate on the cache write ──────────────────────────────────
    #
    # The Canada incident was not really about a filename. The deeper problem is that a
    # DEGRADED map is indistinguishable from a good one downstream: parse_brand_sales
    # books whatever it can't attribute as "unmapped", prints an info line, and returns
    # a smaller-but-plausible number. generate_skinuva_supplement then stamps it
    # "sp-api-brand-filtered", and once the month passes RESTATEMENT_GRACE_DAYS the
    # freeze logic treats it as settled and never re-fetches. A single timed-out
    # listings report can therefore lock a wrong figure in permanently.
    #
    # Two guards, both about refusing to overwrite good state with worse state:
    #   1. If Source 1 failed, this map only covers ASINs with ad spend. Don't cache it —
    #      a cache write would make every later month in the run reuse it.
    #   2. Even if Source 1 "succeeded", refuse a map that collapsed relative to the
    #      previous cache. A real catalogue doesn't lose 40% of its ASINs overnight.
    prev_map = {}
    if cache_path.exists():
        try:
            prev_blob = json.loads(cache_path.read_text())
            if isinstance(prev_blob, dict) and isinstance(prev_blob.get("map"), dict):
                prev_map = prev_blob["map"]
        except Exception:
            prev_map = {}

    degraded_reason = None
    if not listings_ok:
        degraded_reason = ("listings report failed — map covers advertised ASINs only "
                           f"({len(asin_brand)} ASINs)")
    elif prev_map and len(asin_brand) < len(prev_map) * ASIN_MAP_MIN_RETENTION:
        degraded_reason = (f"map collapsed from {len(prev_map)} to {len(asin_brand)} ASINs "
                           f"(< {int(ASIN_MAP_MIN_RETENTION*100)}% retained)")

    if degraded_reason:
        print(f"  ✗ ASIN→brand map looks degraded: {degraded_reason}")
        record_integrity_failure(f"ASIN→brand map rejected for {cache_name}: {degraded_reason}")
        if prev_map:
            # Prefer yesterday's complete map over today's broken one. Stale-but-correct
            # beats fresh-but-wrong; the whole reason today's outage went unnoticed is
            # that the freshness check only ever asked "was this rewritten?".
            print(f"  ↩ reusing previous cached map ({len(prev_map)} ASINs) instead")
            return prev_map
        print("  ⚠ no previous cache to fall back on — proceeding, NOT caching")
        return asin_brand

    try:
        cache_path.write_text(json.dumps({
            "date":    dt.date.today().isoformat(),
            "version": "sku-prefix-v1",
            "map":     asin_brand,
        }))
    except Exception:
        pass
    return asin_brand


def parse_brand_sales(report_data, asin_brand_map, excluded_brands=None):
    """
    Returns {brand_name: total_sales_usd} using salesAndTrafficByAsin.
    excluded_brands: set of brand names to exclude from all totals.
    Also returns excluded_sales total (to subtract from portfolio daily totals).
    """
    excluded_brands = set(excluded_brands or [])
    brand_sales    = {}
    excluded_sales = 0.0
    unmapped_sales = 0.0

    for entry in report_data.get("salesAndTrafficByAsin", []):
        asin  = entry.get("parentAsin", "")
        sales = entry.get("salesByAsin", {}).get("orderedProductSales", {})
        amt   = sales.get("amount", 0) or 0
        if amt <= 0:
            continue
        brand = asin_brand_map.get(asin)
        if brand in excluded_brands:
            excluded_sales += amt   # count separately so we can subtract from portfolio
        elif brand:
            brand_sales[brand] = round(brand_sales.get(brand, 0) + amt, 2)
        else:
            unmapped_sales += amt

    if excluded_sales > 0:
        print(f"  ✓ Excluded {len(excluded_brands)} brands: ${excluded_sales:,.2f} removed from totals")
    if unmapped_sales > 0:
        print(f"  ℹ  ${unmapped_sales:,.2f} in sales not mapped (non-advertised ASINs)")
    return brand_sales, round(excluded_sales, 2)


# ── Single-account fetcher ─────────────────────────────────────────────────────

def _fetch_one_account(cfg, refresh_token_key, start_str, end_str,
                       default_brand=None, cache_name="asin_brand_cache.json"):
    """
    Pull Sales & Traffic for one seller account.
    Returns (daily_sales_dict, brand_sales_dict).
    default_brand: if set, all unmapped ASINs are attributed to this brand
                   (use for single-brand accounts like HighOnLove).
    """
    marketplace = cfg.get("marketplace_id", "ATVPDKIKX0DER")
    sp_token    = get_sp_access_token(cfg, refresh_token_key)

    report_id   = submit_sales_report(cfg, sp_token, start_str, end_str, marketplace)
    doc_id      = poll_report(cfg, sp_token, report_id)
    report_data = download_report(cfg, sp_token, doc_id)

    daily_sales = parse_daily_sales(report_data)
    total = round(sum(daily_sales.values()), 2)
    print(f"  ✓ Account total: ${total:,.2f} over {len(daily_sales)} days")

    brand_sales = {}
    try:
        # For single-brand accounts, build a trivial map (all ASINs → brand)
        if default_brand:
            asin_brand_map = {}
            for entry in report_data.get("salesAndTrafficByAsin", []):
                asin = entry.get("parentAsin", "")
                if asin:
                    asin_brand_map[asin] = default_brand
            print(f"  ✓ Single-brand mapping: {len(asin_brand_map)} ASINs → {default_brand}")
        else:
            # Multi-brand account: use cached map or rebuild
            cache_path = Path(__file__).parent / cache_name
            asin_brand_map = None
            if cache_path.exists():
                try:
                    cached = json.loads(cache_path.read_text())
                    if (cached.get("date") == dt.date.today().isoformat()
                            and cached.get("version") == "sku-prefix-v1"
                            and cached.get("map")):
                        asin_brand_map = cached["map"]
                        print(f"  ✓ ASIN→brand map loaded from cache ({len(asin_brand_map)} ASINs)")
                except Exception:
                    pass
            if asin_brand_map is None:
                asin_brand_map = build_asin_brand_map(
                    cfg, start_str, end_str, refresh_token_key,
                    cache_name=cache_name)

        excluded_brands = set(cfg.get("excluded_brands", [])) if not default_brand else set()
        brand_sales, excluded_period_sales = parse_brand_sales(
            report_data, asin_brand_map, excluded_brands)

        if excluded_period_sales > 0 and total > 0 and daily_sales:
            excl_fraction = excluded_period_sales / total
            daily_sales = {d: round(v * (1 - excl_fraction), 2) for d, v in daily_sales.items()}
            adj = round(sum(daily_sales.values()), 2)
            print(f"  ✓ Adjusted total (excl. excluded brands): ${adj:,.2f}")
        print(f"  ✓ Per-brand totals: {len(brand_sales)} brands")
    except Exception as e:
        print(f"  ⚠ Per-brand sales failed: {e}")

    return daily_sales, brand_sales


# ── Main ───────────────────────────────────────────────────────────────────────

def fetch_total_sales(cfg, lookback_days=30):
    """
    Fetches total portfolio sales + per-brand totals for the lookback period.
    Pulls from all configured seller accounts (Medaltus + HighOnLove if set up).
    Returns (daily_sales_dict, brand_sales_dict).
    """
    end       = dt.date.today() - dt.timedelta(days=1)
    start     = end - dt.timedelta(days=lookback_days - 1)
    start_str = start.isoformat()
    end_str   = end.isoformat()

    print(f"  Fetching total sales {start_str} → {end_str}…")

    # ── Account 1: Medaltus (primary) ─────────────────────────────────────────
    daily_sales, brand_sales = _fetch_one_account(
        cfg, "sp_refresh_token", start_str, end_str,
        cache_name="asin_brand_cache.json",
    )

    # ── Account 2: HighOnLove (if credentials present) ────────────────────────
    if cfg.get("hol_sp_refresh_token") and cfg.get("hol_seller_id"):
        print(f"\n  Fetching HighOnLove Seller Central sales…")
        # HighOnLove is a single-brand account — all ASINs map directly to it
        hol_daily, hol_brands = _fetch_one_account(
            cfg, "hol_sp_refresh_token", start_str, end_str,
            default_brand="HighOnLove",
        )
        # Merge daily totals
        for date, amount in hol_daily.items():
            daily_sales[date] = round(daily_sales.get(date, 0) + amount, 2)
        # Merge brand totals
        for brand, amount in hol_brands.items():
            brand_sales[brand] = round(brand_sales.get(brand, 0) + amount, 2)
        hol_total = round(sum(hol_daily.values()), 2)
        print(f"  ✓ HighOnLove added: ${hol_total:,.2f} total sales")
    elif cfg.get("hol_sp_refresh_token") and not cfg.get("hol_seller_id"):
        print(f"  ℹ  hol_sp_refresh_token found but hol_seller_id missing — skipping HOL account")

    # Canada etc. Previously only fetch_brand_sales_for_period() did this, so this
    # path measured Canadian spend against US-only revenue.
    _add_extra_marketplaces(cfg, start_str, end_str, brand_sales, daily_sales=daily_sales)

    combined_total = round(sum(daily_sales.values()), 2)
    print(f"  ✓ Portfolio total: ${combined_total:,.2f} over {len(daily_sales)} days")
    print(f"  ✓ Per-brand totals: {len(brand_sales)} brands")

    return daily_sales, brand_sales


# ── FX ─────────────────────────────────────────────────────────────────────────
# SP-API reports each marketplace in that marketplace's own currency. Everything here
# was US-only, so nothing needed converting. Canada reports CAD, and adding CAD into a
# USD total would silently corrupt every downstream figure — TACOS, net sales, the
# budget numbers built on them.
#
# Config rate first, then a live lookup. If neither resolves, the marketplace is
# SKIPPED rather than added unconverted. A missing marketplace is visible; a
# mis-summed one is not.
_TS_FX_CACHE: dict = {}

def ts_fx_to_usd(cfg, currency):
    cur = (currency or "USD").upper()
    if cur == "USD":
        return 1.0
    if cur in _TS_FX_CACHE:
        return _TS_FX_CACHE[cur]
    rate = None
    cfg_rate = (cfg.get("fx_rates") or {}).get(cur)
    if cfg_rate:
        try:
            rate = float(cfg_rate)
            print(f"    ✓ FX {cur}→USD = {rate} (config)")
        except (TypeError, ValueError):
            rate = None
    if rate is None:
        try:
            r = requests.get("https://open.er-api.com/v6/latest/" + cur, timeout=15)
            if r.ok:
                v = (r.json().get("rates") or {}).get("USD")
                if v:
                    rate = float(v)
                    print(f"    ✓ FX {cur}→USD = {rate} (live)")
        except Exception as e:
            print(f"    ⚠ FX lookup for {cur} failed: {e}")
    if rate is None:
        # Deliberately NOT cached. Caching the failure meant one network blip on
        # open.er-api.com poisoned the lookup for the rest of the process, so every
        # later marketplace in that currency was skipped without retrying — Canadian
        # revenue vanished while Canadian spend stayed in the numerator, with no signal
        # beyond a single line of log output.
        print(f"    ✗ no {cur}→USD rate — marketplace SKIPPED rather than "
              f"summed unconverted. Set fx_rates.{cur} in config.json.")
        record_integrity_failure(
            f"FX {cur}→USD unavailable — {cur} marketplace revenue excluded while its "
            f"ad spend is still counted, which overstates TACOS")
        return None
    _TS_FX_CACHE[cur] = rate
    return rate


def fetch_brand_sales_for_period(cfg, start_str, end_str):
    """
    Fetches per-brand total sales for an explicit date range from SP-API.
    Combines Medaltus + HighOnLove accounts if both are configured.
    Returns {brand_name: total_sales_usd}.
    """
    print(f"  Fetching brand sales {start_str} → {end_str}…")

    _, brand_sales = _fetch_one_account(
        cfg, "sp_refresh_token", start_str, end_str,
        cache_name="asin_brand_cache.json",
    )

    if cfg.get("hol_sp_refresh_token") and cfg.get("hol_seller_id"):
        print(f"  Fetching HighOnLove sales {start_str} → {end_str}…")
        _, hol_brands = _fetch_one_account(
            cfg, "hol_sp_refresh_token", start_str, end_str,
            default_brand="HighOnLove",
        )
        for brand, amount in hol_brands.items():
            brand_sales[brand] = round(brand_sales.get(brand, 0) + amount, 2)

    _add_extra_marketplaces(cfg, start_str, end_str, brand_sales)

    return brand_sales


# ── Additional marketplaces (Canada, etc.) ────────────────────────────────────
# Canada ad spend flows into the dashboards, so its revenue has to as well or TACOS is
# measured against a denominator that excludes it.
#
# Each marketplace gets its OWN ASIN→brand cache. Canada uses DIFFERENT ASINs from the
# US for the same products (B0H3CLS4XW vs B07RCJDFN4 for 1oz Scar Cream), so reusing
# the US map would leave every Canadian sale unattributed and silently discarded as
# "unmapped". Brands still resolve because the mapping keys off the seller SKU's
# 3-character prefix and CA SKUs keep it — SVA0001-CA.
#
# Shared by fetch_brand_sales_for_period() AND fetch_total_sales(). It lived inline in
# the first one only, which meant the lookback path (the one feeding ads_data's
# summary.totalSales and total_sales_by_date) counted Canadian SPEND while excluding
# Canadian REVENUE. Skinuva's rolling TACOS came out ~1.4pp too high, and it disagreed
# with the same dashboard's monthly figure — which reads as the data being unstable
# rather than as a bug.
def _add_extra_marketplaces(cfg, start_str, end_str, brand_sales, daily_sales=None):
    for mp in (cfg.get("extra_marketplaces") or []):
        mp_id  = mp.get("id")
        label  = mp.get("label") or mp_id
        ccy    = mp.get("currency") or "USD"
        if not mp_id:
            continue
        rate = ts_fx_to_usd(cfg, ccy)
        if rate is None:
            continue
        print(f"  Fetching {label} sales {start_str} → {end_str} ({ccy})…")
        try:
            mp_cfg = {**cfg, "marketplace_id": mp_id}
            mp_daily, mp_brands = _fetch_one_account(
                mp_cfg, "sp_refresh_token", start_str, end_str,
                cache_name=f"asin_brand_cache_{label.lower()}.json",
            )
        except Exception as e:
            print(f"    ⚠ {label} sales fetch failed: {e} — US totals unaffected")
            continue
        for brand, amount in mp_brands.items():
            usd = round(amount * rate, 2)
            brand_sales[brand] = round(brand_sales.get(brand, 0) + usd, 2)
        if daily_sales is not None:
            for date, amount in (mp_daily or {}).items():
                daily_sales[date] = round(daily_sales.get(date, 0) + amount * rate, 2)
        if mp_brands:
            print(f"    ✓ {label}: {len(mp_brands)} brand(s), "
                  f"${round(sum(mp_brands.values()) * rate, 2):,.2f} added in USD")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).parent / "config.json"))
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)

    daily, by_brand = fetch_total_sales(cfg, cfg.get("lookback_days", 30))

    print("\nPortfolio daily totals:")
    for date, sales in sorted(daily.items()):
        print(f"  {date}: ${sales:,.2f}")

    if by_brand:
        print("\nPer-brand totals (period):")
        for brand, sales in sorted(by_brand.items(), key=lambda x: -x[1]):
            print(f"  {brand}: ${sales:,.2f}")
