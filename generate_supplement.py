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


def build_daily_archive(data: dict) -> dict:
    """
    Store per-brand per-day ad rows (+ per-campaign breakdown) for date-range queries.
    Structure: {"YYYY-MM-DD": {"brands": {"BrandName": {spend, adSales, orders, clicks,
                impressions, campaigns: {"CampaignName": {spend, adSales, orders, ...}}}}}}
    NOTE: totalSales is NOT included — it's a monthly SP-API metric, not a true daily value.
    Merged into medaltus_daily_archive.json — new data wins, old days preserved.
    """
    entries: dict = {}

    for brand in data.get('brands', []):
        name            = brand.get('name', '')
        timeline        = brand.get('timeline', [])
        camp_timeline   = brand.get('campaign_timeline', [])

        # Build per-date campaign lookup from campaign_timeline
        camp_by_date: dict = {}
        for ct in camp_timeline:
            d = ct.get('date')
            if not d:
                continue
            camp_by_date[d] = {
                c['name']: {
                    'spend':       round(float(c.get('spend', 0) or 0), 2),
                    'adSales':     round(float(c.get('sales', 0) or 0), 2),
                    'orders':      int(c.get('purchases', 0) or 0),
                    'clicks':      int(c.get('clicks', 0) or 0),
                    'impressions': int(c.get('impressions', 0) or 0),
                }
                for c in ct.get('campaigns', [])
                if c.get('name')
            }

        for row in timeline:
            d = row.get('date')
            if not d:
                continue
            if d not in entries:
                entries[d] = {'brands': {}}
            brand_entry = {
                'spend':       round(float(row.get('spend', 0) or 0), 2),
                'adSales':     round(float(row.get('sales', 0) or 0), 2),
                'orders':      int(row.get('purchases', 0) or 0),
                'clicks':      int(row.get('clicks', 0) or 0),
                'impressions': int(row.get('impressions', 0) or 0),
            }
            if d in camp_by_date:
                brand_entry['campaigns'] = camp_by_date[d]
            entries[d]['brands'][name] = brand_entry

    return entries


def build_search_term_insights(search_terms: list) -> dict:
    """
    Categorize a brand's search terms into 3 buckets, top 10 each.
    - top_performing:  ACOS < 40%, spend >= $5, sales > 0  → sorted by sales desc
    - wasted_spend:    spend >= $5, sales == 0 or ACOS > 100% → sorted by spend desc
    - opportunities:   CVR > 3%, spend < $30, sales > 0    → sorted by cvr desc
    """
    top, wasted, opps = [], [], []

    for t in search_terms:
        spend = t.get('spend', 0) or 0
        sales = t.get('sales', 0) or 0
        acos  = t.get('acos')
        cvr   = t.get('cvr') or 0

        if spend >= 5 and sales > 0 and acos is not None and acos <= 40:
            top.append(t)
        if spend >= 5 and (sales == 0 or (acos is not None and acos > 100)):
            wasted.append(t)
        if cvr > 0.03 and spend < 30 and sales > 0:
            opps.append(t)

    top.sort(key=lambda x: x.get('sales', 0) or 0, reverse=True)
    wasted.sort(key=lambda x: x.get('spend', 0) or 0, reverse=True)
    opps.sort(key=lambda x: x.get('cvr', 0) or 0, reverse=True)

    # CPC changes: scan ALL search terms for biggest CPC increases vs 30-day avg
    cpc_changes = []
    for t in search_terms:
        daily = t.get('daily', [])
        if len(daily) < 2:
            continue
        avg_cpc = t.get('cpc') or 0
        if avg_cpc <= 0:
            continue
        latest_cpc = daily[-1].get('cpc', 0) or 0
        if latest_cpc <= 0:
            continue
        pct_change = (latest_cpc - avg_cpc) / avg_cpc * 100
        if pct_change > 0:
            cpc_changes.append({**t, '_latest_cpc': latest_cpc, '_cpc_change_pct': pct_change})

    cpc_changes.sort(key=lambda x: x.get('_cpc_change_pct', 0), reverse=True)

    def trim(terms):
        keys = ('query', 'spend', 'sales', 'acos', 'impressions',
                'clicks', 'ctr', 'cpc', 'purchases', 'cvr')
        return [
            {**{k: t.get(k) for k in keys}, 'daily': t.get('daily', [])}
            for t in terms[:10]
        ]

    def trim_cpc(terms):
        keys = ('query', 'spend', 'sales', 'acos', 'impressions',
                'clicks', 'ctr', 'cpc', 'purchases', 'cvr')
        return [
            {**{k: t.get(k) for k in keys},
             'daily':          t.get('daily', []),
             'latest_cpc':     round(t.get('_latest_cpc', 0), 4),
             'cpc_change_pct': round(t.get('_cpc_change_pct', 0), 1)}
            for t in terms[:10]
        ]

    return {
        'top_performing': trim(top),
        'wasted_spend':   trim(wasted),
        'opportunities':  trim(opps),
        'cpc_changes':    trim_cpc(cpc_changes),
    }


