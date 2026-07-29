# Shopify Integration Setup

One-time setup to stop entering the Shopify number by hand each morning.
Takes about 10 minutes. **Do not send the access token to anyone, including
Claude** — it goes straight into the GitHub secret.

---

## Important: the old "custom app" route is gone

> **As of January 1, 2026 Shopify no longer allows new legacy custom apps.**

So there is no static `shpat_` token to copy any more. New apps live in the
**Dev Dashboard** and you exchange a Client ID + Secret for a short-lived
token. Per Shopify's docs:

> "Public and custom apps created in the Dev Dashboard generate tokens using
> OAuth, and custom apps made in the Shopify admin are authenticated in the
> Shopify admin."

We use the **client credentials grant** — the option Shopify documents for
"trusted, server-to-server integrations owned by your organization." Nothing
long-lived is stored; the script requests a fresh 24-hour token each run.

---

## Step 1 — Create the app in the Dev Dashboard

1. Go to **dev.shopify.com/dashboard** (or store admin → your name, top right
   → **Dev Dashboard**).
2. **Apps → Create app**, name it `Medaltus Dashboard`.
3. You'll land on a **Create version** screen. In the **API access → Scopes**
   box, enter exactly:

   ```
   read_reports
   ```

   That's the only scope needed — the script runs the analytics query behind
   your report, not a raw order dump, so no order or customer scopes.

   Leave App URL / Redirect URLs / Optional scopes / POS / App proxy alone.
   They're only used for apps you distribute to other merchants.
4. Click **Release**.

## Step 2 — Install it on the Skinuva store

The token exchange only works once the app is installed. Install it on
`skinuva.com` from the Dev Dashboard.

**The app and the store must be in the same Shopify organization** (Ozlee
Brands). If they aren't, Shopify returns `shop_not_permitted` and the script
will tell you so explicitly.

## Step 3 — Copy the client credentials

Dev Dashboard → your app → **Settings** → copy **Client ID** and
**Client secret**.

Note there is no access token on this page — that's expected. You request
tokens programmatically with these two values.

---

## Step 4 — Add a NEW GitHub secret (do not edit CONFIGJSON)

> **Do not click the pencil on `CONFIGJSON`.** GitHub secrets are write-only —
> you can't read an existing value back. Editing it opens a blank box, and
> whatever you paste **replaces the whole secret**, which would wipe every
> Amazon and Google credential in there.

Instead add a second, separate secret. The workflow merges the two at runtime,
so this is purely additive and `CONFIGJSON` is never touched.

1. Repo → **Settings → Secrets and variables → Actions**
2. Click the green **New repository secret**
3. **Name:** exactly

   ```
   SHOPIFY_CONFIG
   ```

4. **Secret:** paste this, substituting your two values from Step 3:

```json
{
  "shopify_store_domain": "http-skinuva-com.myshopify.com",
  "shopify_client_id": "your-client-id",
  "shopify_client_secret": "your-client-secret"
}
```

5. **Add secret**.

Notes:

- `shopify_store_domain` must be the `.myshopify.com` address, **not**
  `skinuva.com`. For this store it's `http-skinuva-com.myshopify.com`
  (confirmed via the API — the odd-looking name is correct). If you paste it
  without `.myshopify.com` the script appends it.
- Optionally add `"shopify_api_version": "2026-07"` to pin the version. Left
  out, the script asks Shopify for the newest supported one.
- Optionally add `"shopify_excluded_channels": ["Draft Orders"]` to change
  which channels are filtered out. That default already matches your report.

If `SHOPIFY_CONFIG` is missing, empty, or malformed, the workflow logs a
warning and carries on using the manual `manual_totals.json` values — it can't
break the rest of the refresh. Only the key *names* are ever logged, never the
values.

Keep the client secret in the GitHub secret only. Don't paste it into chat.

---

## Step 5 — What it's reproducing (already verified)

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
