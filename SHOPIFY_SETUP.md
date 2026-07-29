# Shopify Integration Setup

One-time setup to stop entering the Shopify number by hand each morning.
Takes about 10 minutes. **Do not send the access token to anyone, including
Claude** — it goes straight into the GitHub secret.

---

## Step 1 — Create a Shopify custom app

1. In Shopify admin: **Settings → Apps and sales channels → Develop apps**
2. Click **Create an app**, name it something like `Medaltus Dashboard`
3. Click **Configure Admin API scopes** and enable:
   - `read_orders` — required
   - `read_all_orders` — only if you want months older than 60 days (Shopify
     restricts older orders without it)
4. **Save**, then **Install app**
5. Under **API credentials**, reveal and copy the **Admin API access token**
   (starts with `shpat_`). It's shown once.

---

## Step 2 — Add it to the GitHub secret

The pipeline reads credentials from one secret called `CONFIGJSON`.

1. Go to the repo → **Settings → Secrets and variables → Actions**
2. Edit the existing **`CONFIGJSON`** secret
3. Add these two keys to the JSON (keep everything already in there):

```json
{
  "shopify_store_domain": "your-store.myshopify.com",
  "shopify_access_token": "shpat_xxxxxxxxxxxxxxxxxxxxx"
}
```

- `shopify_store_domain` is the `.myshopify.com` address, **not** your custom
  domain. Find it under Settings → Domains.
- Optionally add `"shopify_api_version": "2026-07"` to pin the API version.
  Left out, the script asks Shopify for the newest supported version.

4. Save.

---

## Step 3 — Confirm the number matches

The next scheduled run picks it up automatically. Nothing else to do.

Shopify's Analytics "Total sales" is a composite
(gross − discounts − returns + taxes + shipping + duties) that no single API
field reproduces exactly, so the first run prints several candidate
definitions side by side against the value you'd entered manually:

```
  ✓ [July 2026] net_current = $46,812.30  (412 orders, through 2026-07-28)
      variants for July 2026:
        net_current        $   46,812.30   <-- matches the manual value
        gross_original     $   48,904.11
        subtotal_current   $   43,190.02
        refunded           $    1,204.55
        discounts          $    2,881.40
        MANUAL (current)   $   46,799.51
```

Check which variant matches what you've been entering. If it's not
`net_current`, tell Claude which one it is and it's a one-line change
(`PRIMARY_VARIANT` in `skinuva/fetch_shopify_sales.py`).

You can also run it locally any time to see the comparison:

```bash
cd ~/Documents/Claude/Projects/All\ Brands\ Ad\ Dashboard/deploy
python3 skinuva/fetch_shopify_sales.py --compare
```

---

## How it behaves

- Refreshes the **current and previous month** each run, so late refunds and
  edits are picked up rather than frozen at first fetch.
- The in-progress month is counted **through yesterday**, so the number
  doesn't drift during the day.
- Writes `skinuva/data/shopify_sales.json`.
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
