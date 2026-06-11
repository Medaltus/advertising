#!/usr/bin/env python3
"""
generate_supplement.py
Converts ads_data.js (output of fetch_ads_data.py) into api_supplement.json,
which the deployed Vercel dashboard reads from /data/api_supplement.json.

Run this immediately AFTER fetch_ads_data.py:
    python3 fetch_ads_data.py
    python3 deploy/generate_supplement.py

The script:
  - Reads ads_data.js from the parent directory (same folder as fetch_ads_data.py)
  - Groups daily timeline data into monthly buckets per brand
  - Packages campaign data under the most recent month in the window
  - Writes deploy/data/api_supplement.json

Then commit and push — Vercel auto-deploys.
"""

import calendar
import json
import re
import sys
from pathlib import Path
from collections import defaultdict
from datetime import datetime, date, timedelta

MONTH_NAMES = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
]


def month_key(date_str: str) -> str:
    """'2026-05-15' → 'May 2026'"""
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    return f"{MONTH_NAMES[dt.month - 1]} {dt.year}"


def safe_div(a, b):
    return round(a / b, 4) if b else None


def acos_pct(spend, sales):
    if not sales or sales == 0:
        return None
    return round((spend / sales) * 100, 1)


def parse_ads_js(path: Path) -> dict:
    content = path.read_text()
    # Strip the JS variable wrapper
    content = re.sub(r'^/\*.*?\*/\s*const ADS_DATA\s*=\s*', '', content, flags=re.DOTALL)
    content = content.rstrip(';\n').strip()
    return json.loads(content)


def build_supplement(data: dict) -> dict:
    supplement = {}

    for brand in data.get('brands', []):
        name      = brand['name']
        timeline  = brand.get('timeline', [])
        campaigns = brand.get('campaigns', [])

        # ── Aggregate timeline into monthly buckets ─────────────────────────
        # NOTE: We do NOT aggregate the Amazon Ads 'totalSales' field.
        # That field is a rolling attribution-window metric, not a true daily
        # value — summing it across days inflates the number massively.
        # Real Total Sales (from SP-API via Easy-Insight) lives in the Google
        # Sheet and is preserved by the dashboard's merge logic.
        monthly: dict = defaultdict(lambda: {
            'spend': 0.0, 'sales': 0.0,
            'impressions': 0, 'clicks': 0, 'purchases': 0,
        })

        for row in timeline:
            mk = month_key(row['date'])
            m  = monthly[mk]
            m['spend']       += float(row.get('spend', 0) or 0)
            m['sales']       += float(row.get('sales', 0) or 0)
            m['impressions'] += int(row.get('impressions', 0) or 0)
            m['clicks']      += int(row.get('clicks', 0) or 0)
            m['purchases']   += int(row.get('purchases', 0) or 0)

        if not monthly:
            continue

        # Determine the latest month so we can attach campaigns to it
        sorted_months = sorted(monthly.keys(),
                               key=lambda x: datetime.strptime(x, '%B %Y'))
        latest_month  = sorted_months[-1]

        for mk, m in monthly.items():
            spend     = round(m['spend'], 2)
            sales     = round(m['sales'], 2)
            impr      = m['impressions']
            clicks    = m['clicks']
            purchases = m['purchases']

            # Campaign data is 30-day rolling — attach to latest month only
            brand_campaigns = []
            if mk == latest_month:
                for c in campaigns:
                    cspend = round(float(c.get('spend', 0) or 0), 2)
                    csales = round(float(c.get('sales', 0) or 0), 2)
                    brand_campaigns.append({
                        'name':        c.get('name', ''),
                        'source':      'AMAZON',
                        'spend':       cspend,
                        'adSales':     csales,
                        'acos':        acos_pct(cspend, csales),
                        'impressions': int(c.get('impressions', 0) or 0),
                        'clicks':      int(c.get('clicks', 0) or 0),
                        'orders':      int(c.get('purchases', 0)) if c.get('purchases') else None,
                    })

            if mk not in supplement:
                supplement[mk] = {'brands': []}

            supplement[mk]['brands'].append({
                'name':        name,
                'spend':       spend,
                'adSales':     sales,
                'acos':        acos_pct(spend, sales),
                'totalSales':  None,
                'tacos':       None,
                'impressions': impr,
                'clicks':      clicks,
                'ctr':         safe_div(clicks * 100, impr),
                'cpc':         safe_div(spend, clicks),
                'cr':          None,
                'orders':      purchases if purchases else None,
                'aov':         round(sales / purchases, 2) if purchases and sales else None,
                'pctFromAds':  None,
                'salesVolume': None,
                'momAcos':     None,
                'momTacos':    None,
                'campaigns':   brand_campaigns,
            })

    # ── Compute month-level portfolio totals ────────────────────────────────
    for mk, entry in supplement.items():
        brands = entry['brands']
        t_spend  = round(sum(b['spend'] or 0 for b in brands), 2)
        t_sales  = round(sum(b['adSales'] or 0 for b in brands), 2)
        t_impr   = sum(b['impressions'] or 0 for b in brands)
        t_clicks = sum(b['clicks'] or 0 for b in brands)
        t_orders = sum(b['orders'] or 0 for b in brands)

        entry.update({
            'spend':       t_spend,
            'adSales':     t_sales,
            'acos':        acos_pct(t_spend, t_sales),
            'totalSales':  None,
            'tacos':       None,
            'impressions': t_impr,
            'clicks':      t_clicks,
            'ctr':         safe_div(t_clicks * 100, t_impr),
            'cpc':         safe_div(t_spend, t_clicks),
            'cr':          None,
            'orders':      t_orders if t_orders else None,
            'aov':         None,
        })
        # Sort brands by spend descending
        entry['brands'].sort(key=lambda b: b['spend'] or 0, reverse=True)

    # ── Sort months chronologically ─────────────────────────────────────────
    return dict(sorted(
        supplement.items(),
        key=lambda x: datetime.strptime(x[0], '%B %Y')
    ))


