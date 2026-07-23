#!/usr/bin/env python3
"""
Amazon Ads API Data Fetcher — Brand-Level
Fetches Sponsored Products campaign data from the HighOnLove and NewDerm
profiles, then splits NewDerm campaigns into individual brands by name matching.

Usage:
  python3 fetch_ads_data.py
  python3 fetch_ads_data.py --config /path/to/config.json
"""

import json
import time
import gzip
import argparse
import sys
import io
from datetime import date, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing dependency: run  pip3 install requests  and try again.")
    sys.exit(1)

# ── Region config ──────────────────────────────────────────────────────────────

REGION_URLS = {
    "NA": {
        "token": "https://api.amazon.com/auth/o2/token",
        "api":   "https://advertising-api.amazon.com",
    },
    "EU": {
        "token": "https://api.amazon.com/auth/o2/token",
        "api":   "https://advertising-api-eu.amazon.com",
    },
    "FE": {
        "token": "https://api.amazon.com/auth/o2/token",
        "api":   "https://advertising-api-fe.amazon.com",
    },
}

# Amazon v3 reports can take 10–30 minutes — be patient.
REPORT_POLL_INTERVAL = 30    # seconds between status checks
REPORT_POLL_TIMEOUT  = 5400  # 90 minutes max

# ── Auth ───────────────────────────────────────────────────────────────────────