def build_asin_insights(asins: list) -> dict:
    """Categorize ASIN data into performance buckets, top 10 each."""
    top, wasted, opps = [], [], []

    for a in asins:
        spend = a.get('spend', 0) or 0
        sales = a.get('sales', 0) or 0
        acos  = a.get('acos')
        cvr   = a.get('cvr') or 0

        if spend >= 5 and sales > 0 and acos is not None and acos <= 35:
            top.append(a)
        if spend >= 5 and (sales == 0 or (acos is not None and acos > 100)):
            wasted.append(a)
        if cvr > 0.05 and spend < 50 and sales > 0:
            opps.append(a)

    top.sort(key=lambda x: x.get('sales', 0) or 0, reverse=True)
    wasted.sort(key=lambda x: x.get('spend', 0) or 0, reverse=True)
    opps.sort(key=lambda x: x.get('cvr', 0) or 0, reverse=True)

    def trim(items):
        keys = ('asin', 'sku', 'title', 'impressions', 'clicks', 'spend',
                'sales', 'purchases', 'acos', 'ctr', 'cpc', 'cvr')
        return [{k: a.get(k) for k in keys} for a in items[:10]]

    return {
        'top_performers': trim(top),
        'wasted_spend':   trim(wasted),
        'opportunities':  trim(opps),
        'all':            trim(asins[:50]),
    }


def build_placement_insights(placements: list) -> list:
    """Pass placement data through — already aggregated by fetch_ads_data.py."""
    return placements


def build_supplement(data: dict) -> dict:
    supplement = {}

    for brand in data.get('brands', []):
        name         = brand['name']
        timeline     = brand.get('timeline', [])
        campaigns    = brand.get('campaigns', [])
        search_terms = brand.get('search_terms', [])
        asins        = brand.get('asins', [])
        placements   = brand.get('placements', [])

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
                'name':              name,
                'spend':             spend,
                'adSales':           sales,
                'acos':              acos_pct(spend, sales),
                'totalSales':        None,
                'tacos':             None,
                'impressions':       impr,
                'clicks':            clicks,
                'ctr':               safe_div(clicks * 100, impr),
                'cpc':               safe_div(spend, clicks),
                'cr':                None,
                'orders':            purchases if purchases else None,
                'aov':               round(sales / purchases, 2) if purchases and sales else None,
                'pctFromAds':        None,
                'salesVolume':       None,
                'momAcos':           None,
                'momTacos':          None,
                'campaigns':         brand_campaigns,
                'searchTermInsights': build_search_term_insights(search_terms) if mk == latest_month else None,
                'productInsights':    build_asin_insights(asins) if mk == latest_month else None,
                'placementInsights': build_placement_insights(placements) if mk == latest_month else None,
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


