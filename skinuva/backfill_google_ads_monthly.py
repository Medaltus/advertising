#!/usr/bin/env python3
"""
Google Ads Historical Backfill — Skinuva
Queries Google Ads API for monthly totals and merges into skinuva_monthly.json.

Usage (run once from the deploy/ directory):
  python3 skinuva/backfill_google_ads_monthly.py
  python3 skinuva/backfill_google_ads_monthly.py --start 2024-07-01 --end 2026-04-30
"""

import json
import sys
import argparse
from datetime import datetime
from pathlib import Path
from calendar import monthrange

try:
    import requests
except ImportError:
    print("Missing dependency: run  pip3 install requests --break-system-packages  and try again.")
    sys.exit(1)

MONTH_NAMES = [
    'January','February','March','April','May','June',
    'July','August','September','October','November','December'
]

# ── Load credentials from config.json ────────────────────────────────────────

def _load_config(explicit_path: str = None) -> dict:
    script_dir = Path(__file__).parent.resolve()
    candidates = [
        Path(explicit_path) if explicit_path else None,
        Path.cwd() / 'config.json',
        script_dir.parent / 'config.json',
        script_dir / 'config.json',
        Path.home() / 'Documents' / 'Claude' / 'Projects' / 'All Brands Ad Dashboard' / 'deploy' / 'config.json',
    ]
    for p in candidates:
        if p and p.exists():
            print(f"  Using config: {p}")
            with open(p) as f:
                return json.load(f).get('skinuva_google_ads', {})
    print("✗ config.json not found. Pass the path explicitly:")
    print("  python3 skinuva/backfill_google_ads_monthly.py --config /path/to/config.json")
    sys.exit(1)

# Config loaded in main() after args are parsed — see below
DEVELOPER_TOKEN = None
CLIENT_ID = CLIENT_SECRET = REFRESH_TOKEN = CUSTOMER_ID = MANAGER_ID = None
CLIENT_ID       = cfg['client_id']
CLIENT_SECRET   = cfg['client_secret']
REFRESH_TOKEN   = cfg['refresh_token']
CUSTOMER_ID     = cfg['customer_id']
MANAGER_ID      = cfg['manager_id']
API_VERSION     = "v23"
API_BASE        = f"https://googleads.googleapis.com/{API_VERSION}"


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_access_token() -> str:
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


def api_headers(token: str) -> dict:
    return {
        "Authorization":     f"Bearer {token}",
        "developer-token":   DEVELOPER_TOKEN,
        "login-customer-id": MANAGER_ID,
        "Content-Type":      "application/json",
    }


def fetch_all_pages(token: str, query: str) -> list:
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

def micros(v) -> float:
    return round(int(v or 0) / 1_000_000, 2)

