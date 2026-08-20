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
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
               'July', 'August', 'September', 'October', 'November', 'December']

# ── Value plausibility ─────────────────────────────────────────────────────────
# Freshness was never enough, and one incident proved it expensively.
#
# A change added the Canada marketplace, and build_asin_brand_map() wrote Canada's
# ASIN->brand map over the US one. Every US month then matched zero ASINs, discarded
# the sales as "unmapped", and Skinuva's June 2026 total went from $159,976.50 to
# $242.41 on both dashboards. This script passed the entire time — the files WERE
# rewritten, with current timestamps. It was asking "did this refresh?" when the
# question that mattered was "is this number possible?".
#
# So: compare each completed month's revenue against the median of the other months in
# the same file. A real business does not lose 99.8% of its revenue in a month while
# still spending on ads. $242 against a $140K median is a 0.17% ratio — this would have
# caught it in the same run that broke it, instead of a week later by hand.
REVENUE_COLLAPSE_RATIO = 0.25   # below 25% of the median = implausible
REVENUE_SPIKE_RATIO    = 4.0    # above 4x the median = implausible
MIN_MONTHS_FOR_MEDIAN  = 4      # need a baseline before the comparison means anything
MAX_REFUND_RATE        = 0.25   # refunds above 25% of revenue = almost certainly a bug
# ...but only where the revenue is big enough for the ratio to mean anything. On a
# $1,000 month a couple of returned orders is legitimately 30%, and a check that fires
# on real data every month is a check people learn to ignore.
REFUND_RATE_MIN_REVENUE = 5000.0

# (path, label). Month-keyed brand files carrying amazon.totalSales.
PLAUSIBILITY_FILES = [
    ('skinuva/data/skinuva_monthly.json', 'Skinuva monthly'),
    ('skinuva/data/eraclea_monthly.json', 'eraclea monthly'),
]


def _read_marker(path):
    """Read a {"failures": [...]} marker file. Returns [] if absent."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        return list((json.loads(p.read_text()) or {}).get('failures') or [])
    except Exception as e:
        return [f'unreadable {path}: {e}']


def _month_sort_key(mk):
    try:
        name, year = mk.split(' ')
        return (int(year), MONTH_NAMES.index(name))
    except Exception:
        return (0, 0)


def _is_current_or_future(mk, now):
    """Partial months are legitimately small; don't compare them to full ones."""
    try:
        name, year = mk.split(' ')
        return (int(year), MONTH_NAMES.index(name) + 1) >= (now.year, now.month)
    except Exception:
        return True


def check_parseable():
    """
    Every committed data file must be valid JSON with no git conflict markers.

    This exists because of a live incident. skinuva_monthly.json was deployed containing

        <<<<<<< Updated upstream
              "totalSales": 151383.39,
        =======
              "totalSales": null,
        >>>>>>> Stashed changes

    -- an unresolved `git stash pop` conflict. The file served HTTP 200, the browser's
    JSON.parse threw, the dashboard's `.catch(()=>{})` swallowed it, and SKINUVA_MONTHLY
    stayed empty. With Skinuva's revenue missing, the Amazon row divided the whole
    account's ad spend by eraclea's ~$1.5K and displayed TACOS as 2573.9%. Nothing in
    the console, nothing in the workflow, no clue anywhere.

    One grep would have caught it, so now one grep does.
    """
    problems = []
    roots = [Path('data'), Path('skinuva/data')]
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.glob('*.json')):
            try:
                text = p.read_text(encoding='utf-8', errors='replace')
            except Exception as e:
                problems.append(f'{p}: cannot read ({e})')
                continue
            markers = [ln for ln in text.splitlines()
                       if ln.startswith('<<<<<<<') or ln.startswith('>>>>>>>')]
            if markers:
                problems.append(
                    f'{p}: contains {len(markers)} unresolved git conflict marker(s) '
                    f'— the file is not valid JSON and every figure derived from it '
                    f'will silently fall back to an incomplete source')
                continue
            try:
                json.loads(text)
            except Exception as e:
                problems.append(f'{p}: invalid JSON — {e}')
    return problems


