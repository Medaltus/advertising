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

import calendar
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

BRAND_NAME = 'Skinuva'
MONTH_NAMES = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
]


def _load_root_config(script_dir: Path):
    """
    Find config.json (written from the CONFIGJSON GitHub secret at CI time,
    or present locally for manual/dev runs). Returns None if not found --
    callers must degrade gracefully rather than crash, since this script
    already runs with continue-on-error in the workflow.
    """
    candidates = [
        Path.cwd() / 'config.json',
        script_dir.parent / 'config.json',   # deploy/config.json (the real one in CI)
        script_dir / 'config.json',          # skinuva/config.json, if ever used standalone
    ]
    for p in candidates:
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                continue
    return None


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
        'fetched_at':   data.get('fetched_at', ''),
        'lookback_days': data.get('lookback_days', 31),
        'currency':     data.get('currency', 'USD'),
        'summary':      brand.get('summary', {}),
        'timeline':     brand.get('timeline', []),
        'campaigns':    brand.get('campaigns', []),
        'pacing':       brand.get('pacing', []),
        'search_terms': brand.get('search_terms', []),
        'asins':        brand.get('asins', []),
        'placements':   brand.get('placements', []),
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


def build_search_term_insights(search_terms: list) -> dict:
    """Categorize search terms into top 10 per bucket."""
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
        keys = ('query','spend','sales','acos','impressions','clicks','ctr','cpc','purchases','cvr')
        return [{**{k: t.get(k) for k in keys}, 'daily': t.get('daily', [])} for t in terms[:10]]

    def trim_cpc(terms):
        keys = ('query','spend','sales','acos','impressions','clicks','ctr','cpc','purchases','cvr')
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


def month_key_from_date(date_str: str) -> str:
    """'2026-05-15' → 'May 2026'"""
    dt = datetime.strptime(date_str[:7], '%Y-%m')
    return f"{MONTH_NAMES[dt.month - 1]} {dt.year}"


def build_daily_archive(supplement: dict, google_path: Path) -> dict:
    """
    Store per-day Amazon + Google rows for arbitrary date-range queries.
    Each entry: {"amazon": {...}, "google": {...}}
    Merged into skinuva_daily_archive.json — new data wins, old days preserved.
    """
    entries = {}

    # Amazon daily rows
    for row in supplement.get('timeline', []):
        d = row.get('date')
        if not d:
            continue
        entries.setdefault(d, {'amazon': {}, 'google': {}})
        entries[d]['amazon'] = {
            'spend':       round(float(row.get('spend', 0) or 0), 2),
            'adSales':     round(float(row.get('sales', 0) or 0), 2),
            # NOT written any more. The ads timeline's per-day totalSales is the WHOLE
            # PORTFOLIO's sales (see backfill_brand_total_sales's docstring below), and
            # storing it under a brand-scoped key made it look brand-filtered. The
            # committed files prove the damage: eraclea's daily archive summed to
            # $132,851.15 for June 2026 against a true $1,224.36 -- 108x -- and was
            # byte-identical to Skinuva's on 35 consecutive days. Skinuva's own archive
            # summed to $229,553.33 against a true $159,976.50.
            #
            # There is no daily brand-filtered source, so the honest value is null.
            # Brand-level revenue lives in skinuva_monthly.json / eraclea_monthly.json,
            # derived per calendar month by backfill_brand_total_sales().
            'totalSales':  None,
            'orders':      int(row.get('purchases', 0) or 0),
            'clicks':      int(row.get('clicks', 0) or 0),
            'impressions': int(row.get('impressions', 0) or 0),
        }

    # Google daily rows.
    # Prefer `daily_history` — it covers completed calendar months, not just the
    # rolling lookback, so days that scrolled out of the window get REFRESHED with
    # Google's current values rather than staying frozen at whatever was attributed
    # when they last fell inside it. Google restates conversion value retroactively,
    # and that freezing left July understated by $1,002.88 (2.55%) versus the
    # Google Ads UI while spend matched to 3 cents.
    # Falls back to `timeline` for older files that predate the field.
    if google_path.exists():
        try:
            gdata = json.loads(google_path.read_text())
            _grows = gdata.get('daily_history') or gdata.get('timeline', [])
            for row in _grows:
                d = row.get('date')
                if not d:
                    continue
                entries.setdefault(d, {'amazon': {}, 'google': {}})
                entries[d]['google'] = {
                    'spend':       round(float(row.get('spend', 0) or 0), 2),
                    'adSales':     round(float(row.get('sales', 0) or 0), 2),
                    'conversions': round(float(row.get('conversions', 0) or 0), 1),
                    'clicks':      int(row.get('clicks', 0) or 0),
                    'impressions': int(row.get('impressions', 0) or 0),
                }
        except Exception as exc:
            print(f"  ⚠  Could not read google_ads_data.json for daily archive: {exc}")

    return entries


