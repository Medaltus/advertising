#!/usr/bin/env python3
"""
Google Ads Data Fetcher — Medaltus (nightly)
Fetches campaign performance by month and merges into
data/medaltus_google_ads_monthly.json.

Usage:
  python3 fetch_medaltus_google_ads.py
  python3 fetch_medaltus_google_ads.py --months 3
"""

import json
import time
import argparse
import sys
from datetime import date, timedelta, datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing dependency: run  pip3 install requests --break-system-packages  and try again.")
    sys.exit(1)

MONTH_NAMES = [
    'January','February','March','April','May','June',
    'July','August','September','October','November','December'
]

API_VERSION = "v23"
API_BASE    = f"https://googleads.googleapis.com/{API_VERSION}"

# ── Load credentials ──────────────────────────────────────────────────────────

def _load_config(explicit_path=None):
    script_dir = Path(__file__).parent.resolve()
    candidates = [
        Path(explicit_path) if explicit_path else None,
        Path.cwd() / 'config.json',
        script_dir / 'config.json',
        script_dir.parent / 'config.json',
        Path.home() / 'Documents' / 'Claude' / 'Projects' / 'All Brands Ad Dashboard' / 'config.json',
    ]
    for p in candidates:
        if p and p.exists():
            print(f"  Using config: {p}")
            with open(p) as f:
                cfg = json.load(f).get('medaltus_google_ads', {})
            if not cfg:
                print("✗ 'medaltus_google_ads' key not found in config.json")
                sys.exit(1)
            return cfg
    print("✗ config.json not found.")
    sys.exit(1)

DEVELOPER_TOKEN = CLIENT_ID = CLIENT_SECRET = REFRESH_TOKEN = CUSTOMER_ID = MANAGER_ID = None

# ── Auth ──────────────────────────────────────────────────────────────────────

def get_access_token():
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type":    "refresh_token",
            "refresh_token": REFRESH_TOKEN,
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=20,
    )
    resp.raise_for_status()
    print("✓ Access token obtained")
    return resp.json()["access_token"]


def api_headers(token):
    return {
        "Authorization":     f"Bearer {token}",
        "developer-token":   DEVELOPER_TOKEN,
        "login-customer-id": MANAGER_ID,
        "Content-Type":      "application/json",
    }