def enrich_with_sp_sales(supplement: dict, cfg: dict) -> None:
    """
    Populate totalSales and tacos in the supplement using SP-API data.
    Calls SP-API once per calendar month that appears in the supplement.
    """
    # Support both local dev (script in deploy/ subdir) and CI (script at repo root)
    for p in [str(Path(__file__).parent.parent), str(Path(__file__).parent)]:
        if p not in sys.path:
            sys.path.insert(0, p)
    from fetch_total_sales import fetch_brand_sales_for_period  # noqa

    # Months before this date use the Google Sheet for totalSales.
    # June 2026 and later always use SP-API.
    API_ERA_START = (2026, 6)

    today = date.today()

    for mk, entry in supplement.items():
        month_dt  = datetime.strptime(mk, '%B %Y')
        if (month_dt.year, month_dt.month) < API_ERA_START:
            # Pre-API era: totalSales comes from the Google Sheet.
            continue

        first_day = date(month_dt.year, month_dt.month, 1)

        if month_dt.year == today.year and month_dt.month == today.month:
            last_day = today - timedelta(days=1)
        else:
            _, last_num = calendar.monthrange(month_dt.year, month_dt.month)
            last_day = date(month_dt.year, month_dt.month, last_num)

        start_str = first_day.isoformat()
        end_str   = last_day.isoformat()

        print(f"\n  [{mk}] Fetching SP-API sales {start_str} → {end_str}…")
        try:
            brand_sales = fetch_brand_sales_for_period(cfg, start_str, end_str)
        except Exception as exc:
            print(f"  ⚠  SP-API failed for {mk}: {exc} — totalSales will be None")
            continue

        # Brand-level totalSales + tacos
        for b in entry['brands']:
            ts = brand_sales.get(b['name'])
            b['totalSales'] = ts
            b['tacos']      = acos_pct(b['spend'], ts) if ts else None

        # Portfolio-level totalSales = sum of brand totals; tacos = spend / totalSales
        t_total = sum(b['totalSales'] or 0 for b in entry['brands'])
        entry['totalSales'] = round(t_total, 2) if t_total else None
        entry['tacos']      = acos_pct(entry['spend'], entry['totalSales'])


def main():
    script_dir = Path(__file__).parent.resolve()
    # ads_data.js lives next to fetch_ads_data.py, one level above deploy/
    ads_js  = script_dir.parent / 'ads_data.js'
    if not ads_js.exists():
        ads_js = script_dir / 'ads_data.js'  # CI fallback: repo root = deploy/
    out_dir = script_dir / 'data'
    out_json = out_dir / 'api_supplement.json'

    if not ads_js.exists():
        print(f"✗ ads_data.js not found at {ads_js}")
        print("  Run fetch_ads_data.py first.")
        sys.exit(1)

    print(f"Reading {ads_js} ...")
    data = parse_ads_js(ads_js)
    fetched_at = data.get('fetched_at', 'unknown')
    print(f"  Fetched at: {fetched_at}  |  {len(data.get('brands', []))} brands")

    supplement = build_supplement(data)

    # Enrich with SP-API total sales per calendar month
    cfg_path = script_dir.parent / 'config.json'
    if not cfg_path.exists():
        cfg_path = script_dir / 'config.json'  # CI fallback: repo root = deploy/
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
            print("\nEnriching with SP-API total sales…")
            enrich_with_sp_sales(supplement, cfg)
        except Exception as exc:
            print(f"\n⚠  SP-API enrichment failed: {exc}")
            print("   totalSales will be None — check config.json and SP-API credentials")
    else:
        print(f"\n⚠  config.json not found at {cfg_path} — skipping SP-API enrichment")

    out_dir.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(supplement, indent=2))

    print(f"\n✓ Wrote {out_json}")
    for mk, entry in supplement.items():
        partial = ' (partial)' if len(entry.get('brands', [])) else ''
        print(f"  {mk}: {len(entry['brands'])} brands | "
              f"spend=${entry['spend']:,.0f} | "
              f"adSales=${entry['adSales']:,.0f} | "
              f"acos={entry['acos']}%{partial}")

    print("\nNext steps:")
    print("  git add deploy/data/api_supplement.json")
    print("  git commit -m 'chore: refresh ad supplement data'")
    print("  git push  →  Vercel auto-deploys")


if __name__ == '__main__':
    main()
