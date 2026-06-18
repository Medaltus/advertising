#!/usr/bin/env python3
"""
generate_skinuva_supplement.py
Converts Skinuva ads_data.js → data/skinuva_supplement.json
and maintains data/skinuva_monthly.json for long-term historical archiving.

ads_data.js is produced by:
  python3 fetch_ads_data.py --config skinuva_config.json

Run immediately after fetch_ads_data.py:
  python3 fetch_ads_data.py --config skinuva_config.json
  python3 generate_skinuva_supplement.py

Then commit and push — Vercel auto-deploys.
"""

import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BRAND_NAME = 'Skinuva'
MONTH_NAMES = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
]


def parse_ads_js(path: Path) -> dict:
    content = path.read_text()
    content = re.sub(r'^/\*.*?\*/\s*const ADS_DATA\s*=\s*', '', content, flags=re.DOTALL)
    content = content.rstrip(';\n').strip()
    return json.loads(content)


def extract_brand(data: dict, brand_name: str) -> dict:
    """Extract a single brand from the brands[] array and flatten to supplement format."""
    brands = data.get('brands', [])
    brand = next((b for b in brands if b.get('name', '').lower() == brand_name.lower()), None)
    if not brand:
        if len(brands) == 1:
            brand = brands[0]
        else:
            raise ValueError(f"Brand '{brand_name}' not found in ads_data.js. "
                             f"Available: {[b.get('name') for b in brands]}")

    return {
        'fetched_at': data.get('fetched_at', ''),
        'lookback_days': data.get('lookback_days', 31),
        'currency': data.get('currency', 'USD'),
        'summary': brand.get('summary', {}),
        'timeline': brand.get('timeline', []),
        'campaigns': brand.get('campaigns', []),
        'pacing': brand.get('pacing', []),
    }


def month_key_from_date(date_str: str) -> str:
    """'2026-05-15' → 'May 2026'"""
    dt = datetime.strptime(date_str[:7], '%Y-%m')
    return f"{MONTH_NAMES[dt.month - 1]} {dt.year}"


def build_monthly_archive(supplement: dict, google_path: Path, manual_path: Path) -> dict:
    """
    Aggregate Amazon + Google timeline data by calendar month.
    Returns a dict keyed by month label (e.g. 'June 2026') with per-channel totals.
    This is merged with the existing skinuva_monthly.json so historical months
    outside the current lookback window are never lost.
    """

    # ── Aggregate Amazon timeline by month ──────────────────────────────────
    amz = defaultdict(lambda: {
        'spend': 0.0, 'adSales': 0.0, 'totalSales': 0.0,
        'orders': 0, 'clicks': 0, 'impressions': 0,
    })
    for row in supplement.get('timeline', []):
        if not row.get('date'):
            continue
        mk = month_key_from_date(row['date'])
        amz[mk]['spend']       += float(row.get('spend', 0) or 0)
        amz[mk]['adSales']     += float(row.get('sales', 0) or 0)
        amz[mk]['orders']      += int(row.get('purchases', 0) or 0)
        amz[mk]['clicks']      += int(row.get('clicks', 0) or 0)
        amz[mk]['impressions'] += int(row.get('impressions', 0) or 0)
        ts = float(row.get('totalSales', 0) or 0)
        if ts > 0:
            amz[mk]['totalSales'] += ts

    # ── Aggregate Google timeline by month ───────────────────────────────────
    ggl = defaultdict(lambda: {
        'spend': 0.0, 'adSales': 0.0, 'conversions': 0.0,
        'clicks': 0, 'impressions': 0,
    })
    if google_path.exists():
        try:
            gdata = json.loads(google_path.read_text())
            for row in gdata.get('timeline', []):
                if not row.get('date'):
                    continue
                mk = month_key_from_date(row['date'])
                ggl[mk]['spend']       += float(row.get('spend', 0) or 0)
                ggl[mk]['adSales']     += float(row.get('sales', 0) or 0)
                ggl[mk]['conversions'] += float(row.get('conversions', 0) or 0)
                ggl[mk]['clicks']      += int(row.get('clicks', 0) or 0)
                ggl[mk]['impressions'] += int(row.get('impressions', 0) or 0)
        except Exception as exc:
            print(f"  ⚠  Could not read google_ads_data.json: {exc}")

    # ── Read manual totals (Shopify, Walmart) ────────────────────────────────
    manual = {}
    if manual_path.exists():
        try:
            manual = json.loads(manual_path.read_text())
        except Exception:
            pass

    # ── Build per-month entries ───────────────────────────────────────────────
    entries = {}
    for mk in set(amz.keys()) | set(ggl.keys()):
        a  = amz[mk]
        g  = ggl[mk]
        mt = manual.get(mk, {})
        shopify  = float(mt.get('shopify', 0) or 0)
        walmart  = float(mt.get('walmart', 0) or 0)
        amz_total = a['totalSales']
        combined = round(amz_total + shopify + walmart, 2) if (amz_total or shopify or walmart) else None

        entries[mk] = {
            'amazon': {
                'spend':       round(a['spend'], 2),
                'adSales':     round(a['adSales'], 2),
                'totalSales':  round(a['totalSales'], 2),
                'orders':      a['orders'],
                'clicks':      a['clicks'],
                'impressions': a['impressions'],
            },
            'google': {
                'spend':       round(g['spend'], 2),
                'adSales':     round(g['adSales'], 2),
                'conversions': round(g['conversions']),
                'clicks':      g['clicks'],
                'impressions': g['impressions'],
            },
            'shopify':            shopify,
            'walmart':            walmart,
            'combinedTotalSales': combined,
            'fetched_at':         supplement.get('fetched_at', ''),
        }

    return entries