def month_key(date_str: str) -> str:
    """'2024-07-01' or '2024-07-01T00:00:00' → 'July 2024'"""
    dt = datetime.strptime(date_str[:10], '%Y-%m-%d')
    return f"{MONTH_NAMES[dt.month - 1]} {dt.year}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start",  default="2024-07-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end",    default="2026-04-30", help="End date YYYY-MM-DD")
    parser.add_argument("--config", default=None, help="Path to config.json")
    args = parser.parse_args()

    # Load credentials now that we have args
    global DEVELOPER_TOKEN, CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, CUSTOMER_ID, MANAGER_ID
    cfg = _load_config(args.config)
    DEVELOPER_TOKEN = cfg['developer_token']
    CLIENT_ID       = cfg['client_id']
    CLIENT_SECRET   = cfg['client_secret']
    REFRESH_TOKEN   = cfg['refresh_token']
    CUSTOMER_ID     = cfg['customer_id']
    MANAGER_ID      = cfg['manager_id']

    print(f"\n{'═'*60}")
    print(f"  Google Ads Monthly Backfill  |  Skinuva")
    print(f"  Range: {args.start} → {args.end}")
    print(f"{'═'*60}\n")

    token = get_access_token()

    # segments.month gives the first day of each month
    query = f"""
        SELECT
          segments.month,
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions,
          metrics.conversions_value
        FROM campaign
        WHERE segments.date BETWEEN '{args.start}' AND '{args.end}'
          AND metrics.impressions > 0
        ORDER BY segments.month ASC
    """

    print("Querying Google Ads by month...")
    rows = fetch_all_pages(token, query)
    print(f"  ✓ {len(rows)} campaign-month rows returned")

    # Aggregate by month
    by_month = {}
    for r in rows:
        seg = r.get("segments", {})
        met = r.get("metrics", {})
        mk = month_key(seg.get("month", ""))
        if not mk:
            continue
        if mk not in by_month:
            by_month[mk] = {"spend": 0.0, "adSales": 0.0, "conversions": 0.0,
                            "clicks": 0, "impressions": 0}
        by_month[mk]["spend"]       += micros(met.get("costMicros", 0))
        by_month[mk]["adSales"]     += float(met.get("conversionsValue", 0) or 0)
        by_month[mk]["conversions"] += float(met.get("conversions", 0) or 0)
        by_month[mk]["clicks"]      += int(met.get("clicks", 0) or 0)
        by_month[mk]["impressions"] += int(met.get("impressions", 0) or 0)

    # Round values
    for mk, d in by_month.items():
        d["spend"]       = round(d["spend"], 2)
        d["adSales"]     = round(d["adSales"], 2)
        d["conversions"] = round(d["conversions"])
        print(f"  {mk}: spend=${d['spend']:,.0f}, sales=${d['adSales']:,.0f}, "
              f"conv={d['conversions']}, clicks={d['clicks']:,}")

    if not by_month:
        print("\n⚠  No Google Ads data found for this date range.")
        sys.exit(0)

    # Load and merge into skinuva_monthly.json
    script_dir   = Path(__file__).parent.resolve()
    monthly_path = script_dir / "data" / "skinuva_monthly.json"

    existing = {}
    if monthly_path.exists():
        try:
            existing = json.loads(monthly_path.read_text())
        except Exception as e:
            print(f"  ⚠  Could not read skinuva_monthly.json: {e}")

    merged = 0
    for mk, gdata in by_month.items():
        if mk not in existing:
            # Create a shell entry for this month (Amazon zeros will be filled by H10)
            existing[mk] = {
                "amazon": {"spend": 0.0, "adSales": 0.0, "totalSales": 0.0,
                           "orders": 0, "clicks": 0, "impressions": 0},
                "google": {"spend": 0.0, "adSales": 0.0, "conversions": 0,
                           "clicks": 0, "impressions": 0},
                "shopify": 0.0, "walmart": 0.0,
                "combinedTotalSales": None,
                "fetched_at": "backfill",
            }
        existing[mk]["google"] = {
            "spend":       gdata["spend"],
            "adSales":     gdata["adSales"],
            "conversions": int(gdata["conversions"]),
            "clicks":      gdata["clicks"],
            "impressions": gdata["impressions"],
        }
        merged += 1

    # Sort chronologically
    def sort_key(mk):
        try:
            return datetime.strptime(mk, "%B %Y")
        except Exception:
            return datetime.min

    sorted_merged = dict(sorted(existing.items(), key=lambda x: sort_key(x[0])))
    monthly_path.write_text(json.dumps(sorted_merged, indent=2))

    print(f"\n✓ Merged Google data for {merged} months into {monthly_path}")
    print("\nNext steps:")
    print("  cd ~/Documents/Claude/Projects/All\\ Brands\\ Ad\\ Dashboard/deploy")
    print("  rm -f .git/*.lock")
    print("  git add skinuva/data/skinuva_monthly.json")
    print('  git commit -m "chore: backfill Skinuva Google Ads historical monthly data"')
    print("  git pull --rebase && git push origin main")


if __name__ == "__main__":
    main()
