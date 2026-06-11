# Skinuva Ad Dashboard — Vercel Setup

One-time setup to deploy the Skinuva dashboard to Vercel. It lives in the same GitHub repo
(`Medaltus/advertising`) as the All Brands dashboard but as a **separate Vercel project**
pointing to the `skinuva/` subfolder.

---

## What you need before starting

- Access to the Vercel account that has the All Brands dashboard
- The `GOOGLE_SERVICE_ACCOUNT_JSON` secret (same one used by All Brands — it already has
  access to the Skinuva sheet)
- Access to `github.com/Medaltus/advertising`

---

## Step 1 — Push the Skinuva files to GitHub

From your terminal, `cd` into the `deploy/` folder and push:

```bash
cd "/Users/SeanDeAvies/Documents/Claude/Projects/All Brands Ad Dashboard/deploy"
git add skinuva/
git add .github/workflows/morning-refresh.yml
git commit -m "feat: add Skinuva Vercel dashboard"
git push
```

---

## Step 2 — Create a new Vercel project for Skinuva

1. Go to **vercel.com** → **Add New Project**
2. Import the same GitHub repo: **Medaltus/advertising**
3. On the configuration screen, expand **Root Directory** and set it to:
   ```
   skinuva
   ```
4. Leave Framework Preset as **Other**
5. Leave Build & Output settings blank (no build step — it's static HTML + serverless)
6. Click **Deploy**

The first deploy will fail because env vars aren't set yet — that's fine, continue to Step 3.

---

## Step 3 — Add environment variables in Vercel

In the new Skinuva Vercel project, go to **Settings → Environment Variables** and add:

| Name | Value |
|------|-------|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | *(paste the full JSON key — same value as All Brands)* |
| `ANTHROPIC_API_KEY` | *(your Anthropic API key — same value as All Brands)* |

Set both to apply to **Production**, **Preview**, and **Development**.

---

## Step 4 — Redeploy

After adding env vars:

1. Go to **Deployments** tab in the Skinuva Vercel project
2. Click the three dots (⋯) next to the latest deployment → **Redeploy**

The dashboard should load at your new Vercel URL (e.g. `skinuva-ad-dashboard.vercel.app`).

---

## Step 5 — Verify daily automation

The GitHub Actions workflow (`morning-refresh.yml`) already includes Skinuva. It runs every
morning at 7 AM ET and now updates **both** dashboards in the same commit:

- `data/api_supplement.json` → All Brands
- `skinuva/data/skinuva_supplement.json` → Skinuva Amazon
- `skinuva/data/google_ads_data.json` → Skinuva Google Ads

When GitHub Actions pushes those files, Vercel auto-deploys both projects.

To confirm it's working, go to your GitHub repo → **Actions** tab and check that the next
morning run succeeds and shows all three data files in the commit.

---

## Updating manual totals (Walmart + Shopify)

Edit `skinuva/data/manual_totals.json` each month when you have final numbers:

```json
{
  "June 2026": {
    "walmart": 485.00,
    "shopify": 12673.48
  },
  "July 2026": {
    "walmart": 0,
    "shopify": 0
  }
}
```

Amazon total sales come automatically from the SP-API timeline data — no manual entry needed.

Commit and push after editing:

```bash
git add skinuva/data/manual_totals.json
git commit -m "chore: update Skinuva manual totals"
git push
```

---

## Troubleshooting

**Dashboard loads but shows no data**
→ Check the browser console for fetch errors on `/api/sheets` or `/data/*.json`

**"Sheet load failed"**
→ Verify `GOOGLE_SERVICE_ACCOUNT_JSON` is set correctly in Vercel env vars

**AI agents don't work**
→ Verify `ANTHROPIC_API_KEY` is set in Vercel env vars

**GitHub Actions shows Skinuva steps failing**
→ Check the Actions log — Google Ads credentials are hardcoded in `fetch_google_ads.py`
  so no secret setup is needed for that step
