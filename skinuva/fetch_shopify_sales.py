#!/usr/bin/env python3
"""
fetch_shopify_sales.py
Pulls Skinuva's Shopify sales per calendar month and writes
data/shopify_sales.json, replacing the manual
`./update-skinuva-sales.sh shopify <amount>` step.

Usage:
  python3 skinuva/fetch_shopify_sales.py                 # current + previous month
  python3 skinuva/fetch_shopify_sales.py --months 3
  python3 skinuva/fetch_shopify_sales.py --compare       # print all metric variants

Config (config.json, from the CONFIGJSON GitHub secret):
  shopify_store_domain   e.g. "skinuva.myshopify.com"   (required)
  shopify_access_token   Admin API token, "shpat_..."   (required)
  shopify_api_version    optional; auto-discovered if omitted

Required Shopify scope: read_orders  (add read_all_orders too if you need
orders older than 60 days — Shopify restricts older orders otherwise).

WHY SEVERAL VARIANTS: Shopify's Analytics "Total sales" is
gross - discounts - returns + taxes + shipping + duties, which no single
API field reproduces exactly. Rather than guess, this computes a few
candidate definitions and (with --compare, or whenever a prior manual
value exists) prints them side by side against that manual value so the
right one can be confirmed against real data instead of assumed.
`net_current` is used as the stored value; see PRIMARY_VARIANT.
"""

import argparse
import calendar
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing dependency: pip install requests --break-system-packages")
    sys.exit(1)

MONTH_NAMES = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
]

# Which computed variant gets written to shopify_sales.json.
# Change this once we've confirmed which one matches Shopify Analytics.
PRIMARY_VARIANT = 'net_current'

FALLBACK_API_VERSION = '2026-07'


def load_config():
    here = Path(__file__).parent.resolve()
    for p in [Path.cwd() / 'config.json', here.parent / 'config.json', here / 'config.json']:
        if p.exists():
            try:
                return json.loads(p.read_text()), p
            except Exception:
                continue
    return None, None


def discover_api_version(domain, token):
    """Ask Shopify which versions it supports and take the newest stable one."""
    url = f"https://{domain}/admin/api/{FALLBACK_API_VERSION}/graphql.json"
    q = '{ publicApiVersions { handle supported } }'
    try:
        r = requests.post(url, json={'query': q},
                          headers={'X-Shopify-Access-Token': token,
                                   'Content-Type': 'application/json'}, timeout=30)
        vers = (r.json().get('data') or {}).get('publicApiVersions') or []
        stable = sorted(v['handle'] for v in vers
                        if v.get('supported') and v['handle'][:4].isdigit())
        if stable:
            print(f"  API version: {stable[-1]} (newest supported of {len(stable)})")
            return stable[-1]
    except Exception as e:
        print(f"  ⚠  Version discovery failed ({e}); using {FALLBACK_API_VERSION}")
    return FALLBACK_API_VERSION


ORDERS_QUERY = """
query($q: String!, $cursor: String) {
  orders(first: 250, query: $q, after: $cursor, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      createdAt
      test
      cancelledAt
      currentTotalPriceSet   { shopMoney { amount } }
      totalPriceSet          { shopMoney { amount } }
      currentSubtotalPriceSet{ shopMoney { amount } }
      totalRefundedSet       { shopMoney { amount } }
      totalDiscountsSet      { shopMoney { amount } }
    } }
  }
}
"""


def money(node, field):
    v = (node.get(field) or {}).get('shopMoney') or {}
    try:
        return float(v.get('amount') or 0)
    except (TypeError, ValueError):
        return 0.0