def merge_daily_archive(existing: dict, new: dict) -> dict:
    """
    Merge freshly-built per-day rows into the existing daily archive.

    Amazon and Google are fetched with separate lookback windows, so a given
    day can fall inside one channel's window but not the other on a given run.
    build_daily_archive() always returns both keys for such a day (one real,
    one an empty placeholder — see its docstring), so a plain
    {**existing, **new} merge would wipe out a channel's already-good
    historical data every time that happens. Only overwrite a channel's data
    for a day when this run actually produced something for it.
    """
    merged = dict(existing)
    for d, entry in new.items():
        if d not in merged:
            merged[d] = {'amazon': {}, 'google': {}}
        for channel in ('amazon', 'google'):
            fresh = entry.get(channel) or {}
            if fresh:
                merged[d][channel] = fresh
    return dict(sorted(merged.items()))


def correct_monthly_from_archive(monthly: dict, daily: dict, today_month_str: str) -> None:
    """
    Re-derive Amazon/Google spend, ad sales, orders, clicks and impressions
    for every COMPLETED month (not the current, still-in-progress month) from
    the complete daily archive, mutating `monthly` in place.

    The monthly entries are otherwise built from the live ads_data.js
    timeline, which only covers a rolling lookback window (~31 days). Once a
    month is no longer the current month it may only be partially covered by
    that window — e.g. just its last handful of days — which silently
    truncates spend/sales/orders for that month a little more each day until
    the window fully exits it. The daily archive accumulates every day
    permanently (once merge_daily_archive() is used to update it), so summing
    it gives the true, complete month total instead.

    Deliberately does NOT touch totalSales/combinedTotalSales — Amazon's true
    (non-ad-attributed) revenue has its own, separately-maintained sourcing
    per brand and is left exactly as the caller already computed it.

    Skips any month where the archive doesn't cover at least 90% of that
    month's calendar days — e.g. May 2026 only has archive rows from the 20th
    onward because the daily-archive feature didn't exist before then, so
    summing it would silently understate the month instead of fixing it.
    In that case the existing (already-imperfect) value is left alone rather
    than replaced with a differently-imperfect one.
    """
    archive_monthly: dict = defaultdict(lambda: {
        'amazon': {'spend': 0.0, 'adSales': 0.0, 'orders': 0, 'clicks': 0, 'impressions': 0},
        'google': {'spend': 0.0, 'adSales': 0.0, 'conversions': 0.0, 'clicks': 0, 'impressions': 0},
        'days_covered': set(),
    })
    for d_str, day_entry in daily.items():
        if not day_entry or not day_entry.get('amazon'):
            continue
        mk = month_key_from_date(d_str)
        archive_monthly[mk]['days_covered'].add(d_str)
        am = day_entry.get('amazon') or {}
        a = archive_monthly[mk]['amazon']
        a['spend']       += float(am.get('spend', 0) or 0)
        a['adSales']     += float(am.get('adSales', 0) or 0)
        a['orders']      += int(am.get('orders', 0) or 0)
        a['clicks']      += int(am.get('clicks', 0) or 0)
        a['impressions'] += int(am.get('impressions', 0) or 0)

        gg = day_entry.get('google') or {}
        g = archive_monthly[mk]['google']
        g['spend']       += float(gg.get('spend', 0) or 0)
        g['adSales']     += float(gg.get('adSales', 0) or 0)
        g['conversions'] += float(gg.get('conversions', 0) or 0)
        g['clicks']      += int(gg.get('clicks', 0) or 0)
        g['impressions'] += int(gg.get('impressions', 0) or 0)

    for mk, arch in archive_monthly.items():
        if mk == today_month_str or mk not in monthly:
            continue
        month_name, year_str = mk.split(' ')
        month_num = MONTH_NAMES.index(month_name) + 1
        days_in_month = calendar.monthrange(int(year_str), month_num)[1]
        coverage = len(arch['days_covered']) / days_in_month
        if coverage < 0.9:
            print(f"  [{mk}] Skipped archive correction — only {len(arch['days_covered'])}/{days_in_month} "
                  f"days covered ({coverage:.0%}); leaving existing value as-is")
            continue
        entry = monthly[mk]
        prev_total_sales = (entry.get('amazon') or {}).get('totalSales')
        prev_google       = entry.get('google') or {}
        entry['amazon'] = {
            'spend':       round(arch['amazon']['spend'], 2),
            'adSales':     round(arch['amazon']['adSales'], 2),
            'totalSales':  prev_total_sales,
            'orders':      arch['amazon']['orders'],
            'clicks':      arch['amazon']['clicks'],
            'impressions': arch['amazon']['impressions'],
        }
        entry['google'] = {
            'spend':       round(arch['google']['spend'], 2),
            'adSales':     round(arch['google']['adSales'], 2),
            'conversions': (round(arch['google']['conversions'])
                            if arch['google']['conversions'] else prev_google.get('conversions', 0)),
            'clicks':      arch['google']['clicks'],
            'impressions': arch['google']['impressions'],
        }
        print(f"  [{mk}] Archive-corrected: amazon.spend=${entry['amazon']['spend']:,.2f}, "
              f"adSales=${entry['amazon']['adSales']:,.2f}, orders={entry['amazon']['orders']}, "
              f"google.spend=${entry['google']['spend']:,.2f}")


