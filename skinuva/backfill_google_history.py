#!/usr/bin/env python3
"""
backfill_google_history.py — one-off historical Google Ads backfill for Ozlee.

WHY THIS IS SEPARATE FROM fetch_google_ads.py
---------------------------------------------
fetch_google_ads.py runs every day and is built around a rolling ~31-day window.
Widening that window would change what the "Past 31 Days" views and the campaign
table's ratio scaling operate on — a change with real blast radius on a script that
runs unattended twice a day. This does the historical pull in its own process,
touches only months the daily pipeline does not own, and is safe to re-run.

WHAT IT DOES
------------
Queries the Google Ads API month by month over a historical range, aggregates each
calendar month, and writes the result into skinuva_monthly.json's `google` block.

WHAT IT WILL NOT DO
-------------------
Overwrite a month that already has Google spend. The daily pipeline owns the recent
months; this only fills gaps. Re-running is therefore idempotent — already-filled
months are skipped and reported as such.

USAGE
-----
    python3 skinuva/backfill_google_history.py                  # 2025-01 → last complete month
    python3 skinuva/backfill_google_history.py --from 2025-06   # narrower range
    python3 skinuva/backfill_google_history.py --dry-run        # fetch + report, write nothing
    python3 skinuva/backfill_google_history.py --force          # also overwrite filled months

Requires the same skinuva_google_ads credentials in config.json that
fetch_google_ads.py uses, so it must run where that config exists — i.e. in the
GitHub Actions workflow, not locally.
"""

import argparse
import calendar
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))

# Importing runs fetch_google_ads' module-level config load and constants, but not
# main() — that is guarded by if __name__ == "__main__". So we inherit its auth,
# pagination and error handling rather than duplicating them.
try:
    from fetch_google_ads import get_access_token, fetch_all_pages, micros, CUSTOMER_ID
except SystemExit:
    # _load_gads_config() calls sys.exit when config.json is absent.
    print("✗ Google Ads config not found — this must run where config.json exists "
          "(i.e. in the GitHub Actions workflow, not locally).")
    raise
except Exception as e:                                           # pragma: no cover
    print(f"✗ Could not import fetch_google_ads: {e}")
    sys.exit(1)

MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
               'July', 'August', 'September', 'October', 'November', 'December']

DATA_DIR = Path(__file__).parent / 'data'
MONTHLY_PATH = DATA_DIR / 'skinuva_monthly.json'

SOURCE = 'google-ads-api (historical backfill)'


def month_key(y: int, m: int) -> str:
    return f"{MONTH_NAMES[m - 1]} {y}"


def month_iter(start_y, start_m, end_y, end_m):
    y, m = start_y, start_m
    while (y, m) <= (end_y, end_m):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def last_complete_month(today: date):
    """Never backfill the in-progress month — the daily pipeline owns it."""
    y, m = today.year, today.month - 1
    if m < 1:
        m, y = 12, y - 1
    return y, m


def fetch_month(access_token: str, y: int, m: int) -> dict:
    """
    One month of campaign-level rows, aggregated. Queried per month rather than as
    one long range so a single failure costs one month, not the whole run, and so
    progress is visible in the log.
    """
    start = date(y, m, 1).isoformat()
    end = date(y, m, calendar.monthrange(y, m)[1]).isoformat()
    query = f"""
        SELECT
          metrics.impressions,
          metrics.clicks,
          metrics.cost_micros,
          metrics.conversions,
          metrics.conversions_value,
          segments.date
        FROM campaign
        WHERE segments.date BETWEEN '{start}' AND '{end}'
          AND campaign.status != 'REMOVED'
          AND metrics.impressions > 0
    """
    rows = fetch_all_pages(access_token, query)
    agg = {'spend': 0.0, 'adSales': 0.0, 'conversions': 0.0, 'clicks': 0, 'impressions': 0}
    for r in rows:
        met = r.get('metrics', {})
        agg['impressions'] += int(met.get('impressions', 0) or 0)
        agg['clicks'] += int(met.get('clicks', 0) or 0)
        agg['spend'] += micros(met.get('costMicros', 0))
        agg['conversions'] += float(met.get('conversions', 0) or 0)
        agg['adSales'] += float(met.get('conversionsValue', 0) or 0)
    return {
        'spend': round(agg['spend'], 2),
        'adSales': round(agg['adSales'], 2),
        'conversions': round(agg['conversions']),
        'clicks': agg['clicks'],
        'impressions': agg['impressions'],
        '_rows': len(rows),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from', dest='start', default='2025-01',
                    help="First month to backfill, YYYY-MM. Default 2025-01.")
    ap.add_argument('--to', dest='end', default=None,
                    help="Last month, YYYY-MM. Default: last complete month.")
    ap.add_argument('--dry-run', action='store_true',
                    help="Fetch and report without writing.")
    ap.add_argument('--force', action='store_true',
                    help="Overwrite months that already have Google spend.")
    args = ap.parse_args()

    sy, sm = (int(x) for x in args.start.split('-'))
    if args.end:
        ey, em = (int(x) for x in args.end.split('-'))
    else:
        ey, em = last_complete_month(date.today())

    if not MONTHLY_PATH.exists():
        print(f"✗ {MONTHLY_PATH} not found")
        sys.exit(1)
    monthly = json.loads(MONTHLY_PATH.read_text())

    print(f"\n{'═'*66}")
    print(f"  Google Ads historical backfill — customer {CUSTOMER_ID}")
    print(f"  Range: {month_key(sy, sm)} → {month_key(ey, em)}")
    print(f"  Mode : {'DRY RUN (no writes)' if args.dry_run else 'write'}"
          f"{' + FORCE overwrite' if args.force else ''}")
    print(f"{'═'*66}\n")

    access_token = get_access_token()
    filled = skipped = absent = failed = 0

    for y, m in month_iter(sy, sm, ey, em):
        mk = month_key(y, m)
        entry = monthly.get(mk)
        if entry is None:
            print(f"  --  {mk:16} not in skinuva_monthly.json — skipping")
            absent += 1
            continue

        existing = (entry.get('google') or {}).get('spend')
        if existing and float(existing) > 0 and not args.force:
            print(f"  ok  {mk:16} already has ${float(existing):,.2f} — leaving alone")
            skipped += 1
            continue

        try:
            g = fetch_month(access_token, y, m)
        except Exception as e:
            print(f"  !!  {mk:16} FAILED: {str(e)[:120]}")
            failed += 1
            continue

        rows = g.pop('_rows')
        if g['spend'] <= 0 and g['impressions'] <= 0:
            print(f"  --  {mk:16} no Google activity ({rows} rows)")
            continue

        g['source'] = SOURCE
        entry['google'] = g
        filled += 1
        acos = (g['spend'] / g['adSales'] * 100) if g['adSales'] else None
        print(f"  ++  {mk:16} spend ${g['spend']:>10,.2f}  adSales ${g['adSales']:>11,.2f}"
              f"  ACOS {(f'{acos:.1f}%' if acos else '   n/a'):>7}"
              f"  clicks {g['clicks']:>6,}  ({rows} rows)")

    print(f"\n  filled {filled}, already-populated {skipped}, "
          f"not-on-dashboard {absent}, failed {failed}")

    if args.dry_run:
        print("\n  DRY RUN — nothing written.")
        return 0

    if filled:
        MONTHLY_PATH.write_text(json.dumps(monthly, indent=2))
        print(f"  ✓ wrote {MONTHLY_PATH}")
    else:
        print("  nothing to write.")

    if failed:
        print(f"\n  ⚠  {failed} month(s) failed — re-run to retry just those "
              f"(filled months are skipped automatically).")
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
