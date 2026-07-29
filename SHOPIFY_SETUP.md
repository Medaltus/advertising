# Shopify Integration Setup

One-time setup to stop entering the Shopify number by hand each morning.
Takes about 10 minutes. **Do not send the access token to anyone, including
Claude** — it goes straight into the GitHub secret.

---

## Step 1 — Create a Shopify custom app

1. In Shopify admin: **Settings → Apps and sales channels → Develop apps**
2. Click **Create an app**, name it something like `Medaltus Dashboard`
3. Click **Configure Admin API scopes** and enable:
   - **`read_reports`** — required (this is what allows the analytics query)
4. **Save**, then **Install app**
5. Under **API credentials**, reveal and copy the **Admin API access token**
   (starts with `shpat_`). It's shown once.

> `read_reports` is the only scope needed. The script runs the same analytics
> query the report uses, not a raw order dump, so it doesn't need order or
> customer scopes.

---

## Step 2 — Add it to the GitHub secret

The pipeline reads credentials from one secret called `CONFIGJSON`.

1. Go to the repo → **Settings → Secrets and variables → Actions**
2. Edit the existing **`CONFIGJSON`** secret
3. Add these two keys to the JSON (keep everything already in there):

```json
{
  "shopify_store_domain": "http-skinuva-com.myshopify.com",
  "shopify_access_token": "shpat_xxxxxxxxxxxxxxxxxxxxx"
}
```

- `shopify_store_domain` must be the `.myshopify.com` address, **not**
  `skinuva.com`. For this store it's `http-skinuva-com.myshopify.com`
  (confirmed via the API — the odd-looking name is correct).
- Optionally add `"shopify_api_version": "2026-07"` to pin the version.
  Left out, the script asks Shopify for the newest supported one.
- Optionally add `"shopify_excluded_channels": ["Draft Orders"]` to change
  which channels are filtered out. That default already matches your report.

4. Save.

---

## Step 3 — What it's reproducing (already verified)

It runs the same query behind your **Total sales by sales channel** report
with sales channel "is not" Draft Orders:

```
FROM sales SHOW total_sales GROUP BY sales_channel SINCE <start> UNTIL <end>
```

then sums every channel except Draft Orders. This was checked against your
hand-entered numbers on two months and matches **to the cent**:

| Month | Online Store | Seal Subs | Shop | TikTok | Total | You entered |
|---|---|---|---|---|---|---|
| July 2026 (1–28) | 41,898.94 | 3,479.70 | 879.56 | 541.31 | **46,799.51** | 46,799.51 ✓ |
| June 2026 (1–30) | 37,776.20 | 1,967.39 | 940.87 | 172.28 | **40,856.74** | 40,856.74 ✓ |

Draft Orders runs ~$200K/month on this store, so excluding it is the entire
point — counting it would overstate the figure roughly 5x.

Nothing to confirm after setup. To see the breakdown any time:

```bash
cd ~/Documents/Claude/Projects/All\ Brands\ Ad\ Dashboard/deploy
python3 skinuva/fetch_shopify_sales.py --dry-run
```

---

## How it behaves

- Refreshes the **current and previous month** each run, so late refunds and
  edits are picked up rather than frozen at first fetch.
- The in-progress month is counted **through yesterday in your store's
  timezone** (US/Pacific), not the server's UTC. Without that the early-morning
  run would ask for a day that hadn't finished yet and the number would shift
  between the two daily runs.
- Writes `skinuva/data/shopify_sales.json`, including the full per-channel
  breakdown and the excluded total, so any figure on the dashboard can be
  traced back to its channels.
- `generate_skinuva_supplement.py` **prefers the API value** and falls back to
  your manual `manual_totals.json` value whenever the API value is missing —
  so a Shopify outage, an expired token, or a month predating this integration
  all keep working instead of dropping to zero.
- If the API and manual values disagree by more than 1%, it logs both.
- Walmart is still manual; there's no API wired up for it.

## After it's confirmed working

`./update-skinuva-sales.sh shopify <amount>` becomes unnecessary. It still
works as a manual override if you ever need one, but the API value wins on the
next scheduled run.