TOTAL_SALES_SOURCE_OK = 'sp-api-brand-filtered'

# Source markers we accept as a trustworthy per-brand revenue figure — i.e. anything
# genuinely filtered to this brand's ASINs. Used both to freeze settled months and to
# retain a value when a fetch fails.
#
# 'helium10-asin-attributed' covers the Jan 2025 – Feb 2026 history, which Helium 10
# supplied per ASIN. It has to be listed here or the backfilled months would look
# untrusted: the pipeline would re-request all 14 from SP-API on every run — which is
# exactly the quota exhaustion that was wiping June — and then null them when that
# failed. Verified against SP-API on the overlap: June 2026 and eraclea June matched
# to the cent, so the two sources agree.
#
# Still deliberately excludes the portfolio-wide timeline figure, which is the one
# number that must never be resurrected as a brand's revenue.
TRUSTED_TOTAL_SALES_SOURCES = (TOTAL_SALES_SOURCE_OK, 'helium10-asin-attributed')


def _is_trusted_total_sales(src) -> bool:
    s = str(src or '')
    return any(s.startswith(p) for p in TRUSTED_TOTAL_SALES_SOURCES)

# How long after a month ends we keep re-fetching it, so Amazon's late returns and
# refunds still land. Past this the figure is treated as settled and frozen, which
# keeps the daily SP-API report quota for the months that actually still move.
# 30 days covers Amazon's standard return window with room to spare.
RESTATEMENT_GRACE_DAYS = 30


def _invalidate_total_sales(entry: dict, mk: str, brand_name: str, reason: str,
                            existing: dict) -> None:
    """
    We could not obtain a trustworthy brand-filtered totalSales for this month.

    The value currently sitting in `entry` came from build_monthly_archive(),
    which sums the ads timeline's portfolio-wide totalSales — a number that is
    simply wrong for a single brand (see backfill_brand_total_sales docstring).
    Leaving it in place is the exact failure mode we're fixing, so instead:

      1. Re-use the previously-committed value IF it was itself produced by a
         successful brand-filtered fetch (marked with totalSalesSource). This
         mirrors generate_supplement.py's proven behaviour — a transient
         SP-API hiccup must not wipe out yesterday's good number.
      2. Otherwise null it out, so the dashboard falls back to the
         Google-Sheet-maintained Total Sales rather than rendering a
         portfolio-wide figure as if it were this brand's.
    """
    prev = (existing.get(mk) or {}) if existing else {}
    prev_amz = prev.get('amazon') or {}
    amz = entry.setdefault('amazon', {})

    # startswith, not ==, and the retained value keeps a marker that also starts with
    # TOTAL_SALES_SOURCE_OK. Without that this was a ONE-WAY RATCHET: the first
    # failure stamped 'unavailable (...)', and every later failure then saw a
    # non-matching marker in `existing` and nulled again, so the month could only ever
    # be restored by a fetch that happened to succeed. June 2026's SP-API fetch fails
    # roughly one run in three, so it flip-flopped between $159,976.50 and blank for a
    # week (verified across 14 commits) and was blank whenever two failures landed in
    # a row. Retention now chains across consecutive failures.
    #
    # Still deliberately refuses to resurrect anything NOT from a brand-filtered
    # fetch -- the whole point is never to show a portfolio-wide figure as this
    # brand's -- which is why this tests the marker rather than just "is not None".
    if (_is_trusted_total_sales(prev_amz.get('totalSalesSource'))
            and prev_amz.get('totalSales') is not None):
        amz['totalSales'] = prev_amz['totalSales']
        amz['totalSalesSource'] = f'{TOTAL_SALES_SOURCE_OK} (retained: {reason})'
        shopify = float(entry.get('shopify', 0) or 0)
        walmart = float(entry.get('walmart', 0) or 0)
        entry['combinedTotalSales'] = round(amz['totalSales'] + shopify + walmart, 2)
        print(f"  ↩  [{mk}] {brand_name}: {reason} — keeping last known-good "
              f"brand-filtered totalSales ${amz['totalSales']:,.2f}")
    else:
        amz['totalSales'] = None
        amz['totalSalesSource'] = f'unavailable ({reason})'
        entry['combinedTotalSales'] = None
        print(f"  ⚠  [{mk}] {brand_name}: {reason} — totalSales set to null "
              f"(dashboard will fall back to the Google Sheet rather than show a "
              f"portfolio-wide number)")


