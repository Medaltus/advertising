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
    resp = requests.post(
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
        resp = requests.get(
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
    resp = requests.get(
        f"https://{SPAPI_HOST}{path}",
        headers=sigv4_headers("GET", path, "", token, "", cfg),
        timeout=20,
    )
    resp.raise_for_status()
    doc_info = resp.json()

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


def build_asin_brand_map(cfg, start_str, end_str, refresh_token_key="sp_refresh_token"):
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

            # Write ASIN→title map for use by fetch_ads_data.py
            if asin_title:
                try:
                    titles_path = Path(__file__).parent / "asin_titles.json"
                    titles_path.write_text(json.dumps(asin_title))
                except Exception:
                    pass

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
    # Cache the map — use a separate file per account token
    cache_name = "asin_brand_cache.json" if refresh_token_key == "sp_refresh_token" else f"asin_brand_cache_{refresh_token_key.replace('sp_refresh_token','')}.json"
    cache_path = Path(__file__).parent / cache_name
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
                asin_brand_map = build_asin_brand_map(cfg, start_str, end_str, refresh_token_key)

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

    combined_total = round(sum(daily_sales.values()), 2)
    print(f"  ✓ Portfolio total: ${combined_total:,.2f} over {len(daily_sales)} days")
    print(f"  ✓ Per-brand totals: {len(brand_sales)} brands")

    return daily_sales, brand_sales


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

    return brand_sales


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
