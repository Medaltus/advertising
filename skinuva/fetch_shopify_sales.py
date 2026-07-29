#!/usr/bin/env python3
"""
fetch_shopify_sales.py
Pulls Skinuva's Shopify sales per calendar month and writes
data/shopify_sales.json, replacing the manual
`./update-skinuva-sales.sh shopify <amount>` step.

WHAT IT REPRODUCES
------------------
The "Total sales by sales channel" report with sales channel "is not"
Draft Orders. Rather than approximating that with the Orders API, this runs
the identical ShopifyQL query through the Admin API's shopifyqlQuery:

    FROM sales SHOW total_sales GROUP BY sales_channel SINCE <start> UNTIL <end>

then sums every channel except the excluded ones. Verified to the cent
against the hand-entered values on two months:

    July 2026 (01-28): Online Store 41,898.94 + Seal Subscriptions 3,479.70
                       + Shop 879.56 + TikTok 541.31          = 46,799.51  ✓
    June 2026 (01-30): Online Store 37,776.20 + Seal Subscriptions 1,967.39
                       + Shop 940.87 + TikTok 172.28          = 40,856.74  ✓

Draft Orders runs ~$200K/month on this store, so including it would
overstate the figure roughly 5x. That exclusion is the whole ballgame.

Usage:
  python3 skinuva/fetch_shopify_sales.py
  python3 skinuva/fetch_shopify_sales.py --months 3
  python3 skinuva/fetch_shopify_sales.py --dry-run   # print, don't write

Config (config.json, from the CONFIGJSON GitHub secret):
  shopify_store_domain      e.g. "http-skinuva-com.myshopify.com"  (required)
  shopify_client_id         Dev Dashboard app Client ID           (required*)
  shopify_client_secret     Dev Dashboard app Client secret       (required*)
  shopify_access_token      * legacy alternative: a static token, used
                              directly if present instead of the exchange
  shopify_excluded_channels optional, defaults to ["Draft Orders"]
  shopify_api_version       optional; auto-discovered if omitted

AUTH: uses the client credentials grant. Shopify stopped allowing new legacy
custom apps on 2026-01-01, so there's no static Admin API token to copy any
more. Instead the app lives in the Dev Dashboard and we exchange client
id/secret for a 24h token at the start of every run. That's a better fit for
a cron job anyway — nothing long-lived to leak or rotate by hand.

Requires: the app installed on the store, with scope read_reports, and the
app and store in the SAME Shopify organization (otherwise Shopify returns
shop_not_permitted).
"""

import argparse
import calendar
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing dependency: pip install requests --break-system-packages")
    sys.exit(1)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

MONTH_NAMES = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
]

DEFAULT_EXCLUDED_CHANNELS = ['Draft Orders']
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


def get_access_token(domain, client_id, client_secret):
    """
    Client credentials grant — exchange app credentials for a 24h access token.
    POST https://{shop}.myshopify.com/admin/oauth/access_token
         grant_type=client_credentials&client_id=..&client_secret=..
    """
    r = requests.post(
        f"https://{domain}/admin/oauth/access_token",
        data={'grant_type': 'client_credentials',
              'client_id': client_id,
              'client_secret': client_secret},
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=30,
    )
    if r.status_code != 200:
        detail = r.text[:300]
        hint = ''
        if 'shop_not_permitted' in detail:
            hint = ("\n      -> The app and the store must be in the SAME Shopify "
                    "organization. In the Dev Dashboard, check the store appears "
                    "under this org.")
        raise RuntimeError(f"token exchange failed: HTTP {r.status_code}: {detail}{hint}")
    body = r.json()
    tok = body.get('access_token')
    if not tok:
        raise RuntimeError(f"token exchange returned no access_token: {body}")
    print(f"  Token acquired (scopes: {body.get('scope')}, "
          f"expires in {body.get('expires_in')}s)")
    if 'read_reports' not in (body.get('scope') or ''):
        print("  ⚠  'read_reports' is NOT in the granted scopes — the analytics "
              "query will fail. Add it to the app's scopes and re-release/reinstall.")
    return tok


