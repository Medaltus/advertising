#!/usr/bin/env python3
"""
Google Ads Historical Backfill — Medaltus
Run once to populate data/medaltus_google_ads_monthly.json with all history.

Usage (run from deploy/ directory):
  python3 backfill_medaltus_google_ads_monthly.py
  python3 backfill_medaltus_google_ads_monthly.py --start 2024-01-01 --end 2026-06-30
"""

import json
import sys
import argparse
from datetime import datetime
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

def micros(v):
    return round(int(v or 0) / 1_000_000, 2)

def safe_div(a, b, d=4):
    return round(a / b, d) if b else None

def month_key(date_str):
    dt = datetime.strptime(date_str[:10], '%Y-%m-%d')
    return f"{MONTH_NAMES[dt.month - 1]} {dt.year}"

def sort_month(mk):
    try: return datetime.strptime(mk, "%B %Y")
    except: return datetime.min

def enrich(d):
    d["spend"]       = round(d.get("spend", 0), 2)
    d["revenue"]     = round(d.get("revenue", 0), 2)
    d["conversions"] = int(round(d.get("conversions", 0)))
    d["cpc"]  = safe_div(d["spend"], d.get("clicks", 0), 2)
    d["ctr"]  = safe_div(d.get("clicks", 0), d.get("impressions", 0), 4)
    d["cpl"]  = safe_div(d["spend"], d["conversions"], 2) if d["conversions"] else None
    return d

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start",  default="2024-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end",    default=None,         help="End date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    from datetime import date, timedelta
    end = args.end or (date.today() - timedelta(days=1)).isoformat()

    global DEVELOPER_TOKEN, CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, CUSTOMER_ID, MANAGER_ID
    cfg = _load_config(args.config)
    DEVELOPER_TOKEN = cfg['developer_token']
    CLIENT_ID       = cfg['client_id']
    CLIENT_SECRET   = cfg['client_secret']
    REFRESH_TOKEN   = cfg['refresh_token']
    CUSTOMER_ID     = cfg['customer_id']
    MANAGER_ID      = cfg['manager_id']

    print(f"\n{'═'*60}")
    print(f"  Google Ads Backfill  |  Medaltus")
    print(f"  Range: {args.start} → {end}")
    print(f"{'═'*60}\n")

    token = get_access_token()

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
        WHERE segments.date BETWEEN '{args.start}' AND '{end}'
          AND campaign.status != 'REMOVED'
          AND metrics.impressions > 0
        ORDER BY segments.month ASC, metrics.cost_micros DESC
    """

    print("Querying Google Ads API...")
    rows = fetch_all_pages(token, query)
    print(f"  ✓ {len(rows)} campaign-month rows returned")

    by_month_campaign = {}
    by_month_summary  = {}

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

        if mk not in by_month_campaign:
            by_month_campaign[mk] = {}
        if cid not in by_month_campaign[mk]:
            by_month_campaign[mk][cid] = {
                "campaign_id": cid, "campaign_name": cname, "type": ctype,
                "impressions": 0, "clicks": 0,
                "spend": 0.0, "conversions": 0.0, "revenue": 0.0,
            }
        c = by_month_campaign[mk][cid]
        c["impressions"] += imp; c["clicks"] += clk
        c["spend"] += spd; c["conversions"] += conv; c["revenue"] += rev

        if mk not in by_month_summary:
            by_month_summary[mk] = {"impressions": 0, "clicks": 0,
                                     "spend": 0.0, "conversions": 0.0, "revenue": 0.0}
        s = by_month_summary[mk]
        s["impressions"] += imp; s["clicks"] += clk
        s["spend"] += spd; s["conversions"] += conv; s["revenue"] += rev

    for mk in sorted(by_month_summary.keys(), key=sort_month):
        enrich(by_month_summary[mk])
        s = by_month_summary[mk]
        print(f"  {mk}: spend=${s['spend']:,.0f}, conv={s['conversions']}, clicks={s['clicks']:,}")
        for c in by_month_campaign[mk].values():
            enrich(c)

    flat_campaigns = []
    for mk in sorted(by_month_campaign.keys(), key=sort_month):
        for c in sorted(by_month_campaign[mk].values(), key=lambda x: x["spend"], reverse=True):
            flat_campaigns.append({"month": mk, **c})

    sorted_summary = dict(sorted(by_month_summary.items(), key=lambda x: sort_month(x[0])))

    output = {
        "fetched_at":        __import__('time').strftime("%Y-%m-%dT%H:%M:%SZ", __import__('time').gmtime()),
        "monthly_summary":   sorted_summary,
        "by_month_campaign": flat_campaigns,
    }

    out_dir  = Path(__file__).parent / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "medaltus_google_ads_monthly.json"
    out_path.write_text(json.dumps(output, indent=2))

    print(f"\n✓ Written {len(sorted_summary)} months, {len(flat_campaigns)} campaign rows → {out_path}")

if __name__ == "__main__":
    main()