def apply_refunds(monthly: dict, brand_name: str, script_dir: Path) -> int:
    """
    Copy per-brand refunds from data/refunds_by_month.json onto each month.

    Kept as a separate step reading a separate file so a refunds fetch failure cannot
    touch sales data. Existing values are preserved when the file has nothing for a
    month — a partial refunds run must not blank out months that already had a figure,
    which is how the dashboard's Refunds and Net Sales rows would silently vanish.
    """
    path = script_dir.parent / 'data' / 'refunds_by_month.json'
    if not path.exists():
        print(f"  ℹ  {path.name} not present — leaving existing refunds untouched")
        return 0
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        print(f"  ⚠  could not read {path.name}: {e} — leaving refunds untouched")
        return 0

    n = 0
    for mk, entry in monthly.items():
        amt = (data.get(mk) or {}).get(brand_name)
        if amt is None:
            continue
        entry['refunds'] = round(float(amt), 2)
        entry['refundsSource'] = 'sp-api-finances'
        amz = (entry.get('amazon') or {}).get('totalSales')
        if amz is not None:
            entry['netAmazonSales'] = round(amz - entry['refunds'], 2)
        comb = entry.get('combinedTotalSales')
        if comb is not None:
            entry['netCombinedTotalSales'] = round(comb - entry['refunds'], 2)
        n += 1
    print(f"  ✓ refunds applied to {n} month(s) for {brand_name}")
    return n


