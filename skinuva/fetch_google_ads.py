#!/usr/bin/env python3
"""
Google Ads Data Fetcher — Skinuva (Vercel deploy version)
Fetches campaign performance data via the Google Ads REST API.
Outputs data/google_ads_data.json (pure JSON, no JS wrapper).

Usage:
  python3 fetch_google_ads.py
  python3 fetch_google_ads.py --days 31
"""

import json
import time
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing dependency: run  pip3 install requests --break-system-packages  and try again.")
    sys.exit(1)

# ── Credentials (loaded from config.json) ────────────────────────────────────
def _load_gads_config() -> dict:
    script_dir = Path(__file__).parent.resolve()
    for cfg_path in [script_dir.parent / 'config.json', script_dir / 'config.json']:
        if cfg_path.exists():
            with open(cfg_path) as f:
                return json.load(f).get('skinuva_google_ads', {})
    print("✗ config.json not found. Add skinuva_google_ads credentials to config.json.")
    sys.exit(1)

_cfg = _load_gads_config()
DEVELOPER_TOKEN = _cfg['developer_token']
CLIENT_ID       = _cfg['client_id']
CLIENT_SECRET   = _cfg['client_secret']
REFRESH_TOKEN   = _cfg['refresh_token']
CUSTOMER_ID     = _cfg['customer_id']   # Skinuva (no dashes)
MANAGER_ID      = _cfg['manager_id']    # Manager account (no dashes)

API_VERSION     = "v23"
API_BASE        = f"https://googleads.googleapis.com/{API_VERSION}"

# ── Auth ──────────────────────────────────────────────────────────────────────

def get_access_token() -> str:
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type":    "refresh_token",
            "refresh_token": REFRESH_TOKEN,
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        timeout=20,
    )
    resp.raise_for_status()
    print("✓ Access token obtained")
    return resp.json()["access_token"]


def api_headers(access_token: str) -> dict:
    return {
        "Authorization":      f"Bearer {access_token}",
        "developer-token":    DEVELOPER_TOKEN,
        "login-customer-id":  MANAGER_ID,
        "Content-Type":       "application/json",
    }


# ── Query ─────────────────────────────────────────────────────────────────────