def check_plausibility(now):
    """
    Returns a list of human-readable problems. Empty means every value looks possible.
    Deliberately conservative: it should only fire on figures that cannot be real, so
    that when it does fire, it is worth stopping for.
    """
    problems = []

    for path, label in PLAUSIBILITY_FILES:
        p = Path(path)
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text())
        except Exception as e:
            problems.append(f'{label}: unreadable ({e})')
            continue
        if not isinstance(data, dict):
            continue

        months = sorted([m for m in data if isinstance(data.get(m), dict)],
                        key=_month_sort_key)

        # ── 1. Revenue magnitude vs the file's own history ────────────────────
        vals = {}
        for mk in months:
            amz = (data[mk].get('amazon') or {})
            ts = amz.get('totalSales')
            if isinstance(ts, (int, float)) and ts > 0:
                vals[mk] = float(ts)
        if len(vals) >= MIN_MONTHS_FOR_MEDIAN:
            for mk, v in vals.items():
                if _is_current_or_future(mk, now):
                    continue
                others = [x for k, x in vals.items() if k != mk]
                med = statistics.median(others)
                if med <= 0:
                    continue
                ratio = v / med
                if ratio < REVENUE_COLLAPSE_RATIO:
                    problems.append(
                        f'{label} {mk}: Amazon totalSales ${v:,.2f} is only '
                        f'{ratio*100:.1f}% of the ${med:,.2f} median for other months '
                        f'— looks like a brand-mapping or fetch failure, not a real drop')
                elif ratio > REVENUE_SPIKE_RATIO:
                    problems.append(
                        f'{label} {mk}: Amazon totalSales ${v:,.2f} is {ratio:.1f}x the '
                        f'${med:,.2f} median for other months — looks like data from '
                        f'another brand or marketplace leaked in')

        for mk in months:
            entry = data[mk]
            amz = (entry.get('amazon') or {})
            ts = amz.get('totalSales')

            # ── 2. Arithmetic identities must hold ───────────────────────────
            # These are the fields the dashboards actually display. If a component
            # changes and a derived field doesn't, the page shows a total that does
            # not equal its own parts.
            comb = entry.get('combinedTotalSales')
            if isinstance(ts, (int, float)) and isinstance(comb, (int, float)):
                expect = ts + float(entry.get('shopify') or 0) + float(entry.get('walmart') or 0)
                if abs(expect - comb) > 0.05:
                    problems.append(
                        f'{label} {mk}: combinedTotalSales ${comb:,.2f} != Amazon '
                        f'${ts:,.2f} + Shopify ${float(entry.get("shopify") or 0):,.2f} '
                        f'+ Walmart ${float(entry.get("walmart") or 0):,.2f} '
                        f'= ${expect:,.2f}')
            ref = entry.get('refunds')
            net_amz = entry.get('netAmazonSales')
            if all(isinstance(x, (int, float)) for x in (ts, ref, net_amz)):
                if abs((ts - ref) - net_amz) > 0.05:
                    problems.append(
                        f'{label} {mk}: netAmazonSales ${net_amz:,.2f} != totalSales '
                        f'${ts:,.2f} - refunds ${ref:,.2f}')

            # ── 3. Refund rate sanity ────────────────────────────────────────
            if isinstance(ref, (int, float)) and isinstance(ts, (int, float)) and ts > 0:
                rate = ref / ts
                if rate > MAX_REFUND_RATE and ts >= REFUND_RATE_MIN_REVENUE:
                    problems.append(
                        f'{label} {mk}: refunds ${ref:,.2f} are {rate*100:.1f}% of '
                        f'${ts:,.2f} revenue — above the {MAX_REFUND_RATE*100:.0f}% '
                        f'sanity limit, check for double-counted refund reversals')
                if ref < 0:
                    problems.append(f'{label} {mk}: refunds are negative (${ref:,.2f})')

    # ── 4. Medaltus: brand rows must add up to the month total ────────────────
    p = Path('data/api_supplement.json')
    if p.exists():
        try:
            sup = json.loads(p.read_text())
            for entry in (sup.get('monthly') or []):
                mk = entry.get('month') or '?'
                brands = entry.get('brands') or []
                for field in ('spend', 'adSales'):
                    tot = entry.get(field)
                    if not isinstance(tot, (int, float)):
                        continue
                    bsum = sum(float(b.get(field) or 0) for b in brands)
                    if abs(bsum - tot) > max(1.0, abs(tot) * 0.005):
                        problems.append(
                            f'Medaltus supplement {mk}: {field} total ${tot:,.2f} != '
                            f'sum of {len(brands)} brand rows ${bsum:,.2f}')
        except Exception as e:
            problems.append(f'Medaltus supplement: unreadable ({e})')

    return problems

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
    # Added after an audit found these were produced by the workflow but tracked by
    # nothing, so they sat 22-28 days stale while the files around them refreshed
    # hourly. walmart_revenue.json in particular had July 2026 frozen at a $45.00
    # single-order snapshot against a true $1,059.99.
    ('skinuva/data/walmart_revenue.json',         'Walmart revenue'),
    ('skinuva/data/walmart_ads.json',             'Walmart ads'),
    ('data/refunds_by_month.json',                'Amazon refunds'),
]