def backfill_brand_total_sales(monthly: dict, daily: dict, brand_name: str, cfg,
                                today_month_str: str, existing: dict = None,
                                fetch_cache: dict = None) -> None:
    """
    Re-derive Amazon totalSales for every month (current + completed) using
    fetch_brand_sales_for_period(), which correctly filters to this brand's
    ASINs via the SP-API sales report. Mutates `monthly` in place.

    Why this is needed: ads_data.js's per-day timeline['totalSales'] is the
    WHOLE portfolio's total sales (every brand on the shared Amazon account,
    combined) stamped identically onto every brand's rows -- see
    fetch_ads_data.py: `row["totalSales"] = total_sales_by_date.get(...)`,
    where total_sales_by_date is portfolio-wide. Verified empirically: Skinuva
    and eraclea's daily archives held byte-identical totalSales on every date
    both were populated, despite ~30x different ad spend. Summing it per brand
    therefore overstates that brand by however much every OTHER brand on the
    account sold, and the overstatement drifts daily -- which is what looked
    like data changing/disappearing.

    This mirrors the approach generate_supplement.py already uses successfully
    for the root Medaltus dashboard: one SP-API call per calendar month, brand
    totals read out of that single response.

    `fetch_cache` — pass the SAME dict across brands so the per-month SP-API
    report is submitted once and shared, not re-submitted per brand. Each
    fetch is a submit+poll+download round trip, so this halves the API work
    for a two-brand run.

    `existing` — the previously-committed monthly file, used to preserve the
    last known-good value if this run can't get a trustworthy one.

    Only fetches months where the daily archive covers >=90% of the expected
    days (full calendar month, or days-elapsed-so-far for the in-progress
    month). Anything less and we can't trust the month is really complete.
    """
    if fetch_cache is None:
        fetch_cache = {}
    existing = existing or {}

    have_creds = bool(cfg and cfg.get('sp_refresh_token') and cfg.get('sp_client_id'))
    if not have_creds:
        print(f"  ⚠  No SP-API credentials available — cannot verify {brand_name} totalSales")

    days_covered = defaultdict(set)
    for d_str, day_entry in daily.items():
        if day_entry and day_entry.get('amazon'):
            days_covered[month_key_from_date(d_str)].add(d_str)

    today = datetime.now().date()

    for mk in list(monthly.keys()):
        entry = monthly[mk]

        parts = mk.split(' ')
        if len(parts) != 2 or parts[0] not in MONTH_NAMES or not parts[1].isdigit():
            print(f"  ⚠  Unrecognised month key '{mk}' — skipping totalSales backfill")
            continue
        month_num = MONTH_NAMES.index(parts[0]) + 1
        year = int(parts[1])

        if not have_creds:
            _invalidate_total_sales(entry, mk, brand_name, 'no SP-API credentials', existing)
            continue

        days_in_month = calendar.monthrange(year, month_num)[1]
        start = datetime(year, month_num, 1).date()

        if mk == today_month_str:
            end = today - timedelta(days=1)   # SP-API lags ~1-2 days; exclude today
            if end < start:
                continue   # first day of the month — nothing to fetch yet
            # In-progress month: judge coverage against days elapsed so far, not the
            # full calendar month, or this would never pass until month-end.
            expected_days = (end - start).days + 1
        else:
            end = datetime(year, month_num, days_in_month).date()
            expected_days = days_in_month

        # ── Freeze settled months: don't re-request what we already have ──────
        #
        # Every run was submitting a fresh SP-API report for EVERY month in the file.
        # Those reports are quota-limited per account, the Medaltus pipeline burns
        # part of that quota earlier in the same workflow, and Amazon answers the
        # overflow with 429 QuotaExceeded. Confirmed in run 31500966243: both June
        # and July failed with QuotaExceeded on createReport.
        #
        # A completed month's revenue stops moving once Amazon finishes settling
        # returns and refunds, so re-fetching it forever buys nothing and costs the
        # quota that the months we DO need are competing for. Past the grace window
        # we reuse the stored brand-filtered figure and skip the call entirely.
        #
        # Only ever skips when there is already a good brand-filtered value to reuse
        # — a month that never fetched successfully keeps being retried.
        prev_amz_f = ((existing.get(mk) or {}).get('amazon') or {}) if existing else {}
        prev_src_f = str(prev_amz_f.get('totalSalesSource') or '')
        prev_val_f = prev_amz_f.get('totalSales')
        settled_days = (today - end).days
        if (mk != today_month_str
                and prev_val_f is not None
                and _is_trusted_total_sales(prev_src_f)
                and settled_days > RESTATEMENT_GRACE_DAYS):
            amz_f = entry.setdefault('amazon', {})
            amz_f['totalSales'] = prev_val_f
            amz_f['totalSalesSource'] = prev_src_f
            shopify_f = float(entry.get('shopify', 0) or 0)
            walmart_f = float(entry.get('walmart', 0) or 0)
            entry['combinedTotalSales'] = round(prev_val_f + shopify_f + walmart_f, 2)
            print(f"  ⏭  [{mk}] {brand_name}: settled {settled_days}d ago — reusing "
                  f"stored ${prev_val_f:,.2f}, no SP-API call")
            continue

        covered = len(days_covered.get(mk, set()))
        coverage = covered / expected_days if expected_days else 0
        if coverage < 0.9:
            _invalidate_total_sales(
                entry, mk, brand_name,
                f'archive covers only {covered}/{expected_days} expected days ({coverage:.0%})',
                existing)
            continue

        key = (start.isoformat(), end.isoformat())
        if key in fetch_cache:
            brand_sales = fetch_cache[key]
        else:
            from fetch_total_sales import fetch_brand_sales_for_period
            try:
                brand_sales = fetch_brand_sales_for_period(cfg, key[0], key[1])
                fetch_cache[key] = brand_sales
            except Exception as e:
                fetch_cache[key] = None
                brand_sales = None
                print(f"  ⚠  [{mk}] SP-API fetch failed: {e}")

        if brand_sales is None:
            _invalidate_total_sales(entry, mk, brand_name, 'SP-API fetch failed', existing)
            continue

        if brand_name not in brand_sales:
            # Brand genuinely absent from the response — could be a real zero
            # (no sales that month) or a broken ASIN→brand mapping. Can't tell
            # them apart, so don't guess.
            _invalidate_total_sales(
                entry, mk, brand_name,
                'brand missing from SP-API response', existing)
            continue

        total = round(float(brand_sales.get(brand_name) or 0), 2)
        amz = entry.setdefault('amazon', {})
        amz['totalSales'] = total
        amz['totalSalesSource'] = TOTAL_SALES_SOURCE_OK
        shopify = float(entry.get('shopify', 0) or 0)
        walmart = float(entry.get('walmart', 0) or 0)
        entry['combinedTotalSales'] = round(total + shopify + walmart, 2) if (total or shopify or walmart) else None
        print(f"  ✓ [{mk}] {brand_name} totalSales (brand-filtered, {key[0]}→{key[1]}): ${total:,.2f}")