def fetch_daily_brand_sales(cfg: dict, dates: list) -> dict:
    """
    For each date in dates, fetch per-brand total sales via SP-API.
    Returns {date: {brand_name: total_sales_float}}.
    Only call for dates missing totalSales or within the last 3 days.
    """
    for p in [str(Path(__file__).parent.parent), str(Path(__file__).parent)]:
        if p not in sys.path:
            sys.path.insert(0, p)
    from fetch_total_sales import fetch_brand_sales_for_period  # noqa

    result = {}
    total = len(dates)
    for i, date_str in enumerate(sorted(dates), 1):
        print(f"  [{i}/{total}] SP-API brand sales for {date_str}…")
        try:
            brand_sales = fetch_brand_sales_for_period(cfg, date_str, date_str)
            result[date_str] = brand_sales
        except Exception as exc:
            print(f"  ⚠  SP-API failed for {date_str}: {exc}")
    return result


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

    # ── Load config once for all SP-API enrichment ──────────────────────────
    cfg_path = script_dir.parent / 'config.json'
    if not cfg_path.exists():
        cfg_path = script_dir / 'config.json'  # CI fallback: repo root = deploy/
    cfg = None
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
        except Exception as exc:
            print(f"\n⚠  Could not read config.json: {exc}")
    else:
        print(f"\n⚠  config.json not found at {cfg_path} — skipping all SP-API enrichment")

    # Enrich supplement with SP-API total sales per calendar month
    if cfg:
        try:
            print("\nEnriching with SP-API total sales…")
            enrich_with_sp_sales(supplement, cfg)
        except Exception as exc:
            print(f"\n⚠  SP-API enrichment failed: {exc}")
            print("   totalSales will be None — check config.json and SP-API credentials")

    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Merge with existing data to preserve historical months ──────────────
    # Months outside the current lookback window are kept from the existing
    # file untouched; months inside the window are updated with fresh data.
    if out_json.exists():
        try:
            existing = json.loads(out_json.read_text())
            merged = {**existing, **supplement}  # fresh data wins for overlapping months
            supplement = dict(sorted(
                merged.items(),
                key=lambda x: datetime.strptime(x[0], '%B %Y')
            ))
            print(f"\n  Merged with existing file: {len(supplement)} total months retained")
        except Exception as exc:
            print(f"\n⚠  Could not merge with existing data: {exc} — writing fresh supplement")

    # NOTE: supplement is written after the daily archive is built and
    # archive-corrected below — do not write it here.

    # ── Build and merge medaltus_daily_archive.json ──────────────────────────
    daily_path = out_dir / 'medaltus_daily_archive.json'
    new_daily = build_daily_archive(data)

    existing_daily = {}
    if daily_path.exists():
        try:
            existing_daily = json.loads(daily_path.read_text())
        except Exception:
            pass

    # Merge per-day: new data wins for each brand's day row
    for d, entry in new_daily.items():
        if d not in existing_daily:
            existing_daily[d] = {'brands': {}}
        existing_daily[d]['brands'].update(entry['brands'])

    merged_daily = dict(sorted(existing_daily.items()))

    # ── Fetch per-brand daily SP-API total sales ─────────────────────────────
    # Fetch days that are missing totalSales OR within the last 3 days
    # (recent days may not yet be finalized in the SP-API).
    # After the first backfill run, only ~3 days are fetched per daily refresh.
    if cfg:
        try:
            today = date.today()
            lookback = cfg.get('lookback_days', 30)
            cutoff       = (today - timedelta(days=lookback)).isoformat()
            refresh_from = (today - timedelta(days=3)).isoformat()

            dates_to_fetch = [
                d for d in sorted(merged_daily.keys())
                if d >= cutoff and (
                    d >= refresh_from or
                    any('totalSales' not in v
                        for v in merged_daily[d]['brands'].values())
                )
            ]

            if dates_to_fetch:
                print(f"\nFetching SP-API daily brand sales for {len(dates_to_fetch)} days…")
                daily_sp = fetch_daily_brand_sales(cfg, dates_to_fetch)
                for d_str, brand_sales in daily_sp.items():
                    if d_str in merged_daily:
                        for brand, sales in brand_sales.items():
                            if brand in merged_daily[d_str]['brands']:
                                merged_daily[d_str]['brands'][brand]['totalSales'] = \
                                    round(float(sales), 2)
                print(f"✓ SP-API daily brand sales stored for {len(daily_sp)} days")
            else:
                print("\n✓ Daily archive SP-API data is up to date")
        except Exception as exc:
            print(f"\n⚠  Daily SP-API brand sales fetch failed: {exc}")

    daily_path.write_text(json.dumps(merged_daily, indent=2))
    print(f"✓ Updated {daily_path} ({len(merged_daily)} days)")

    # ── Correct supplement totals for completed months from the complete archive ─
    # The fresh ads_data.js covers only the last N days (lookback_days window).
    # When running in a new month (e.g. July), the early days of the prior month
    # (e.g. June 1-5) fall outside the window, causing the supplement to under-
    # count completed months. We re-derive brand and portfolio totals from the
    # complete daily archive, which accumulates every day permanently.
    print("\nCorrecting supplement month totals from complete daily archive…")
    today_month_str = date.today().strftime('%B %Y')

    # Aggregate archive into monthly brand totals (metrics + campaigns)
    archive_monthly: dict = defaultdict(lambda: defaultdict(lambda: {
        'spend': 0.0, 'adSales': 0.0, 'orders': 0, 'clicks': 0, 'impressions': 0,
    }))
    # { month: { brand: { campaign_name: {spend, adSales, orders, clicks, impressions} } } }
    archive_monthly_camps: dict = defaultdict(
        lambda: defaultdict(lambda: defaultdict(lambda: {
            'spend': 0.0, 'adSales': 0.0, 'orders': 0, 'clicks': 0, 'impressions': 0,
        }))
    )
    for d_str, day_entry in merged_daily.items():
        mk = month_key(d_str)
        for brand_name, brand_day in day_entry.get('brands', {}).items():
            b = archive_monthly[mk][brand_name]
            b['spend']       += float(brand_day.get('spend', 0) or 0)
            b['adSales']     += float(brand_day.get('adSales', 0) or 0)
            b['orders']      += int(brand_day.get('orders', 0) or 0)
            b['clicks']      += int(brand_day.get('clicks', 0) or 0)
            b['impressions'] += int(brand_day.get('impressions', 0) or 0)
            for camp_name, camp_data in brand_day.get('campaigns', {}).items():
                c = archive_monthly_camps[mk][brand_name][camp_name]
                c['spend']       += float(camp_data.get('spend', 0) or 0)
                c['adSales']     += float(camp_data.get('adSales', 0) or 0)
                c['orders']      += int(camp_data.get('orders', 0) or 0)
                c['clicks']      += int(camp_data.get('clicks', 0) or 0)
                c['impressions'] += int(camp_data.get('impressions', 0) or 0)

    # Apply archive-derived totals to supplement for all completed months
    for mk, entry in supplement.items():
        if mk == today_month_str:
            continue  # Current partial month: fresh data is the best available
        if mk not in archive_monthly:
            continue  # No archive data for this month — leave as-is
        arch_brands = archive_monthly[mk]
        for b in entry.get('brands', []):
            ab = arch_brands.get(b['name'])
            if not ab:
                continue
            b['spend']       = round(ab['spend'], 2)
            b['adSales']     = round(ab['adSales'], 2)
            b['orders']      = ab['orders'] if ab['orders'] else None
            b['clicks']      = ab['clicks']
            b['impressions'] = ab['impressions']
            b['acos']        = acos_pct(b['spend'], b['adSales'])
            b['ctr']         = safe_div(b['clicks'] * 100, b['impressions'])
            b['cpc']         = safe_div(b['spend'], b['clicks'])
            b['aov']         = (round(b['adSales'] / b['orders'], 2)
                                if b['orders'] and b['adSales'] else None)
            b['tacos']       = acos_pct(b['spend'], b.get('totalSales'))
            # Populate campaigns from daily archive
            arch_camps = archive_monthly_camps.get(mk, {}).get(b['name'], {})
            if arch_camps:
                b['campaigns'] = sorted([
                    {
                        'name':        cname,
                        'source':      'AMAZON',
                        'spend':       round(cd['spend'], 2),
                        'adSales':     round(cd['adSales'], 2),
                        'orders':      cd['orders'] or None,
                        'acos':        acos_pct(cd['spend'], cd['adSales']),
                        'clicks':      cd['clicks'],
                        'impressions': cd['impressions'],
                    }
                    for cname, cd in arch_camps.items()
                    if cd['spend'] > 0
                ], key=lambda c: c['spend'], reverse=True)
        # Recompute portfolio totals from corrected brand data
        brands = entry.get('brands', [])
        t_spend  = round(sum(b['spend'] or 0 for b in brands), 2)
        t_sales  = round(sum(b['adSales'] or 0 for b in brands), 2)
        t_impr   = sum(b['impressions'] or 0 for b in brands)
        t_clicks = sum(b['clicks'] or 0 for b in brands)
        t_orders = sum(b['orders'] or 0 for b in brands)
        entry.update({
            'spend':       t_spend,
            'adSales':     t_sales,
            'acos':        acos_pct(t_spend, t_sales),
            'impressions': t_impr,
            'clicks':      t_clicks,
            'ctr':         safe_div(t_clicks * 100, t_impr),
            'cpc':         safe_div(t_spend, t_clicks),
            'orders':      t_orders if t_orders else None,
            'tacos':       acos_pct(t_spend, entry.get('totalSales')),
        })
        print(f"  [{mk}] Archive-corrected: spend=${t_spend:,.2f}, "
              f"sales=${t_sales:,.2f}, orders={t_orders}")

    out_json.write_text(json.dumps(supplement, indent=2))
    print(f"\n✓ Wrote {out_json}")
    for mk, entry in supplement.items():
        print(f"  {mk}: {len(entry.get('brands', []))} brands | "
              f"spend=${entry['spend']:,.0f} | "
              f"adSales=${entry['adSales']:,.0f} | "
              f"acos={entry['acos']}%")

    print("\nNext steps:")
    print("  git add deploy/data/api_supplement.json deploy/data/medaltus_daily_archive.json")
    print("  git commit -m 'chore: refresh ad supplement data'")
    print("  git push  →  Vercel auto-deploys")


if __name__ == '__main__':
    main()
