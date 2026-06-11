#!/usr/bin/env python3
"""
generate_skinuva_supplement.py
Converts Skinuva ads_data.js → data/skinuva_supplement.json.

ads_data.js is produced by:
  python3 fetch_ads_data.py --config skinuva_config.json

The supplement JSON has the same flat structure used by the dashboard:
  { fetched_at, summary, timeline, campaigns, pacing }

Run immediately after fetch_ads_data.py:
  python3 fetch_ads_data.py --config skinuva_config.json
  python3 generate_skinuva_supplement.py

Then commit and push — Vercel auto-deploys.
"""

import json
import re
import sys
from pathlib import Path

BRAND_NAME = 'Skinuva'


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
        # If only one brand and name is close enough, use it
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
    out_path = out_dir / 'skinuva_supplement.json'
    out_path.write_text(json.dumps(supplement, indent=2))

    print(f"\n✓ Wrote {out_path}")
    print("\nNext steps:")
    print("  git add skinuva/data/skinuva_supplement.json skinuva/data/google_ads_data.json")
    print("  git commit -m 'chore: refresh Skinuva ad data'")
    print("  git push  →  Vercel auto-deploys")


if __name__ == '__main__':
    main()