def fetch_month(domain, token, version, year, month, upto=None):
    """
    Sum orders for one calendar month. Returns (variants_dict, order_count).
    Excludes test orders. `upto` caps the range for the in-progress month.
    """
    last = calendar.monthrange(year, month)[1]
    start = date(year, month, 1)
    end = min(date(year, month, last), upto) if upto else date(year, month, last)
    if end < start:
        return None, 0

    # created_at is inclusive on both ends with date-only values
    q = f"created_at:>={start.isoformat()} created_at:<={end.isoformat()}"
    url = f"https://{domain}/admin/api/{version}/graphql.json"
    headers = {'X-Shopify-Access-Token': token, 'Content-Type': 'application/json'}

    v = {'net_current': 0.0, 'gross_original': 0.0, 'subtotal_current': 0.0,
         'refunded': 0.0, 'discounts': 0.0}
    n = 0
    cursor = None

    while True:
        r = requests.post(url, json={'query': ORDERS_QUERY,
                                     'variables': {'q': q, 'cursor': cursor}},
                          headers=headers, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
        if body.get('errors'):
            raise RuntimeError(f"GraphQL errors: {json.dumps(body['errors'])[:400]}")
        conn = (body.get('data') or {}).get('orders') or {}
        for edge in conn.get('edges', []):
            node = edge.get('node') or {}
            if node.get('test'):
                continue
            n += 1
            v['net_current']      += money(node, 'currentTotalPriceSet')
            v['gross_original']   += money(node, 'totalPriceSet')
            v['subtotal_current'] += money(node, 'currentSubtotalPriceSet')
            v['refunded']         += money(node, 'totalRefundedSet')
            v['discounts']        += money(node, 'totalDiscountsSet')

        page = conn.get('pageInfo') or {}
        if page.get('hasNextPage'):
            cursor = page.get('endCursor')
        else:
            break

    return {k: round(x, 2) for k, x in v.items()}, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--months', type=int, default=2,
                    help='how many months back to refresh (default 2, catches late refunds)')
    ap.add_argument('--compare', action='store_true',
                    help='print every variant next to the existing manual value')
    args = ap.parse_args()

    cfg, cfg_path = load_config()
    if not cfg:
        print("✗ config.json not found — cannot fetch Shopify sales")
        sys.exit(1)
    domain = cfg.get('shopify_store_domain')
    token = cfg.get('shopify_access_token')
    if not domain or not token:
        print("✗ shopify_store_domain / shopify_access_token missing from config.json")
        print("  Add both to the CONFIGJSON GitHub secret, then re-run.")
        sys.exit(1)
    print(f"  Using config: {cfg_path}")
    print(f"  Store: {domain}")

    version = cfg.get('shopify_api_version') or discover_api_version(domain, token)

    out_dir = Path(__file__).parent / 'data'
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / 'shopify_sales.json'

    existing = {}
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
        except Exception:
            pass

    # The manual values we're replacing — used purely as a sanity comparison.
    manual = {}
    mt_path = out_dir / 'manual_totals.json'
    if mt_path.exists():
        try:
            manual = json.loads(mt_path.read_text())
        except Exception:
            pass

    today = date.today()
    yesterday = today - timedelta(days=1)

    results = dict(existing)
    failures = 0

    for i in range(args.months):
        y, m = today.year, today.month - i
        while m <= 0:
            m += 12
            y -= 1
        mk = f"{MONTH_NAMES[m-1]} {y}"
        is_current = (y == today.year and m == today.month)
        # Cap the in-progress month at yesterday so a partial today doesn't
        # make the number wobble mid-day.
        upto = yesterday if is_current else None

        try:
            variants, count = fetch_month(domain, token, version, y, m, upto)
        except Exception as e:
            print(f"  ⚠  [{mk}] fetch failed: {e}")
            print(f"      keeping previous value: {existing.get(mk, {}).get('total')}")
            failures += 1
            continue

        if variants is None:
            continue

        total = variants[PRIMARY_VARIANT]
        prev_manual = (manual.get(mk) or {}).get('shopify')

        results[mk] = {
            'total': total,
            'variant': PRIMARY_VARIANT,
            'orders': count,
            'through': (upto or date(y, m, calendar.monthrange(y, m)[1])).isoformat(),
            'fetched_at': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'source': 'shopify-admin-api',
        }

        print(f"  ✓ [{mk}] {PRIMARY_VARIANT} = ${total:,.2f}  ({count} orders, through {results[mk]['through']})")
        if args.compare or prev_manual is not None:
            print(f"      variants for {mk}:")
            for k, val in variants.items():
                flag = ''
                if prev_manual is not None and abs(val - prev_manual) < max(1.0, prev_manual * 0.005):
                    flag = '   <-- matches the manual value'
                print(f"        {k:18} ${val:>12,.2f}{flag}")
            if prev_manual is not None:
                d = total - prev_manual
                print(f"        {'MANUAL (current)':18} ${prev_manual:>12,.2f}")
                print(f"        stored variant differs from manual by ${d:+,.2f} "
                      f"({d/prev_manual*100:+.2f}%)" if prev_manual else "")

    if failures and not results:
        print("✗ All months failed — not writing shopify_sales.json")
        sys.exit(1)

    out_path.write_text(json.dumps(dict(sorted(
        results.items(),
        key=lambda x: datetime.strptime(x[0], '%B %Y'))), indent=2))
    print(f"✓ Wrote {out_path} ({len(results)} months)")


if __name__ == '__main__':
    main()