def _snapshot_covers_month(entry: dict, mk: str, label: str) -> bool:
    """
    Does this Shopify/Walmart snapshot actually cover the whole calendar month?

    These files carry a `range: {since, until}`, and it was read for nothing — the API
    value won unconditionally. That is fine while a month is in progress, but the
    snapshot then STAYS on disk after the month closes, and any later run rebuilding
    that month overwrote the complete figure with the stale partial one while printing a
    reassuring "using API $X" line.

    Live examples in the committed data:
      shopify_sales.json  July 2026: range ends 2026-07-28, total $46,250.76
                          skinuva_monthly.json July 2026: $50,039.42  (-$3,788.66)
      walmart_revenue.json July 2026: total $45.00, fetched 2026-07-29
                          skinuva_monthly.json July 2026: $1,059.99   (-$1,014.99)

    Understating revenue overstates TACOS, which is the number budgets are set from, so
    a partial snapshot for a CLOSED month is rejected rather than trusted. In-progress
    months are still accepted: there the month-to-date figure is the correct answer.
    """
    if not isinstance(entry, dict):
        return True
    rng = entry.get('range') or {}
    until = rng.get('until')
    if not until:
        return True                      # no range metadata — nothing to check
    try:
        parts = mk.split(' ')
        month_num = MONTH_NAMES.index(parts[0]) + 1
        year = int(parts[1])
        until_d = datetime.strptime(str(until)[:10], '%Y-%m-%d').date()
    except Exception:
        return True
    today = datetime.now().date()
    if (year, month_num) >= (today.year, today.month):
        return True                      # in progress: month-to-date is correct
    last_day = calendar.monthrange(year, month_num)[1]
    if until_d.day >= last_day and until_d.month == month_num and until_d.year == year:
        return True
    print(f"  ⚠  [{mk}] {label}: snapshot only covers through {until_d} but the month "
          f"closed on day {last_day} — REJECTED so a partial figure cannot overwrite "
          f"the complete one. Re-run the {label} fetch to refresh it.")
    return False