def fetch_all_pages(access_token: str, query: str) -> list:
    rows, page_token = [], None
    while True:
        payload = {"query": query}
        if page_token:
            payload["pageToken"] = page_token
        url = f"{API_BASE}/customers/{CUSTOMER_ID}/googleAds:search"
        resp = requests.post(
            url,
            headers=api_headers(access_token),
            json=payload,
            timeout=60,
        )
        if not resp.ok:
            try:
                err_body = resp.json()
                err_msg  = err_body.get("error", {}).get("message", resp.text[:500])
            except Exception:
                err_msg = resp.text[:500]

            if "developer token" in err_msg.lower() and "not approved" in err_msg.lower():
                print("\n⚠  Developer token not yet approved for production access.")
                sys.exit(1)
            if "Google Ads API has not been used" in err_msg or "PERMISSION_DENIED" in err_msg:
                print("\n⚠  The Google Ads API is not enabled in your Google Cloud project.")
                sys.exit(1)
            if "not authorized" in err_msg.lower() or "oauth" in err_msg.lower():
                print(f"\n⚠  Authorization error: {err_msg}")
                sys.exit(1)

            raise RuntimeError(f"API error {resp.status_code}: {err_msg}")

        data = resp.json()
        rows.extend(data.get("results", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return rows


# ── Helpers ───────────────────────────────────────────────────────────────────

def micros(v) -> float:
    return round(int(v or 0) / 1_000_000, 2)

def safe_div(a, b, d=4):
    return round(a / b, d) if b else None

def acos_pct(spend, sales):
    return round(spend / sales * 100, 2) if sales else None

def roas_val(sales, spend):
    return round(sales / spend, 2) if spend else None

def date_range(lookback_days: int):
    end   = date.today() - timedelta(days=1)
    start = end - timedelta(days=lookback_days - 1)
    return start.isoformat(), end.isoformat()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=31,
                        help="Lookback days (default 31 to match Amazon)")
    args = parser.parse_args()

    start, end = date_range(args.days)

    print(f"\n{'═'*60}")
    print(f"  Google Ads Fetcher  |  Skinuva  |  lookback={args.days}d")
    print(f"  Date range: {start} → {end}")
    print(f"{'═'*60}\n")

    access_token = get_access_token()

    print("Fetching campaign performance...")
    query = f"""
        SELECT
          campaign.id,
          campaign.name,
          campaign.status,
          campaign.advertising_channel_type,
          campaign_budget.amount_micros,
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
        ORDER BY segments.date ASC
    """

    rows = fetch_all_pages(access_token, query)
    print(f"  ✓ {len(rows)} campaign-day rows")

    by_date, by_campaign, budgets = {}, {}, {}

    for r in rows:
        seg  = r.get("segments", {})
        camp = r.get("campaign", {})
        met  = r.get("metrics", {})
        bud  = r.get("campaignBudget", {})

        d     = seg.get("date", "")
        cid   = camp.get("id", "")
        cname = camp.get("name", "")
        imp   = int(met.get("impressions", 0) or 0)
        clk   = int(met.get("clicks", 0) or 0)
        spd   = micros(met.get("costMicros", 0))
        conv  = float(met.get("conversions", 0) or 0)
        rev   = float(met.get("conversionsValue", 0) or 0)

        if d:
            if d not in by_date:
                by_date[d] = {"date": d, "impressions": 0, "clicks": 0,
                               "spend": 0.0, "sales": 0.0, "conversions": 0.0}
            by_date[d]["impressions"] += imp
            by_date[d]["clicks"]      += clk
            by_date[d]["spend"]       += spd
            by_date[d]["sales"]       += rev
            by_date[d]["conversions"] += conv

        if cid:
            if cid not in by_campaign:
                by_campaign[cid] = {
                    "id": cid, "name": cname,
                    "type": camp.get("advertisingChannelType", ""),
                    "impressions": 0, "clicks": 0,
                    "spend": 0.0, "sales": 0.0, "conversions": 0.0,
                }
            by_campaign[cid]["impressions"] += imp
            by_campaign[cid]["clicks"]      += clk
            by_campaign[cid]["spend"]       += spd
            by_campaign[cid]["sales"]       += rev
            by_campaign[cid]["conversions"] += conv

            budget_micros = bud.get("amountMicros", 0)
            if budget_micros:
                budgets[cid] = {
                    "id":          cid,
                    "name":        cname,
                    "state":       camp.get("status", "").lower(),
                    "dailyBudget": micros(budget_micros),
                }

    campaigns = []
    for c in by_campaign.values():
        c["spend"] = round(c["spend"], 2)
        c["sales"] = round(c["sales"], 2)
        c["cpc"]   = safe_div(c["spend"], c["clicks"])
        c["acos"]  = acos_pct(c["spend"], c["sales"])
        c["roas"]  = roas_val(c["sales"], c["spend"])
        campaigns.append(c)
    campaigns.sort(key=lambda x: x["spend"], reverse=True)

    timeline = []
    for row in sorted(by_date.values(), key=lambda x: x["date"]):
        row["spend"] = round(row["spend"], 2)
        row["sales"] = round(row["sales"], 2)
        row["conversions"] = round(row["conversions"], 1)
        row["acos"] = acos_pct(row["spend"], row["sales"])
        row["roas"] = roas_val(row["sales"], row["spend"])
        timeline.append(row)

    pacing = []
    days = max(len(by_date), 1)
    for cid, b in budgets.items():
        avg = by_campaign.get(cid, {}).get("spend", 0) / days
        pct = round(avg / b["dailyBudget"] * 100, 1) if b["dailyBudget"] else None
        pacing.append({**b, "recentSpend": round(avg, 2), "pacingPct": pct})
    pacing.sort(key=lambda x: x.get("dailyBudget") or 0, reverse=True)

    imp  = sum(r["impressions"] for r in timeline)
    clk  = sum(r["clicks"]     for r in timeline)
    spd  = round(sum(r["spend"]  for r in timeline), 2)
    sls  = round(sum(r["sales"]  for r in timeline), 2)
    conv = round(sum(r["conversions"] for r in timeline), 0)

    summary = {
        "impressions": imp, "clicks": clk, "spend": spd,
        "sales": sls, "conversions": int(conv),
        "ctr":  safe_div(clk, imp),
        "cpc":  safe_div(spd, clk),
        "acos": acos_pct(spd, sls),
        "roas": roas_val(sls, spd),
    }

    output = {
        "fetched_at":    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lookback_days": args.days,
        "currency":      "USD",
        "summary":       summary,
        "timeline":      timeline,
        "campaigns":     campaigns[:50],
        "pacing":        pacing,
    }

    # Write pure JSON to data/ directory (served by Vercel at /data/google_ads_data.json)
    out_dir = Path(__file__).parent / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "google_ads_data.json"
    out_path.write_text(json.dumps(output, indent=2))

    print(f"\n{'═'*60}")
    print(f"  ✓ Written to {out_path}")
    print(f"  Summary: spend=${spd:,.0f}, sales=${sls:,.0f}, "
          f"acos={summary['acos']}%, roas={summary['roas']}x, "
          f"conversions={int(conv)}")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
