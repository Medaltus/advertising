# How to Get the Dashboard Online

This guide gets your dashboard live on the internet using GitHub (stores your code) and Vercel (hosts the website). You'll do this once, then updating it is just two commands.

**Time to complete:** ~30 minutes

---

## Before You Start — Install Two Things

Open Terminal and run these one at a time. Paste each line, press Enter, wait for it to finish.

**Install Node.js** (if you don't have it):
Download from https://nodejs.org — click the "LTS" version button and run the installer.

**Install the Vercel tool:**
```bash
npm install -g vercel
```

---

## Step 1 — Create a Free GitHub Account (if you don't have one)

Go to https://github.com and sign up. GitHub is where your code lives — think of it like a Google Drive for code.

---

## Step 2 — Create a New Repo on GitHub

A "repo" is just a folder on GitHub.

1. Go to https://github.com/new
2. Under "Repository name" type: `medaltus-ad-dashboard`
3. Click **Private**
4. **Do not** check any of the "Initialize this repository" boxes
5. Click **Create repository**

GitHub will show you a page with setup instructions — leave that tab open.

---

## Step 3 — Upload Your Files to GitHub

Open Terminal and paste these commands one at a time:

```bash
cd "/Users/SeanDeAvies/Documents/Claude/Projects/All Brands Ad Dashboard/deploy"
```
*(This opens the deploy folder in Terminal)*

```bash
git init
```
*(Sets up git tracking in the folder)*

```bash
git add .
```
*(Selects all files to upload)*

```bash
git commit -m "first upload"
```
*(Packages the files)*

```bash
git branch -M main
```

Now, on the GitHub page you left open, copy the line that looks like this (yours will have your username):
```
git remote add origin https://github.com/YOUR_USERNAME/medaltus-ad-dashboard.git
```
Paste it in Terminal and press Enter.

Then run:
```bash
git push -u origin main
```

It will ask for your GitHub username and password. For the password, GitHub requires a "Personal Access Token" — not your actual password. To get one:
1. Go to https://github.com/settings/tokens
2. Click **Generate new token (classic)**
3. Give it a name, set expiration to "No expiration"
4. Check the **repo** checkbox
5. Click **Generate token** at the bottom
6. Copy the token and paste it as your password in Terminal

Refresh your GitHub repo page — your files should now be there.

---

## Step 4 — Create a Free Vercel Account

Go to https://vercel.com and sign up. **Sign up with GitHub** so they're connected.

---

## Step 5 — Deploy to Vercel

In Terminal (still in the deploy folder), run:

```bash
vercel
```

It will ask a few questions — answer them like this:
- **Set up and deploy?** → press Enter (Yes)
- **Which scope?** → press Enter (your account)
- **Link to existing project?** → type `n` and press Enter
- **What's your project's name?** → press Enter (keeps the folder name)
- **In which directory is your code located?** → press Enter (current folder)
- **Want to modify these settings?** → type `n` and press Enter

Vercel will deploy. It'll give you a URL like `https://medaltus-ad-dashboard.vercel.app` — the dashboard won't fully work yet because we need to add the passwords in Step 6.

---

## Step 6 — Add Your Passwords (Environment Variables)

Vercel needs three "secrets" to connect to Google Sheets and the AI. These are called environment variables — they're like passwords stored safely in Vercel, never in your code.

Go to https://vercel.com → click your project → click **Settings** → click **Environment Variables**.

Add these three, one at a time (click **Add** after each):

---

**Variable 1: `ANTHROPIC_API_KEY`**

Go to https://console.anthropic.com/ → click **API Keys** → **Create Key** → copy it.
Paste it as the value.

---

**Variable 2: `GOOGLE_SERVICE_ACCOUNT_JSON`**

This is the trickiest one. It lets Vercel read your Google Sheet.

1. Go to https://console.cloud.google.com/
2. At the top, click the project dropdown → **New Project** → name it anything → **Create**
3. In the search bar at the top, search `Google Sheets API` → click it → click **Enable**
4. In the left menu: **APIs & Services** → **Credentials** → **+ Create Credentials** → **Service Account**
5. Name it `medaltus-dashboard`, click **Done**
6. Click the service account you just created → go to the **Keys** tab
7. **Add Key** → **Create new key** → **JSON** → **Create** — a file downloads to your computer
8. Open that downloaded file in TextEdit (right-click → Open With → TextEdit)
9. Press Cmd+A to select everything, then Cmd+C to copy
10. Back in Vercel, paste the entire thing as the value for `GOOGLE_SERVICE_ACCOUNT_JSON`

Then share your Google Sheet with the service account:
1. Open your Google Sheet
2. Click **Share** (top right)
3. The service account email looks like `medaltus-dashboard@YOUR-PROJECT.iam.gserviceaccount.com` — find it in the JSON you downloaded (it's the `client_email` field)
4. Paste it in the Share box → **Viewer** → **Share**

---

**Variable 3: `SHEET_ID`**

Value: `11FfiFyI4v40WZNBfe04KiwT5jB1g74L5VPnV2AJ3X6c`

---

After adding all three, scroll up and click **Redeploy** (or go to Deployments → click the top deployment → Redeploy).

---

## Step 7 — Open Your Dashboard

Go to your Vercel URL (e.g., `https://medaltus-ad-dashboard.vercel.app`).

The dashboard should load with live data. If something doesn't work, check:
- Vercel → your project → **Functions** tab — it shows errors in plain English
- Most common issue: forgot to share the Google Sheet with the service account email

---

## Updating the Data (Daily)

Every morning, after your normal data scripts run, do this to push fresh data to the live site:

```bash
cd "/Users/SeanDeAvies/Documents/Claude/Projects/All Brands Ad Dashboard"
python3 fetch_ads_data.py
python3 deploy/generate_supplement.py
cd deploy
git add data/api_supplement.json
git commit -m "refresh data"
git push
```

Vercel automatically re-publishes within ~30 seconds of the push.

---

## Help

| Problem | Solution |
|---|---|
| Dashboard loads but no data | The service account wasn't shared on the Google Sheet |
| AI features don't work | `ANTHROPIC_API_KEY` is missing or wrong in Vercel |
| `git push` asks for password | Use your GitHub Personal Access Token, not your GitHub password |
| Page not found at the URL | Wait 1–2 min and refresh — Vercel may still be deploying |