def build_monthly_archive(supplement: dict, google_path: Path, manual_path: Path,
                          include_dtc: bool = True) -> dict:
    """
    Aggregate Amazon + Google timeline data by calendar month.
    Returns a dict keyed by month label (e.g. 'June 2026') with per-channel totals.
    This is merged with the existing skinuva_monthly.json so historical months
    outside the current lookback window are never lost.

    include_dtc — whether this brand HAS a Shopify store and a Walmart listing.
    False for eraclea, which is Amazon-only.

    Why this parameter has to exist: the eraclea call site passes deliberately-absent
    sentinel paths (`_no_manual.json`, `_no_google.json`) to suppress Skinuva's inputs,
    but shopify_path and walmart_path were re-derived from `manual_path.parent` — still
    `skinuva/data/` — so the sentinel had no effect on them. eraclea therefore inherited
    SKINUVA's Shopify and Walmart revenue verbatim: June 2026 recorded
    combinedTotalSales $43,831.10 against $1,224.36 of actual Amazon sales, 35x
    overstated, and apply_refunds() then published netCombinedTotalSales from it. Same
    class of bug as the ASIN cache: a path recomputed from a variable the caller
    believed it had overridden.
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

    # ── Shopify sales from the Admin API (fetch_shopify_sales.py) ─────────────
    # Preferred over the hand-entered value in manual_totals.json, but we fall
    # back to manual whenever the API value is missing for a month — so a
    # Shopify outage or a month predating the integration keeps working.
    # Walmart stays manual; there's no API for it here.
    shopify_api = {}
    shopify_path = manual_path.parent / 'shopify_sales.json'
    if include_dtc and shopify_path.exists():
        try:
            shopify_api = json.loads(shopify_path.read_text())
        except Exception:
            pass

    # ── Walmart total sales, from the "Newderm - Walmart Revenue" Google Sheet
    # (skinuva tab). Preferred over the hand-entered manual_totals value, which
    # is what it replaces; falls back to manual for months the sheet doesn't
    # cover so nothing drops to zero.
    walmart_sheet = {}
    walmart_path = manual_path.parent / 'walmart_revenue.json'
    if include_dtc and walmart_path.exists():
        try:
            walmart_sheet = json.loads(walmart_path.read_text())
        except Exception:
            pass

    # ── Build per-month entries ───────────────────────────────────────────────
    entries = {}
    for mk in set(amz.keys()) | set(ggl.keys()):
        a  = amz[mk]
        g  = ggl[mk]
        mt = manual.get(mk, {})
        _api_shop = (shopify_api.get(mk) or {}).get('total')
        if _api_shop is not None and _snapshot_covers_month(shopify_api.get(mk), mk, 'Shopify'):
            shopify = float(_api_shop)
            _manual_shop = mt.get('shopify')
            if _manual_shop is not None and abs(float(_manual_shop) - shopify) > max(1.0, shopify * 0.01):
                print(f"  ℹ  [{mk}] Shopify: using API ${shopify:,.2f} "
                      f"(manual value was ${float(_manual_shop):,.2f}, differs by "
                      f"${shopify - float(_manual_shop):+,.2f})")
        else:
            shopify = float(mt.get('shopify', 0) or 0)
        _sheet_wm = (walmart_sheet.get(mk) or {}).get('total')
        if _sheet_wm is not None and _snapshot_covers_month(walmart_sheet.get(mk), mk, 'Walmart'):
            walmart = float(_sheet_wm)
            _manual_wm = mt.get('walmart')
            if _manual_wm is not None and abs(float(_manual_wm) - walmart) > max(1.0, walmart * 0.01):
                print(f"  ℹ  [{mk}] Walmart: using sheet ${walmart:,.2f} "
                      f"(manual value was ${float(_manual_wm):,.2f}, differs by "
                      f"${walmart - float(_manual_wm):+,.2f})")
        else:
            walmart = float(mt.get('walmart', 0) or 0)
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
    cfg = _load_root_config(script_dir)

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

    # ── Add search term insights ──────────────────────────────────────────────
    supplement['searchTermInsights'] = build_search_term_insights(
        supplement.get('search_terms', []))
    supplement['productInsights'] = build_asin_insights(
        supplement.get('asins', []))
    supplement['placementInsights'] = supplement.get('placements', [])

    # ── Write flat supplement (used for current-month injection) ─────────────
    out_path = out_dir / 'skinuva_supplement.json'
    out_path.write_text(json.dumps(supplement, indent=2))
    print(f"\n✓ Wrote {out_path}")

    google_path  = out_dir / 'google_ads_data.json'
    manual_path  = out_dir / 'manual_totals.json'
    today_month_str = f"{MONTH_NAMES[datetime.now().month - 1]} {datetime.now().year}"

    # ── Build and merge skinuva_daily_archive.json FIRST (per-day rows for date
    # range queries) — the monthly archive below is corrected from this, so it
    # must be up to date before we touch the monthly file. ──────────────────────
    daily_path = out_dir / 'skinuva_daily_archive.json'
    new_daily = build_daily_archive(supplement, google_path)

    existing_daily = {}
    if daily_path.exists():
        try:
            existing_daily = json.loads(daily_path.read_text())
        except Exception:
            pass

    merged_daily = merge_daily_archive(existing_daily, new_daily)
    daily_path.write_text(json.dumps(merged_daily, indent=2))
    print(f"✓ Updated {daily_path} ({len(merged_daily)} days)")

    # ── Build and merge skinuva_monthly.json (permanent historical archive) ──
    monthly_path = out_dir / 'skinuva_monthly.json'

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

    print("\nCorrecting completed months' spend/orders from complete daily archive…")
    correct_monthly_from_archive(merged, merged_daily, today_month_str)

    # Shared across both brands so each calendar month's SP-API report is
    # submitted once, not once per brand.
    ts_fetch_cache: dict = {}

    print(f"\nBackfilling {BRAND_NAME}-specific totalSales from SP-API (portfolio-wide fix)…")
    backfill_brand_total_sales(merged, merged_daily, BRAND_NAME, cfg, today_month_str,
                               existing=existing, fetch_cache=ts_fetch_cache)

    print("\nApplying Amazon refunds…")
    apply_refunds(merged, BRAND_NAME, Path(__file__).parent)

    monthly_path.write_text(json.dumps(merged, indent=2))
    print(f"✓ Updated {monthly_path} ({len(merged)} months: {', '.join(merged.keys())})")

    # ── Also generate eraclea supplement (Amazon-only brand) ─────────────────
    try:
        eraclea_supplement = extract_brand(data, 'eraclea')
        eraclea_supplement['searchTermInsights'] = build_search_term_insights(
            eraclea_supplement.get('search_terms', []))
        eraclea_supplement['productInsights'] = build_asin_insights(
            eraclea_supplement.get('asins', []))
        eraclea_supplement['placementInsights'] = eraclea_supplement.get('placements', [])

        eraclea_out = out_dir / 'eraclea_supplement.json'
        eraclea_out.write_text(json.dumps(eraclea_supplement, indent=2))
        print(f"\n✓ Wrote {eraclea_out}")

        no_google = out_dir / '_no_google.json'   # intentionally absent
        no_manual = out_dir / '_no_manual.json'   # intentionally absent

        # eraclea_daily_archive.json FIRST — eraclea's monthly archive is
        # corrected from this below, same as Skinuva's.
        eraclea_daily_path = out_dir / 'eraclea_daily_archive.json'
        eraclea_new_daily = build_daily_archive(eraclea_supplement, no_google)
        existing_eraclea_daily = {}
        if eraclea_daily_path.exists():
            try:
                existing_eraclea_daily = json.loads(eraclea_daily_path.read_text())
            except Exception:
                pass
        merged_eraclea_daily = merge_daily_archive(existing_eraclea_daily, eraclea_new_daily)
        eraclea_daily_path.write_text(json.dumps(merged_eraclea_daily, indent=2))
        print(f"✓ Updated {eraclea_daily_path} ({len(merged_eraclea_daily)} days)")

        # eraclea_monthly.json (Amazon-only, no Google/Shopify paths)
        eraclea_monthly_path = out_dir / 'eraclea_monthly.json'
        # include_dtc=False — eraclea is Amazon-only. The sentinel paths alone did NOT
        # suppress Shopify/Walmart (they were re-derived from manual_path.parent), so
        # eraclea was inheriting Skinuva's DTC revenue.
        eraclea_new_monthly = build_monthly_archive(eraclea_supplement, no_google, no_manual,
                                                   include_dtc=False)
        existing_eraclea = {}
        if eraclea_monthly_path.exists():
            try:
                existing_eraclea = json.loads(eraclea_monthly_path.read_text())
            except Exception:
                pass
        merged_eraclea = {**existing_eraclea, **eraclea_new_monthly}
        merged_eraclea = dict(sorted(merged_eraclea.items(), key=lambda x: datetime.strptime(x[0], '%B %Y')))

        print("\nCorrecting eraclea completed months' spend/orders from complete daily archive…")
        correct_monthly_from_archive(merged_eraclea, merged_eraclea_daily, today_month_str)

        print("\nBackfilling eraclea-specific totalSales from SP-API (portfolio-wide fix)…")
        backfill_brand_total_sales(merged_eraclea, merged_eraclea_daily, 'eraclea', cfg,
                                   today_month_str, existing=existing_eraclea,
                                   fetch_cache=ts_fetch_cache)

        apply_refunds(merged_eraclea, 'eraclea', Path(__file__).parent)
        eraclea_monthly_path.write_text(json.dumps(merged_eraclea, indent=2))
        print(f"✓ Updated {eraclea_monthly_path} ({len(merged_eraclea)} months)")

    except ValueError as e:
        print(f"  ⚠  No eraclea data in ads_data.js: {e}")

    print("\nNext steps:")
    print("  git add skinuva/data/")
    print("  git commit -m 'chore: refresh Skinuva + eraclea ad data'")
    print("  git push  →  Vercel auto-deploys")


if __name__ == '__main__':
    main()