def get_access_token(cfg: dict, region_urls: dict) -> str:
    resp = requests.post(
        region_urls["token"],
        data={
            "grant_type":    "refresh_token",
            "refresh_token": cfg["refresh_token"],
            "client_id":     cfg["client_id"],
            "client_secret": cfg["client_secret"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    resp.raise_for_status()
    print("✓ Access token obtained")
    return resp.json()["access_token"]


def api_headers(access_token: str, client_id: str, profile_id: str = None) -> dict:
    h = {
        "Authorization":                   f"Bearer {access_token}",
        "Amazon-Advertising-API-ClientId": client_id,
        "Content-Type":                    "application/json",
        "Accept":                          "application/json",
    }
    if profile_id:
        h["Amazon-Advertising-API-Scope"] = str(profile_id)
    return h


# ── Profiles ───────────────────────────────────────────────────────────────────

def list_profiles(api_base: str, access_token: str, client_id: str) -> list:
    resp = requests.get(
        f"{api_base}/v2/profiles",
        headers=api_headers(access_token, client_id),
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def get_profile_name(p: dict) -> str:
    """Safely extract the display name from a profile object."""
    ai = p.get("accountInfo")
    if isinstance(ai, dict):
        name = ai.get("name") or ai.get("id") or ""
    else:
        name = ""
    return name.strip()


def get_profile_type(p: dict) -> str:
    ai = p.get("accountInfo")
    if isinstance(ai, dict):
        return (ai.get("type") or "").lower()
    return ""


def filter_profiles(profiles: list, cfg: dict) -> list:
    """Return only the profiles matching the configured profile IDs (primary)
    or name filters (fallback if no IDs are configured)."""
    allowed_ids = set(cfg.get("profile_ids", []))
    pf = cfg.get("profile_filters", {})
    allowed_names = [n.lower() for n in pf.get("account_names", [])]
    excluded_countries = [c.upper() for c in pf.get("exclude_countries", [])]
    SP_TYPES = {"seller", "vendor", ""}

    filtered = []
    print("\nProfile scan:")
    for p in profiles:
        pid     = p.get("profileId", "?")
        name    = get_profile_name(p)
        country = (p.get("countryCode") or "").upper()
        ptype   = get_profile_type(p)

        # Primary filter: explicit profile ID list
        if allowed_ids:
            if pid in allowed_ids:
                print(f"  USE   {name or pid}  (id={pid})")
                filtered.append(p)
            else:
                print(f"  SKIP  {name or pid}  (id={pid})  [not in profile_ids]")
            continue

        # Fallback: name + type + country filters
        reasons = []
        if ptype and ptype not in SP_TYPES:
            reasons.append(f"type={ptype}")
        if country in excluded_countries:
            reasons.append(f"country={country}")
        if allowed_names and not any(n in name.lower() for n in allowed_names):
            reasons.append("name not in allow-list")

        if reasons:
            print(f"  SKIP  {name or pid}  [{', '.join(reasons)}]")
        else:
            print(f"  USE   {name or pid}  (id={pid})")
            filtered.append(p)

    print(f"\n✓ Using {len(filtered)} profile(s)\n")
    return filtered


# ── Reporting v3 ───────────────────────────────────────────────────────────────

# Per-ad-product report configs.
# SB uses different column names from SP: cost/purchases/salesAmount instead of spend/purchases7d/sales7d.
AD_PRODUCT_CONFIGS = [
    {
        "adProduct":    "SPONSORED_PRODUCTS",
        "reportTypeId": "spCampaigns",
        "columns": [
            "date", "campaignId", "campaignName", "campaignStatus",
            "campaignBudgetAmount", "campaignBudgetType",
            "impressions", "clicks", "spend", "purchases7d", "sales7d",
        ],
        # No normalization needed — column names match build_brand_data expectations.
        "normalize": None,
    },
    {
        "adProduct":    "SPONSORED_BRANDS",
        "reportTypeId": "sbCampaigns",
        "columns": [
            "date", "campaignId", "campaignName", "campaignStatus",
            "campaignBudgetAmount", "campaignBudgetType",
            "impressions", "clicks", "cost", "purchases", "sales",
        ],
        # Rename SB columns to the names build_brand_data expects.
        "normalize": lambda r: r.update({
            "spend":       r.pop("cost",      0) or 0,
            "sales7d":     r.pop("sales",     0) or 0,
            "purchases7d": r.pop("purchases", 0) or 0,
        }) or r,
    },
    {
        "adProduct":    "SPONSORED_DISPLAY",
        "reportTypeId": "sdCampaigns",
        "columns": [
            "date", "campaignId", "campaignName", "campaignStatus",
            "campaignBudgetAmount", "campaignBudgetType",
            "impressions", "clicks", "cost", "purchases", "sales",
        ],
        # Rename SD columns to the names build_brand_data expects (same as SB).
        "normalize": lambda r: r.update({
            "spend":       r.pop("cost",      0) or 0,
            "sales7d":     r.pop("sales",     0) or 0,
            "purchases7d": r.pop("purchases", 0) or 0,
        }) or r,
    },
]


def submit_campaign_report(api_base: str, hdrs: dict, start: str, end: str,
                            ad_product: str = "SPONSORED_PRODUCTS") -> str:
    """Submit a daily campaigns report for the given ad product. Returns reportId."""
    cfg = next(c for c in AD_PRODUCT_CONFIGS if c["adProduct"] == ad_product)

    payload = {
        "name":      f"{ad_product.replace('_', ' ').title()} Campaigns Daily",
        "startDate": start,
        "endDate":   end,
        "configuration": {
            "adProduct":    ad_product,
            "groupBy":      ["campaign"],
            "columns":      cfg["columns"],
            "reportTypeId": cfg["reportTypeId"],
            "timeUnit":     "DAILY",
            "format":       "GZIP_JSON",
        },
    }
    resp = requests.post(
        f"{api_base}/reporting/reports",
        headers={**hdrs, "Content-Type": "application/vnd.createasyncreportrequest.v3+json"},
        json=payload,
        timeout=30,
    )
    if resp.status_code == 425:
        # Amazon detected a duplicate — parse and reuse the existing report ID
        import re as _re
        m = _re.search(r'duplicate of\s*[:\s]+([a-f0-9-]{36})', resp.text, _re.IGNORECASE)
        if m:
            report_id = m.group(1)
            print(f"  ↩  Duplicate — reusing existing {ad_product} (reportId={report_id})")
            return report_id
    if not resp.ok:
        print(f"    Report submission error {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
    report_id = resp.json()["reportId"]
    print(f"  ✓ Submitted {ad_product} (reportId={report_id})")
    return report_id


def poll_report(api_base: str, hdrs: dict, report_id: str) -> str:
    """Poll until COMPLETED; return presigned download URL."""
    deadline   = time.time() + REPORT_POLL_TIMEOUT
    start_time = time.time()
    last_msg   = time.time()
    print(f"  ⏳ Waiting for report (may take 5–20 min)…")

    while time.time() < deadline:
        resp = requests.get(
            f"{api_base}/reporting/reports/{report_id}",
            headers=hdrs,
            timeout=20,
        )
        resp.raise_for_status()
        data   = resp.json()
        status = data.get("status", "")

        if status == "COMPLETED":
            elapsed = int(time.time() - start_time)
            print(f"  ✓ Report ready ({elapsed}s)")
            return data["url"]

        if status in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"Report ended with status: {status} — {data}")

        # Progress message every minute
        if time.time() - last_msg >= 60:
            elapsed = int(time.time() - start_time)
            print(f"    Still waiting… ({elapsed // 60}m {elapsed % 60}s elapsed, status={status})")
            last_msg = time.time()

        time.sleep(REPORT_POLL_INTERVAL)

    raise TimeoutError(f"Report not ready after {REPORT_POLL_TIMEOUT}s")


def download_report(url: str) -> list:
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    with gzip.open(io.BytesIO(resp.content)) as f:
        return json.loads(f.read().decode("utf-8"))


# ── Helpers ────────────────────────────────────────────────────────────────────

def date_range(lookback_days: int):
    end   = date.today() - timedelta(days=1)
    start = end - timedelta(days=lookback_days - 1)
    return start.isoformat(), end.isoformat()


def safe_div(a, b):
    return round(a / b, 4) if b else None


def acos_pct(spend, sales):
    return round(spend / sales * 100, 2) if sales else None


def roas_val(sales, spend):
    return round(sales / spend, 2) if spend else None


# ── Brand identification ───────────────────────────────────────────────────────

def identify_brand(campaign_name: str, brands: list) -> str:
    """Match a campaign name to a brand (longest match wins)."""
    name_lower = campaign_name.lower()
    for brand in sorted(brands, key=len, reverse=True):
        if brand.lower() in name_lower:
            return brand
    return "Other"


# ── Brand aggregation ──────────────────────────────────────────────────────────

def build_brand_data(records: list, brand_name: str, brands: list,
                     single_brand: bool) -> dict:
    """Aggregate campaign records for one brand into summary + timeline."""

    def matches(r):
        if single_brand:
            return True
        return identify_brand(r.get("campaignName", ""), brands) == brand_name

    brand_records = [r for r in records if matches(r)]
    if not brand_records:
        return None

    # Timeline: daily totals
    by_date = {}
    # Campaign list: one row per campaign (summed across days)
    by_campaign = {}
    # Per-day per-campaign: enables month-accurate campaign tables in the dashboard
    by_campaign_date = {}
    # Budget pacing: latest daily budget per campaign
    budgets = {}

    for r in brand_records:
        d   = r.get("date", "")
        cid = r.get("campaignId", "")
        cn  = r.get("campaignName", "")
        imp = int(r.get("impressions", 0) or 0)
        clk = int(r.get("clicks", 0) or 0)
        spd = float(r.get("spend", 0) or 0)
        sls = float(r.get("sales7d", 0) or 0)
        pur = int(r.get("purchases7d", 0) or 0)

        # Timeline
        if d:
            if d not in by_date:
                by_date[d] = {"date": d, "impressions": 0, "clicks": 0,
                               "spend": 0.0, "sales": 0.0, "purchases": 0}
            by_date[d]["impressions"] += imp
            by_date[d]["clicks"]      += clk
            by_date[d]["spend"]       += spd
            by_date[d]["sales"]       += sls
            by_date[d]["purchases"]   += pur

        # Campaign summary (full-period aggregate)
        if cid:
            if cid not in by_campaign:
                by_campaign[cid] = {"id": cid, "name": cn,
                                     "impressions": 0, "clicks": 0,
                                     "spend": 0.0, "sales": 0.0, "purchases": 0}
            by_campaign[cid]["impressions"] += imp
            by_campaign[cid]["clicks"]      += clk
            by_campaign[cid]["spend"]       += spd
            by_campaign[cid]["sales"]       += sls
            by_campaign[cid]["purchases"]   += pur

        # Per-day per-campaign (for month-accurate campaign tables)
        if d and cid:
            if d not in by_campaign_date:
                by_campaign_date[d] = {}
            if cid not in by_campaign_date[d]:
                by_campaign_date[d][cid] = {"name": cn, "spend": 0.0,
                                             "sales": 0.0, "purchases": 0,
                                             "impressions": 0, "clicks": 0}
            by_campaign_date[d][cid]["spend"]       += spd
            by_campaign_date[d][cid]["sales"]       += sls
            by_campaign_date[d][cid]["purchases"]   += pur
            by_campaign_date[d][cid]["impressions"] += imp
            by_campaign_date[d][cid]["clicks"]      += clk

        # Budget (store latest)
        budget = float(r.get("campaignBudgetAmount", 0) or 0)
        if cid and budget:
            budgets[cid] = {
                "id":          cid,
                "name":        cn,
                "state":       r.get("campaignStatus", ""),
                "dailyBudget": round(budget, 2),
            }

    # Finalize campaigns
    campaigns = []
    for c in by_campaign.values():
        c["spend"] = round(c["spend"], 2)
        c["sales"] = round(c["sales"], 2)
        c["ctr"]   = safe_div(c["clicks"], c["impressions"])
        c["cpc"]   = safe_div(c["spend"], c["clicks"])
        c["acos"]  = acos_pct(c["spend"], c["sales"])
        c["roas"]  = roas_val(c["sales"], c["spend"])
        campaigns.append(c)
    campaigns.sort(key=lambda x: x["spend"], reverse=True)

    # Finalize timeline
    timeline = []
    for row in sorted(by_date.values(), key=lambda x: x["date"]):
        row["spend"] = round(row["spend"], 2)
        row["sales"] = round(row["sales"], 2)
        row["acos"]  = acos_pct(row["spend"], row["sales"])
        row["roas"]  = roas_val(row["sales"], row["spend"])
        timeline.append(row)

    # Compute pacing (recent spend = last 7 days of timeline)
    recent = timeline[-7:] if len(timeline) >= 7 else timeline
    pacing = []
    for b in budgets.values():
        cid   = b["id"]
        spd7  = sum(
            r["spend"]
            for tr in recent
            for r2 in brand_records
            if r2.get("campaignId") == cid and r2.get("date") == tr["date"]
            for _ in [float(r2.get("spend", 0) or 0)]
        )
        # Simpler: just use total spend / days
        total_spd = by_campaign.get(cid, {}).get("spend", 0)
        days      = max(len(by_date), 1)
        avg_daily = total_spd / days
        pct       = round(avg_daily / b["dailyBudget"] * 100, 1) if b["dailyBudget"] else None
        pacing.append({
            "id":          b["id"],
            "name":        b["name"],
            "state":       b["state"].lower(),
            "dailyBudget": b["dailyBudget"],
            "recentSpend": round(avg_daily, 2),
            "pacingPct":   pct,
        })
    pacing.sort(key=lambda x: x["dailyBudget"] or 0, reverse=True)

    # Brand summary
    imp  = sum(by_date[d]["impressions"] for d in by_date)
    clk  = sum(by_date[d]["clicks"]      for d in by_date)
    spd  = round(sum(by_date[d]["spend"]  for d in by_date), 2)
    sls  = round(sum(by_date[d]["sales"]  for d in by_date), 2)
    pur  = sum(c["purchases"] for c in campaigns)

    return {
        "name":      brand_name,
        "summary": {
            "impressions": imp, "clicks": clk, "spend": spd, "sales": sls,
            "purchases": pur,
            "ctr":  safe_div(clk, imp),
            "cpc":  safe_div(spd, clk),
            "acos": acos_pct(spd, sls),
            "roas": roas_val(sls, spd),
        },
        "campaigns": campaigns[:100],
        "keywords":  [],  # not fetched in this run
        "products":  [],  # not fetched in this run
        "pacing":    pacing,
        "timeline":  timeline,
        # Per-day campaign data — used to build month-accurate campaign tables
        "campaign_timeline": [
            {
                "date": d,
                "campaigns": [
                    {
                        "id":          cid,
                        "name":        cd["name"],
                        "spend":       round(cd["spend"], 2),
                        "sales":       round(cd["sales"], 2),
                        "purchases":   cd["purchases"],
                        "impressions": cd["impressions"],
                        "clicks":      cd["clicks"],
                        "cpc":         safe_div(cd["spend"], cd["clicks"]),
                        "acos":        acos_pct(cd["spend"], cd["sales"]),
                        "roas":        roas_val(cd["sales"], cd["spend"]),
                    }
                    for cid, cd in sorted(
                        by_campaign_date[d].items(),
                        key=lambda x: x[1]["spend"], reverse=True
                    )
                    if cd["spend"] > 0
                ]
            }
            for d in sorted(by_campaign_date)
        ],
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).parent / "config.json"))
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)

    region   = cfg.get("region", "NA").upper()
    urls     = REGION_URLS[region]
    lookback = int(cfg.get("lookback_days", 30))
    brands   = cfg.get("brands", [])
    single_brand_profiles = cfg.get("single_brand_profiles", {})

    print(f"\n{'═'*60}")
    print(f"  Amazon Ads Fetcher  |  region={region}  |  lookback={lookback}d")
    print(f"  Brands: {', '.join(brands[:5])}{'…' if len(brands) > 5 else ''}")
    print(f"{'═'*60}")

    access_token = get_access_token(cfg, urls)
    all_profiles = list_profiles(urls["api"], access_token, cfg["client_id"])
    print(f"✓ Found {len(all_profiles)} total profile(s)")
    profiles = filter_profiles(all_profiles, cfg)

    if not profiles:
        print("✗ No profiles matched — check profile_filters in config.json")
        sys.exit(1)

    start, end = date_range(lookback)
    print(f"Date range: {start} → {end}\n")

    # Output: keyed by brand name
    brand_outputs = {}
    currency = "USD"

    import threading

    # ── Phase 1: Submit ALL reports for ALL profiles upfront ───────────────────
    # Each profile gets a fresh token. All reports run in parallel (no serial waiting).
    print("Submitting all reports…\n")

    # pending: list of (profile, ap_cfg, report_id, hdrs, single_brand, currency)
    pending = []

    for profile in profiles:
        profile_id   = profile.get("profileId")
        profile_name = get_profile_name(profile) or str(profile_id)
        currency     = profile.get("currencyCode") or "USD"
        single_brand = single_brand_profiles.get(profile_name)

        print(f"━━ {profile_name} (id={profile_id}) ━━")
        access_token = get_access_token(cfg, urls)
        hdrs = api_headers(access_token, cfg["client_id"], profile_id)

        for idx_cfg, ap_cfg in enumerate(AD_PRODUCT_CONFIGS):
            if idx_cfg > 0:
                time.sleep(5)   # brief gap between submissions to avoid 429
            report_id = None
            for attempt in range(3):
                try:
                    report_id = submit_campaign_report(
                        urls["api"], hdrs, start, end, ap_cfg["adProduct"])
                    break
                except Exception as e:
                    if "429" in str(e) and attempt < 2:
                        wait = 30 * (attempt + 1)
                        print(f"  ⏸  Rate limited — retrying in {wait}s…")
                        time.sleep(wait)
                    else:
                        print(f"  ✗ {ap_cfg['adProduct']} submission failed: {e}")
                        break
            if report_id:
                pending.append((profile, ap_cfg, report_id, hdrs, single_brand, currency))

        time.sleep(5)   # small gap between profiles

    print(f"\n✓ {len(pending)} reports submitted — polling all simultaneously…\n")

    # ── Phase 2: Poll + download all reports in parallel via threads ───────────
    downloaded = {}   # report_id → rows
    dl_lock    = threading.Lock()

    def fetch_report(ap_cfg, report_id, hdrs):
        for attempt in range(3):   # retry up to 3× on connection errors
            try:
                url  = poll_report(urls["api"], hdrs, report_id)
                rows = download_report(url)
                if ap_cfg["normalize"]:
                    for r in rows:
                        ap_cfg["normalize"](r)
                with dl_lock:
                    downloaded[report_id] = rows
                print(f"  ✓ {ap_cfg['adProduct']} ({report_id[:8]}…): {len(rows)} rows")
                return
            except Exception as e:
                is_conn = any(k in str(e) for k in ("ConnectionReset", "Connection reset", "ConnectionAborted", "RemoteDisconnected"))
                if is_conn and attempt < 2:
                    print(f"  ↩  Connection reset, retrying {ap_cfg['adProduct']} ({attempt+1}/2)…")
                    time.sleep(10)
                else:
                    print(f"  ✗ {ap_cfg['adProduct']} ({report_id[:8]}…) failed: {e}")
                    with dl_lock:
                        downloaded[report_id] = []
                    return

    threads = [threading.Thread(target=fetch_report, args=(ap_cfg, report_id, hdrs),
                                daemon=True)
               for _, ap_cfg, report_id, hdrs, _, _ in pending]
    for t in threads: t.start()
    for t in threads: t.join()

    print(f"\n✓ All reports ready — processing brand data…\n")

    # ── Phase 3: Build brand data per profile ──────────────────────────────────
    # Group downloaded rows by profile
    profile_records = {}   # profile_id → [rows]
    for profile, ap_cfg, report_id, _, _, _ in pending:
        pid = profile.get("profileId")
        profile_records.setdefault(pid, []).extend(downloaded.get(report_id, []))

    for profile in profiles:
        profile_id   = profile.get("profileId")
        profile_name = get_profile_name(profile) or str(profile_id)
        currency     = profile.get("currencyCode") or "USD"
        single_brand = single_brand_profiles.get(profile_name)

        records = profile_records.get(profile_id, [])

        print(f"━━ {profile_name} ━━")
        if not records:
            print(f"  → Skipping (no data)\n")
            continue
        print(f"  ✓ Combined: {len(records)} total campaign-day rows")

        # Split into brands
        target_brands = [single_brand] if single_brand else brands

        for brand in target_brands:
            bdata = build_brand_data(records, brand, brands, bool(single_brand))
            if bdata:
                bdata["profile"]  = profile_name
                bdata["currency"] = currency
                brand_outputs[brand] = bdata
                s = bdata["summary"]
                print(f"  ✓ {brand}: spend=${s['spend']:,.0f}, "
                      f"sales=${s['sales']:,.0f}, acos={s['acos']}%")
            else:
                print(f"  → {brand}: no data (no matching campaigns)")

        print()

    # ── Build output ───────────────────────────────────────────────────────────

    brands_out = [brand_outputs[b] for b in brands if b in brand_outputs]

    if not brands_out:
        print("✗ No brand data collected. Check that campaigns exist in the selected profiles.")
        sys.exit(1)

    def portfolio():
        imp = sum(b["summary"]["impressions"] for b in brands_out)
        clk = sum(b["summary"]["clicks"]      for b in brands_out)
        spd = round(sum(b["summary"]["spend"]  for b in brands_out), 2)
        sls = round(sum(b["summary"]["sales"]  for b in brands_out), 2)
        pur = sum(b["summary"]["purchases"]    for b in brands_out)
        return {
            "impressions": imp, "clicks": clk, "spend": spd,
            "sales": sls, "purchases": pur,
            "ctr":  safe_div(clk, imp),
            "cpc":  safe_div(spd, clk),
            "acos": acos_pct(spd, sls),
            "roas": roas_val(sls, spd),
        }

    # ── Fetch total sales from SP-API Reports ─────────────────────────────────
    total_sales_by_date = {}
    if cfg.get("sp_refresh_token") and cfg.get("sp_client_id"):
        try:
            from fetch_total_sales import fetch_total_sales as _fetch_ts
            print("\nFetching total portfolio sales from SP-API…")
            total_sales_by_date, brand_sales_period = _fetch_ts(cfg, lookback)
            # Attach daily portfolio total sales to each brand's timeline
            for b in brands_out:
                for row in b.get("timeline", []):
                    row["totalSales"] = total_sales_by_date.get(row["date"], 0) or 0
            # Attach period total sales to each brand summary
            for b in brands_out:
                b["summary"]["totalSales"] = brand_sales_period.get(b["name"], 0)
        except Exception as e:
            print(f"  ⚠ Total sales fetch failed (TACOS will be unavailable): {e}")
    else:
        print("\n  (SP-API credentials not configured — skipping total sales)")

    output = {
        "fetched_at":          time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lookback_days":       lookback,
        "currency":            currency,
        "portfolio":           portfolio(),
        "brands":              brands_out,
        "total_sales_by_date": total_sales_by_date,
    }

    out_path = Path(args.config).parent / "ads_data.js"
    out_path.write_text(
        f"/* Auto-generated by fetch_ads_data.py — do not edit */\n"
        f"const ADS_DATA = {json.dumps(output, indent=2)};\n"
    )
    p = output["portfolio"]
    print(f"{'═'*60}")
    print(f"  ✓ {len(brands_out)} brands written to ads_data.js")
    print(f"  Portfolio: spend=${p['spend']:,.0f}, "
          f"sales=${p['sales']:,.0f}, acos={p['acos']}%")
    print(f"{'═'*60}")
    print(f"\n  Open dashboard.html in your browser to see your data.\n")


if __name__ == "__main__":
    main()