# Files allowed to be absent without failing (not yet configured, etc).
# Once a file exists it must still be fresh — absence is the only exemption.
#
# refunds_by_month.json has never been produced: fetch_refunds.py was added but its
# step is continue-on-error and it had a SigV4 query-encoding bug, so the file does not
# exist yet. Exempt until the first successful run creates it, then the freshness rules
# apply like everything else.
OPTIONAL_IF_MISSING = {'data/refunds_by_month.json'}


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

    # Report submissions that failed during the Amazon Ads fetch. That step cannot
    # exit nonzero itself — it has no continue-on-error, so aborting there would skip
    # every downstream step and turn a partial loss into a total one. It records the
    # failures instead and they are raised here, after the commit, so whatever did
    # fetch is saved and the run still goes red.
    #
    # This exists because Sponsored Display was rejected by Amazon for months
    # (400: invalid column campaignBudgetType) and every run reported success while
    # SD spend and sales were missing from both dashboards.
    report_failures = _read_marker('data/report_failures.json')

    # Data-integrity problems recorded by fetch_total_sales.py during the run: a
    # rejected ASIN→brand map, or a marketplace dropped for want of an FX rate. These
    # cannot fail their own step (it would skip every downstream fetch and turn a
    # partial loss into a total one), so they are raised here instead.
    for f in _read_marker('data/data_integrity_failures.json'):
        report_failures.append(f)

    problems = []
    for path, label in TRACKED:
        status, detail = inspect(path, now, args.max_age_hours, args.max_lag_days, run_start)
        if status == 'MISSING' and path in OPTIONAL_IF_MISSING:
            status, detail = 'SKIP', detail + ' (optional)'
        mark = {'PASS': 'ok', 'SKIP': '--'}.get(status, status)
        print(f"  {mark:8} {label:26} {detail}")
        if status in ('STALE', 'MISSING', 'UNKNOWN'):
            problems.append((label, path, status, detail))

    # ── Value plausibility, not just freshness ───────────────────────────────
    # The whole reason this section exists: a wrong-but-fresh number used to pass.
    # Parseability first: an unparseable file makes every other check meaningless, and
    # it is the failure mode that actually reached production.
    implausible = check_parseable() + check_plausibility(now)
    print()
    if implausible:
        print(f"  {len(implausible)} implausible value(s):")
        for m in implausible:
            print(f"    - {m}")
            print(f"::error title=Implausible data value::{m}")
        print()

    if report_failures:
        print(f"  {len(report_failures)} Amazon Ads report submission(s) failed this run:")
        for f in report_failures:
            print(f"    - {f}")
            print(f"::error title=Amazon Ads report submission failed::{f}"
                  f" — that ad product is MISSING from both dashboards")
        print()

    if not problems and not report_failures and not implausible:
        print(f"All {len(TRACKED)} datasets refreshed, and every value looks plausible.")
        return 0

    if not problems and (report_failures or implausible):
        if report_failures:
            print(f"All {len(TRACKED)} datasets refreshed, but an ad product is incomplete.")
        if implausible:
            print(f"All {len(TRACKED)} datasets refreshed, but {len(implausible)} value(s) "
                  f"cannot be right. Fresh is not the same as correct.")
        if args.warn_only:
            print("(--warn-only: not failing the run)")
            return 0
        return 1

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
