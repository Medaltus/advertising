#!/usr/bin/env python3
"""
fetch_refunds.py — per-brand Amazon refunds by month.

WHY
---
The dashboards' Total Sales is SP-API `orderedProductSales`, measured when the order
is PLACED with nothing deducted for returns. Refunds run 2-5% of revenue every month,
so gross Total Sales overstates realised revenue and TACOS flatters efficiency by
roughly 0.4pp. The dashboard shows Refunds and Net Sales rows; this is what keeps them
current.

WHY THE FINANCES API AND NOT A REPORT
-------------------------------------
Every other Amazon pull here goes through the Reports API: submit, poll, download.
Those submissions are quota-limited per account, and exhausting that quota is what
silently wiped June 2026's total sales for a week. The Finances API is a direct
paginated GET — no submission, no report quota — so adding refunds costs nothing
against the budget that has actually been breaking things.

OUTPUT
------
data/refunds_by_month.json
    { "July 2026": { "Skinuva": 4979.98, "eraclea": 93.69 }, ... }

Merged with whatever is already there, so a partial or failed run cannot erase
history. Amounts are positive (the API reports refunds as negative principal).

USAGE
-----
    python3 fetch_refunds.py                 # last 3 months
    python3 fetch_refunds.py --months 14     # backfill further
    python3 fetch_refunds.py --dry-run
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))

try:
    import requests
except ImportError:
    print("Missing dependency: pip3 install requests --break-system-packages")
    sys.exit(1)

try:
    from fetch_total_sales import (
        get_sp_access_token, sigv4_headers, _request_with_backoff,
        ABBREV_TO_BRAND, SPAPI_HOST,
    )
except Exception as e:                                            # pragma: no cover
    print(f"✗ Could not import from fetch_total_sales: {e}")
    sys.exit(1)

MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
               'July', 'August', 'September', 'October', 'November', 'December']

OUT_PATH = Path(__file__).parent / 'data' / 'refunds_by_month.json'


def month_key(d: date) -> str:
    return f"{MONTH_NAMES[d.month - 1]} {d.year}"


def brand_for_sku(sku: str):
    """Same rule the sales pipeline uses: first 3 characters of the seller SKU."""
    if not sku:
        return None
    return ABBREV_TO_BRAND.get(str(sku).strip().upper()[:3])


def _principal(adj: dict) -> float:
    """
    Sum the Principal component of one shipment-item adjustment.

    Only Principal is counted — deliberately. The refund event also carries fee
    reversals (commission credited back, FBA fees) which are cost recoveries, not
    negative revenue. Including them would understate refunds against sales.
    """
    total = 0.0
    for charge in (adj.get('ItemChargeAdjustmentList') or []):
        if (charge.get('ChargeType') or '') != 'Principal':
            continue
        amt = (charge.get('ChargeAmount') or {}).get('CurrencyAmount')
        if isinstance(amt, (int, float)):
            total += float(amt)
    return total


def fetch_refund_events(cfg, start: date, end: date) -> list:
    """All RefundEvent objects posted in [start, end]. Paginated."""
    events, next_token, page = [], None, 0
    while True:
        page += 1
        path = "/finances/v0/financialEvents"
        if next_token:
            qs = f"NextToken={requests.utils.quote(next_token, safe='')}"
        else:
            # RFC3986-encode the values. SigV4 signs the CANONICAL query string, which
            # requires ':' in these ISO timestamps to appear as %3A -- and
            # sigv4_headers() does no encoding of its own (every other call site in this
            # codebase passes qs=""). Sending raw colons produces a signature mismatch,
            # i.e. a 403 on every run, while apply_refunds() quietly preserves the last
            # good values so the dashboard looks fine and the numbers never move.
            # Keys stay in alphabetical order, which SigV4 also requires.
            _q = lambda v: requests.utils.quote(str(v), safe='')
            qs = ("MaxResultsPerPage=100"
                  f"&PostedAfter={_q(start.isoformat() + 'T00:00:00Z')}"
                  f"&PostedBefore={_q((end + timedelta(days=1)).isoformat() + 'T00:00:00Z')}")
        token = get_sp_access_token(cfg, "sp_refresh_token")
        resp = _request_with_backoff(
            "GET",
            f"https://{SPAPI_HOST}{path}?{qs}",
            headers=sigv4_headers("GET", path, qs, token, "", cfg),
            timeout=40,
        )
        if not resp.ok:
            raise RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
        payload = (resp.json().get('payload') or {})
        fe = payload.get('FinancialEvents') or {}
        batch = fe.get('RefundEventList') or []
        events.extend(batch)
        next_token = payload.get('NextToken')
        print(f"    page {page}: {len(batch)} refund events "
              f"({len(events)} total){' …more' if next_token else ''}")
        if not next_token:
            break
        # Finances API is 0.5 req/sec sustained.
        time.sleep(2.5)
    return events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=str(Path(__file__).parent / 'config.json'))
    ap.add_argument('--months', type=int, default=3,
                    help="How many months back to (re)fetch. Default 3 — refunds "
                         "trail sales, so recent months keep moving.")
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"✗ {cfg_path} not found — refunds not fetched")
        return 1
    cfg = json.loads(cfg_path.read_text())
    if not cfg.get('sp_refresh_token'):
        print("✗ no sp_refresh_token in config — refunds not fetched")
        return 1

    today = date.today()
    first_of_this = today.replace(day=1)
    start = first_of_this
    for _ in range(args.months - 1):
        start = (start - timedelta(days=1)).replace(day=1)
    end = today - timedelta(days=1)
    # On the 1st of a month, `end` is the last day of the PREVIOUS month while `start` is
    # today, so the window is inverted and empty. Step back one more month.
    if start > end:
        start = (start - timedelta(days=1)).replace(day=1)

    print(f"\n{'═'*62}")
    print(f"  Amazon refunds — {month_key(start)} → {month_key(end)}")
    print(f"  Finances API (no report quota consumed)")
    print(f"{'═'*62}\n")

    try:
        events = fetch_refund_events(cfg, start, end)
    except Exception as e:
        print(f"  ✗ refund fetch failed: {e}")
        return 1

    by_month = defaultdict(lambda: defaultdict(float))
    unmapped = defaultdict(float)
    for ev in events:
        posted = ev.get('PostedDate') or ''
        try:
            d = datetime.fromisoformat(posted.replace('Z', '+00:00')).date()
        except Exception:
            continue
        mk = month_key(d)
        for adj in (ev.get('ShipmentItemAdjustmentList') or []):
            amt = _principal(adj)
            if amt == 0:
                continue
            brand = brand_for_sku(adj.get('SellerSKU'))
            # Refunds arrive as NEGATIVE principal, so negate to get a positive refund.
            #
            # This was abs(), which is wrong: refund REVERSALS (chargeback reversals,
            # returnless-refund corrections) post as POSITIVE principal, and abs() added
            # them as extra refunds instead of netting them off. A $50 refund that
            # reverses the following week reported $100 of refunds against a true $0,
            # understating Net Sales and overstating Net TACOS.
            if brand:
                by_month[mk][brand] += -amt
            else:
                unmapped[mk] += -amt

    if not by_month:
        # Not success. Refunds run 2-5% of revenue EVERY month, so a clean empty result
        # across a multi-month window means the request or the parse is broken, not that
        # nobody returned anything. Returning 0 here made a broken pull look fine while
        # apply_refunds() preserved the previous values, so the dashboard kept showing
        # stale refunds that never moved.
        print(f"\n  ✗ no refund events found across {args.months} month(s) — treating as "
              f"a FAILED pull, not a genuine zero. Nothing written; existing values kept.")
        return 1

    print(f"\n  {'month':16}{'brand':16}{'refunds':>12}")
    print("  " + "-" * 44)
    for mk in sorted(by_month, key=lambda m: (int(m.split()[1]),
                                              MONTH_NAMES.index(m.split()[0]))):
        for brand, amt in sorted(by_month[mk].items(), key=lambda x: -x[1]):
            print(f"  {mk:16}{brand:16}{amt:>12,.2f}")
        if unmapped.get(mk):
            print(f"  {mk:16}{'(unmapped SKU)':16}{unmapped[mk]:>12,.2f}")

    if args.dry_run:
        print("\n  DRY RUN — nothing written")
        return 0

    # Merge, never replace: a short --months window must not erase older history.
    existing = {}
    if OUT_PATH.exists():
        try:
            existing = json.loads(OUT_PATH.read_text())
        except Exception:
            existing = {}
    # Write an explicit 0.00 for tracked brands with no refund events this month.
    #
    # Previously a brand-month with genuinely zero refunds got no key at all, which the
    # dashboard cannot distinguish from "we never fetched this" — so it fell back to
    # GROSS TACOS for that month while its neighbours showed NET, and the trend line
    # silently mixed two different metrics. The pull succeeded, so absence is a real zero.
    _tracked = sorted(set(ABBREV_TO_BRAND.values()))
    for mk, brands in by_month.items():
        row = {b: round(v, 2) for b, v in brands.items()}
        for b in _tracked:
            row.setdefault(b, 0.0)
        existing[mk] = row
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(existing, indent=2))
    print(f"\n  ✓ wrote {OUT_PATH} ({len(existing)} months)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