def main():
    script_dir = Path(__file__).parent.resolve()

    # ads_data.js: same directory (CI) or parent directory (local dev)
    ads_js = script_dir / 'ads_data.js'
    if not ads_js.exists():
        ads_js = script_dir.parent / 'ads_data.js'
    if not ads_js.exists():
        print(f"✗ ads_data.js not found")
        print("  Run:  python3 fetch_ads_data.py --config skinuva_config.json")
        sys.exit(1)

    print(f"Reading {ads_js} ...")
    data = parse_ads_js(ads_js)
    fetched_at = data.get('fetched_at', 'unknown')
    print(f"  Fetched at: {fetched_at}")

    supplement = extract_brand(data, BRAND_NAME)

    timeline = supplement.get('timeline', [])
    spend = round(sum(r.get('spend', 0) or 0 for r in timeline), 2)
    sales = round(sum(r.get('sales', 0) or 0 for r in timeline), 2)
    print(f"  {len(timeline)} timeline rows | spend=${spend:,.0f} | adSales=${sales:,.0f}")

    out_dir = script_dir / 'data'
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Write flat supplement (used for current-month injection) ─────────────
    out_path = out_dir / 'skinuva_supplement.json'
    out_path.write_text(json.dumps(supplement, indent=2))
    print(f"\n✓ Wrote {out_path}")

    # ── Build and merge skinuva_monthly.json (permanent historical archive) ──
    monthly_path = out_dir / 'skinuva_monthly.json'
    google_path  = out_dir / 'google_ads_data.json'
    manual_path  = out_dir / 'manual_totals.json'

    new_entries = build_monthly_archive(supplement, google_path, manual_path)

    # Load existing archive and merge — new data wins for overlapping months,
    # months outside current lookback window are preserved from existing file.
    existing = {}
    if monthly_path.exists():
        try:
            existing = json.loads(monthly_path.read_text())
        except Exception:
            pass

    merged = {**existing, **new_entries}
    merged = dict(sorted(merged.items(), key=lambda x: datetime.strptime(x[0], '%B %Y')))
    monthly_path.write_text(json.dumps(merged, indent=2))
    print(f"✓ Updated {monthly_path} ({len(merged)} months: {', '.join(merged.keys())})")

    print("\nNext steps:")
    print("  git add skinuva/data/skinuva_supplement.json skinuva/data/google_ads_data.json skinuva/data/skinuva_monthly.json")
    print("  git commit -m 'chore: refresh Skinuva ad data'")
    print("  git push  →  Vercel auto-deploys")


if __name__ == '__main__':
    main()