def gql(domain, token, version, query, variables=None):
    r = requests.post(
        f"https://{domain}/admin/api/{version}/graphql.json",
        json={'query': query, 'variables': variables or {}},
        headers={'X-Shopify-Access-Token': token, 'Content-Type': 'application/json'},
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    body = r.json()
    if body.get('errors'):
        raise RuntimeError(f"GraphQL errors: {json.dumps(body['errors'])[:400]}")
    return body.get('data') or {}


def discover_api_version(domain, token):
    try:
        d = gql(domain, token, FALLBACK_API_VERSION,
                '{ publicApiVersions { handle supported } }')
        stable = sorted(v['handle'] for v in (d.get('publicApiVersions') or [])
                        if v.get('supported') and v['handle'][:4].isdigit())
        if stable:
            return stable[-1]
    except Exception as e:
        print(f"  ⚠  API version discovery failed ({e}); using {FALLBACK_API_VERSION}")
    return FALLBACK_API_VERSION


def shop_today(domain, token, version):
    """
    'Today' in the SHOP's timezone, not the runner's.

    ShopifyQL SINCE/UNTIL are interpreted in shop-local time. CI runs in UTC,
    and this store is US/Pacific — so during the early-UTC cron the UTC date
    is already tomorrow relative to the shop, which would make us ask for a
    day that hasn't finished and produce a number that changes on the next run.
    """
    try:
        d = gql(domain, token, version, '{ shop { ianaTimezone } }')
        tz = ((d.get('shop') or {}).get('ianaTimezone'))
        if tz and ZoneInfo:
            return datetime.now(ZoneInfo(tz)).date(), tz
    except Exception as e:
        print(f"  ⚠  Could not read shop timezone ({e}); falling back to UTC")
    return datetime.now(timezone.utc).date(), 'UTC'


SHOPIFYQL_QUERY = """
query($q: String!) {
  shopifyqlQuery(query: $q) {
    parseErrors
    tableData { columns { name dataType } rows }
  }
}
"""


def rows_to_records(table):
    """
    Normalise shopifyqlQuery rows into a list of {column_name: value} dicts.

    tableData.rows is typed JSON! and the Admin API returns a list of OBJECTS
    keyed by column name:
        [{"sales_channel": "Online Store", "total_sales": "41350.19"}, ...]
    The Shopify MCP wrapper reshapes the same data into positional arrays:
        [["Online Store", "41350.19"], ...]
    The first version of this script only handled the positional form and did
    `continue` on anything else — so against the real API it silently skipped
    every row and confidently wrote $0.00 into the dashboard. Handle both, and
    let the caller treat "no records" as a failure rather than a real zero.
    """
    cols = [c.get('name') for c in (table.get('columns') or [])]
    records = []
    for row in (table.get('rows') or []):
        if isinstance(row, dict):
            records.append(row)
        elif isinstance(row, (list, tuple)):
            records.append({cols[i]: row[i]
                            for i in range(min(len(cols), len(row)))})
        else:
            print(f"  ⚠  Unrecognised row shape {type(row).__name__}: {row!r:.80}")
    return records


def fetch_month(domain, token, version, year, month, excluded, upto=None):
    """
    Run the report for one calendar month.
    Returns (total_excluding, per_channel_dict, excluded_total, start, end).
    Raises on an empty/unparseable result so the caller keeps the prior value.
    """
    last = calendar.monthrange(year, month)[1]
    start = date(year, month, 1)
    end = min(date(year, month, last), upto) if upto else date(year, month, last)
    if end < start:
        return None, None, None

    q = (f"FROM sales SHOW total_sales GROUP BY sales_channel "
         f"SINCE {start.isoformat()} UNTIL {end.isoformat()}")
    d = gql(domain, token, version, SHOPIFYQL_QUERY, {'q': q})
    resp = d.get('shopifyqlQuery') or {}
    if resp.get('parseErrors'):
        raise RuntimeError(f"ShopifyQL parse errors: {resp['parseErrors']}")
    table = resp.get('tableData') or {}
    records = rows_to_records(table)

    if not records:
        # Never fall through to 0.00 here. An empty result means the query,
        # the scopes, or the response shape is wrong -- not that the store sold
        # nothing. Writing 0 silently wiped ~$47K of Shopify revenue off the
        # dashboard once already.
        raise RuntimeError(
            f"query returned no usable rows for {start}..{end} "
            f"(raw rows: {str(table.get('rows'))[:200]}) — refusing to record $0")

    per_channel, kept, dropped = {}, 0.0, 0.0
    skipped = 0
    for rec in records:
        channel = rec.get('sales_channel')
        raw = rec.get('total_sales')
        if channel is None or raw is None:
            skipped += 1
            continue
        try:
            amount = float(raw)
        except (TypeError, ValueError):
            skipped += 1
            continue
        per_channel[channel] = round(amount, 2)
        if channel in excluded:
            dropped += amount
        else:
            kept += amount

    if skipped:
        print(f"  ⚠  {skipped} of {len(records)} rows unparseable for {start}..{end}")
    if not per_channel:
        raise RuntimeError(
            f"no channel rows parsed for {start}..{end} — refusing to record $0")

    return round(kept, 2), per_channel, round(dropped, 2), start, end


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--months', type=int, default=2,
                    help='months back to refresh (default 2, catches late refunds)')
    ap.add_argument('--dry-run', action='store_true', help='print without writing')
    args = ap.parse_args()

    cfg, cfg_path = load_config()
    if not cfg:
        print("✗ config.json not found — cannot fetch Shopify sales")
        sys.exit(1)
    domain = cfg.get('shopify_store_domain')
    if domain and not domain.endswith('.myshopify.com'):
        domain = f"{domain}.myshopify.com"
    client_id = cfg.get('shopify_client_id')
    client_secret = cfg.get('shopify_client_secret')
    static_token = cfg.get('shopify_access_token')

    if not domain or not (static_token or (client_id and client_secret)):
        print("✗ Shopify credentials missing from config.json")
        print("  Need shopify_store_domain plus EITHER")
        print("    shopify_client_id + shopify_client_secret  (Dev Dashboard app), or")
        print("    shopify_access_token                       (legacy static token)")
        print("  See SHOPIFY_SETUP.md.")
        sys.exit(1)

    excluded = cfg.get('shopify_excluded_channels') or DEFAULT_EXCLUDED_CHANNELS
    excluded = set(excluded)

    print(f"  Config: {cfg_path}")
    print(f"  Store:  {domain}")
    print(f"  Excluding channels: {sorted(excluded)}")

    if static_token:
        print("  Auth: static access token from config")
        token = static_token
    else:
        print("  Auth: client credentials grant")
        try:
            token = get_access_token(domain, client_id, client_secret)
        except Exception as e:
            print(f"✗ {e}")
            print("  Not writing shopify_sales.json — previous values are preserved,")
            print("  and generate_skinuva_supplement.py falls back to manual_totals.json.")
            sys.exit(1)

    version = cfg.get('shopify_api_version') or discover_api_version(domain, token)
    print(f"  API version: {version}")

    today, tzname = shop_today(domain, token, version)
    yesterday = today - timedelta(days=1)
    print(f"  Shop-local today: {today} ({tzname})")

    out_dir = Path(__file__).parent / 'data'
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / 'shopify_sales.json'

    existing = {}
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
        except Exception:
            pass

    manual = {}
    mt = out_dir / 'manual_totals.json'
    if mt.exists():
        try:
            manual = json.loads(mt.read_text())
        except Exception:
            pass

    results = dict(existing)
    failures = 0

    for i in range(args.months):
        y, m = today.year, today.month - i
        while m <= 0:
            m += 12
            y -= 1
        mk = f"{MONTH_NAMES[m-1]} {y}"
        is_current = (y == today.year and m == today.month)
        upto = yesterday if is_current else None

        try:
            total, per_channel, dropped, start, end = fetch_month(
                domain, token, version, y, m, excluded, upto)
        except Exception as e:
            prev = (existing.get(mk) or {}).get('total')
            print(f"  ⚠  [{mk}] fetch failed: {e}")
            print(f"      keeping previous value: "
                  f"{('$%,.2f' % prev) if prev is not None else 'none'}")
            failures += 1
            continue

        if total is None:
            continue

        results[mk] = {
            'total': total,
            'byChannel': per_channel,
            'excludedChannels': sorted(excluded),
            'excludedTotal': dropped,
            'range': {'since': start.isoformat(), 'until': end.isoformat()},
            'fetched_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'source': 'shopifyql-total_sales-by-sales_channel',
        }

        print(f"  ✓ [{mk}] ${total:,.2f}   ({start} → {end})")
        for ch, amt in sorted(per_channel.items(), key=lambda x: -x[1]):
            mark = '  EXCLUDED' if ch in excluded else ''
            print(f"        {ch:22} ${amt:>12,.2f}{mark}")

        prev_manual = (manual.get(mk) or {}).get('shopify')
        if prev_manual is not None:
            diff = total - float(prev_manual)
            verdict = 'exact match' if abs(diff) < 0.01 else f'differs by ${diff:+,.2f}'
            print(f"        vs manual ${float(prev_manual):,.2f} — {verdict}")

    if failures and not results:
        print("✗ All months failed — not writing shopify_sales.json")
        sys.exit(1)

    if args.dry_run:
        print("  (--dry-run: nothing written)")
        return

    out_path.write_text(json.dumps(dict(sorted(
        results.items(), key=lambda x: datetime.strptime(x[0], '%B %Y'))), indent=2))
    print(f"✓ Wrote {out_path} ({len(results)} months)")


if __name__ == '__main__':
    main()