def fetch_all_pages(token, query):
    rows, page_token = [], None
    while True:
        payload = {"query": query}
        if page_token:
            payload["pageToken"] = page_token
        resp = requests.post(
            f"{API_BASE}/customers/{CUSTOMER_ID}/googleAds:search",
            headers=api_headers(token),
            json=payload,
            timeout=60,
        )
        if not resp.ok:
            try:
                err = resp.json().get("error", {}).get("message", resp.text[:500])
            except Exception:
                err = resp.text[:500]
            raise RuntimeError(f"Google Ads API {resp.status_code}: {err}")
        data = resp.json()
        rows.extend(data.get("results", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return rows

# ── Helpers ───────────────────────────────────────────────────────────────────

def micros(v):
    return round(int(v or 0) / 1_000_000, 2)

def safe_div(a, b, d=4):
    return round(a / b, d) if b else None

def month_key(date_str):
    dt = datetime.strptime(date_str[:10], '%Y-%m-%d')
    return f"{MONTH_NAMES[dt.month - 1]} {dt.year}"

def date_range_for_months(n_months):
    """Return start/end covering the last n_months calendar months + current month so far."""
    today = date.today()
    # Start = first day of (n_months ago)
    start_month = today.month - n_months
    start_year  = today.year
    while start_month <= 0:
        start_month += 12
        start_year  -= 1
    start = date(start_year, start_month, 1)
    end   = today - timedelta(days=1)
    return start.isoformat(), end.isoformat()

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=2,
                        help="How many prior calendar months to refresh (default 2 = last month + current)")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    global DEVELOPER_TOKEN, CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, CUSTOMER_ID, MANAGER_ID
    cfg = _load_config(args.config)
    DEVELOPER_TOKEN = cfg['developer_token']
    CLIENT_ID       = cfg['client_id']
    CLIENT_SECRET   = cfg['client_secret']
    REFRESH_TOKEN   = cfg['refresh_token']
    CUSTOMER_ID     = cfg['customer_id']
    MANAGER_ID      = cfg['manager_id']

    start, end = date_range_for_months(args.months)

    print(f"\n{'═'*60}")
    print(f"  Google Ads Fetcher  |  Medaltus  |  {start} → {end}")
    print(f"{'═'*60}\n")

    token = get_access_token()

    # Query by campaign + month segment
    query = f"""
        SELECT
          segments.month,
          campaign.id,
          campaign.name,
          campaign.advertising_channel_type,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions,
          metrics.conversions_value
        FROM campaign
        WHERE segments.date BETWEEN '{start}' AND '{end}'
          AND campaign.status != 'REMOVED'
          AND metrics.impressions > 0
        ORDER BY segments.month ASC, metrics.cost_micros DESC
    """

    print("Fetching campaign data by month...")
    rows = fetch_all_pages(token, query)
    print(f"  ✓ {len(rows)} campaign-month rows")

    # Aggregate by month → campaign
    by_month_campaign = {}   # month → campaign_id → dict
    by_month_summary  = {}   # month → totals

    for r in rows:
        seg  = r.get("segments", {})
        camp = r.get("campaign", {})
        met  = r.get("metrics", {})

        raw_month = seg.get("month", "")
        if not raw_month:
            continue
        mk    = month_key(raw_month)
        cid   = camp.get("id", "")
        cname = camp.get("name", "")
        ctype = camp.get("advertisingChannelType", "")

        imp  = int(met.get("impressions", 0) or 0)
        clk  = int(met.get("clicks", 0) or 0)
        spd  = micros(met.get("costMicros", 0))
        conv = float(met.get("conversions", 0) or 0)
        rev  = float(met.get("conversionsValue", 0) or 0)

        # Per-campaign
        if mk not in by_month_campaign:
            by_month_campaign[mk] = {}
        if cid not in by_month_campaign[mk]:
            by_month_campaign[mk][cid] = {
                "campaign_id": cid, "campaign_name": cname,
                "type": ctype,
                "impressions": 0, "clicks": 0,
                "spend": 0.0, "conversions": 0.0, "revenue": 0.0,
            }
        c = by_month_campaign[mk][cid]
        c["impressions"] += imp
        c["clicks"]      += clk
        c["spend"]       += spd
        c["conversions"] += conv
        c["revenue"]     += rev

        # Monthly summary
        if mk not in by_month_summary:
            by_month_summary[mk] = {"impressions": 0, "clicks": 0,
                                     "spend": 0.0, "conversions": 0.0, "revenue": 0.0}
        s = by_month_summary[mk]
        s["impressions"] += imp
        s["clicks"]      += clk
        s["spend"]       += spd
        s["conversions"] += conv
        s["revenue"]     += rev

    # Compute derived metrics
    def enrich(d):
        d["spend"]       = round(d["spend"], 2)
        d["revenue"]     = round(d["revenue"], 2)
        d["conversions"] = round(d["conversions"])
        d["cpc"]  = safe_div(d["spend"], d["clicks"], 2)
        d["ctr"]  = safe_div(d["clicks"], d["impressions"], 4)
        d["cpl"]  = safe_div(d["spend"], d["conversions"], 2) if d["conversions"] else None
        return d

    for mk in by_month_summary:
        enrich(by_month_summary[mk])
        print(f"  {mk}: spend=${by_month_summary[mk]['spend']:,.0f}, "
              f"conv={int(by_month_summary[mk]['conversions'])}, "
              f"clicks={by_month_summary[mk]['clicks']:,}")
        for cid, c in by_month_campaign[mk].items():
            enrich(c)

    # Build flat list sorted by month (asc) then spend (desc)
    def sort_month(mk):
        try: return datetime.strptime(mk, "%B %Y")
        except: return datetime.min

    flat_campaigns = []
    for mk in sorted(by_month_campaign.keys(), key=sort_month):
        for c in sorted(by_month_campaign[mk].values(), key=lambda x: x["spend"], reverse=True):
            flat_campaigns.append({"month": mk, **c})

    # Load existing JSON and merge
    out_dir  = Path(__file__).parent / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "medaltus_google_ads_monthly.json"

    existing = {}
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
        except Exception:
            pass

    # existing structure: { "monthly_summary": {...}, "by_month_campaign": [...] }
    existing_summary    = existing.get("monthly_summary", {})
    existing_campaigns  = existing.get("by_month_campaign", [])

    # Merge summary (overwrite refreshed months)
    for mk, s in by_month_summary.items():
        existing_summary[mk] = s

    # Sort summary chronologically
    existing_summary = dict(sorted(existing_summary.items(), key=lambda x: sort_month(x[0])))

    # Merge campaigns: remove old entries for refreshed months, add new
    refreshed_months = set(by_month_summary.keys())
    kept = [c for c in existing_campaigns if c["month"] not in refreshed_months]
    merged_campaigns = sorted(kept + flat_campaigns, key=lambda x: (sort_month(x["month"]), -x["spend"]))

    output = {
        "fetched_at":        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "monthly_summary":   existing_summary,
        "by_month_campaign": merged_campaigns,
    }

    out_path.write_text(json.dumps(output, indent=2))
    print(f"\n✓ Written to {out_path}")
    print(f"  Months in file: {len(existing_summary)}")
    print(f"  Campaign-month rows: {len(merged_campaigns)}")


if __name__ == "__main__":
    main()
