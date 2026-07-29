#!/usr/bin/env python3
"""
verify_data_freshness.py
Fails the workflow when a data file did NOT actually refresh.

WHY THIS EXISTS
---------------
Three fetch steps run with continue-on-error, because one API being down
shouldn't throw away the datasets that did refresh. The cost is that a broken
fetch produced a GREEN run serving silently stale numbers — which is exactly
how a failure went unnoticed until someone happened to compare figures by hand.

This turns that silence into a visible failure. It runs AFTER the commit step
on purpose: whatever refreshed successfully still gets saved, and then the run
goes red so the staleness is impossible to miss.

Signals used, per file shape:
  date-keyed archives  -> newest date key must be within --max-lag-days
  flat + fetched_at    -> fetched_at must be within --max-age-hours
  month-keyed          -> newest parseable inner fetched_at, same window
  no timestamp at all  -> file mtime must be newer than the run-start marker
                          (a fresh checkout stamps every file at checkout time,
                          so a later mtime means a script actually rewrote it)

Usage:
  python3 verify_data_freshness.py --run-start-file /tmp/run_start_epoch
  python3 verify_data_freshness.py --warn-only       # report, never fail
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# (path, human label). Everything here is expected to refresh on every run.
TRACKED = [
    ('data/api_supplement.json',                  'Medaltus supplement'),
    ('data/medaltus_daily_archive.json',          'Medaltus daily archive'),
    ('data/medaltus_google_ads_monthly.json',     'Medaltus Google Ads'),
    ('skinuva/data/skinuva_supplement.json',      'Skinuva supplement'),
    ('skinuva/data/skinuva_monthly.json',         'Skinuva monthly'),
    ('skinuva/data/skinuva_daily_archive.json',   'Skinuva daily archive'),
    ('skinuva/data/google_ads_data.json',         'Skinuva Google Ads'),
    ('skinuva/data/eraclea_supplement.json',      'eraclea supplement'),
    ('skinuva/data/eraclea_monthly.json',         'eraclea monthly'),
    ('skinuva/data/eraclea_daily_archive.json',   'eraclea daily archive'),
    ('skinuva/data/shopify_sales.json',           'Shopify sales'),
]

# Files allowed to be absent without failing (not yet configured, etc).
# Once a file exists it must still be fresh — absence is the only exemption.
OPTIONAL_IF_MISSING = set()


def parse_iso(s):
    if not isinstance(s, str):
        return None
    t = s.strip().replace('Z', '+00:00')
    try:
        dt = datetime.fromisoformat(t)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def inspect(path, now, max_age, max_lag, run_start):
    """Returns (status, detail). status in {'PASS','STALE','MISSING','UNKNOWN'}"""
    p = Path(path)
    if not p.exists():
        return ('MISSING', 'file not present')
    try:
        data = json.loads(p.read_text())
    except Exception as e:
        return ('STALE', f'unreadable: {e}')

    if isinstance(data, dict) and data:
        keys = list(data.keys())

        # date-keyed archive
        if all(len(k) == 10 and k[4] == '-' and k[7] == '-' for k in keys[:5]):
            newest = max(keys)
            try:
                d = datetime.fromisoformat(newest).replace(tzinfo=timezone.utc)
            except Exception:
                return ('UNKNOWN', f'unparseable date key {newest}')
            lag = (now.date() - d.date()).days
            if lag > max_lag:
                return ('STALE', f'newest day {newest} is {lag}d old (limit {max_lag}d)')
            return ('PASS', f'newest day {newest} ({lag}d lag)')

        # flat fetched_at
        ts = parse_iso(data.get('fetched_at'))
        if ts:
            age = (now - ts).total_seconds() / 3600
            if age > max_age:
                return ('STALE', f'fetched_at {data["fetched_at"]} is {age:.1f}h old (limit {max_age}h)')
            return ('PASS', f'fetched {age:.1f}h ago')

        # month-keyed with inner fetched_at
        inner = [parse_iso(v.get('fetched_at')) for v in data.values()
                 if isinstance(v, dict)]
        inner = [t for t in inner if t]
        if inner:
            ts = max(inner)
            age = (now - ts).total_seconds() / 3600
            if age > max_age:
                return ('STALE', f'newest inner fetched_at {age:.1f}h old (limit {max_age}h)')
            return ('PASS', f'fetched {age:.1f}h ago')

    # no embedded timestamp — fall back to "was it rewritten this run?"
    if run_start is None:
        return ('UNKNOWN', 'no timestamp in file and no run-start marker')
    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    if mtime <= run_start:
        return ('STALE', f'not rewritten this run (mtime {mtime:%H:%M:%SZ} '
                         f'<= run start {run_start:%H:%M:%SZ})')
    return ('PASS', f'rewritten this run ({mtime:%H:%M:%SZ})')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-age-hours', type=float, default=8.0)
    ap.add_argument('--max-lag-days', type=int, default=2)
    ap.add_argument('--run-start-file', default=None,
                    help='file containing the run-start epoch seconds')
    ap.add_argument('--warn-only', action='store_true')
    args = ap.parse_args()

    run_start = None
    if args.run_start_file and Path(args.run_start_file).exists():
        try:
            run_start = datetime.fromtimestamp(
                int(Path(args.run_start_file).read_text().strip()), tz=timezone.utc)
        except Exception:
            pass

    now = datetime.now(timezone.utc)
    print(f"Data freshness check — {now:%Y-%m-%d %H:%M:%SZ}")
    print(f"  limits: fetched_at < {args.max_age_hours}h, archive lag <= {args.max_lag_days}d")
    if run_start:
        print(f"  run started: {run_start:%H:%M:%SZ}")
    print()
    print(f"  {'status':8} {'dataset':26} detail")
    print("  " + "-" * 88)

    problems = []
    for path, label in TRACKED:
        status, detail = inspect(path, now, args.max_age_hours, args.max_lag_days, run_start)
        if status == 'MISSING' and path in OPTIONAL_IF_MISSING:
            status, detail = 'SKIP', detail + ' (optional)'
        mark = {'PASS': 'ok', 'SKIP': '--'}.get(status, status)
        print(f"  {mark:8} {label:26} {detail}")
        if status in ('STALE', 'MISSING', 'UNKNOWN'):
            problems.append((label, path, status, detail))

    print()
    if not problems:
        print(f"All {len(TRACKED)} datasets refreshed.")
        return 0

    for label, path, status, detail in problems:
        # GitHub Actions annotation so it surfaces on the run summary, not just the log
        print(f"::error file={path}::{label} did not refresh — {detail}")
    print(f"\n{len(problems)} of {len(TRACKED)} datasets did NOT refresh:")
    for label, path, status, detail in problems:
        print(f"  - {label} ({status}): {detail}")
    print("\nThe data that DID refresh has already been committed by the previous")
    print("step. This failure exists so the stale datasets don't pass unnoticed.")

    if args.warn_only:
        print("\n(--warn-only: not failing the run)")
        return 0
    return 1


if __name__ == '__main__':
    sys.exit(main())
