// api/daily-briefing.js — Vercel Cron Job
// Runs daily at 9am ET. Fetches Medaltus sheet data, generates an executive
// briefing via Claude, and posts it to the #claude-updates Slack channel.
//
// Env vars required:
//   GOOGLE_SERVICE_ACCOUNT_JSON  — same service account used by sheets.js
//   ANTHROPIC_API_KEY            — same key used by claude.js
//   SLACK_WEBHOOK_URL            — Slack Incoming Webhook URL for #claude-updates
//   BRIEFING_SECRET (optional)   — if set, manual calls must pass ?secret=<value>

const { getSheetsClient } = require('./config/_sheets_client');
const Anthropic = require('@anthropic-ai/sdk');
const https = require('https');

const SHEET_ID = process.env.SHEET_ID || '11FfiFyI4v40WZNBfe04KiwT5jB1g74L5VPnV2AJ3X6c';

async function fetchSheetText() {
  const sheetsApi = getSheetsClient();
  const meta = await sheetsApi.spreadsheets.get({ spreadsheetId: SHEET_ID });
  const sheetNames = meta.data.sheets.map(s => s.properties.title);

  let output = '';
  for (const sheetName of sheetNames) {
    const response = await sheetsApi.spreadsheets.values.get({
      spreadsheetId: SHEET_ID,
      range: sheetName,
      valueRenderOption: 'FORMATTED_VALUE',
    });
    const rows = response.data.values || [];
    if (!rows.length) continue;
    const maxCols = Math.max(...rows.map(r => r.length));
    const lines = rows.map(row => {
      const padded = [...row, ...Array(maxCols - row.length).fill('')];
      return '| ' + padded.join(' | ') + ' |';
    });
    output += `## ${sheetName}\n` + lines.join('\n') + '\n\n';
  }
  return output;
}

function postToSlack(webhookUrl, text) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ text });
    const url = new URL(webhookUrl);
    const options = {
      hostname: url.hostname,
      path: url.pathname + url.search,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
      },
    };
    const req = https.request(options, (res) => {
      res.resume();
      res.on('end', () => resolve(res.statusCode));
    });
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

module.exports = async function handler(req, res) {
  // Allow GET (cron) or POST (manual trigger)
  if (req.method !== 'GET' && req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  // Optional secret for manual calls
  const secret = process.env.BRIEFING_SECRET;
  if (secret) {
    const provided = req.query?.secret || req.headers?.['x-briefing-secret'];
    if (provided !== secret) {
      return res.status(401).json({ error: 'Unauthorized' });
    }
  }

  const slackUrl = process.env.SLACK_WEBHOOK_URL;
  if (!slackUrl) {
    return res.status(500).json({ error: 'SLACK_WEBHOOK_URL env var not set' });
  }

  try {
    const rawData = await fetchSheetText();

    const today = new Date().toLocaleDateString('en-US', {
      weekday: 'long', month: 'long', day: 'numeric', year: 'numeric',
    });
    const currentMonth = new Date().toLocaleDateString('en-US', {
      month: 'long', year: 'numeric',
    });
    const dayOfMonth = new Date().getDate();

    const prompt = `Today is ${today}. We are ${dayOfMonth} days into ${currentMonth}.

You are reviewing the Medaltus multi-brand advertising portfolio. The data below is raw pipe-delimited table data from Google Sheets covering multiple months. Find ONLY the ${currentMonth} data and generate a quick morning briefing for our Slack channel.

Rules:
- Plain language, like a casual update to a manager
- Under 200 words
- Lead with the bottom line: is the portfolio on track or not vs the 30% ACOS goal?
- Key numbers: total ad spend, ad sales, ACOS, and 1-2 standout brands (good or bad)
- We are ${dayOfMonth} days into the month — factor that in when assessing performance
- No bullet points or headers — write in short plain paragraphs
- Round numbers naturally ("about $14k" not "$14,223.18")
- Don't use: "robust", "leverage", "optimize", "trajectory", "endeavor", "commendable"
- End with one thing to watch

Here is the raw data (multiple months — focus only on ${currentMonth}):
${rawData.substring(0, 18000)}`;

    const client = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });
    const message = await client.messages.create({
      model: 'claude-haiku-4-5-20251001',
      max_tokens: 512,
      messages: [{ role: 'user', content: prompt }],
    });

    const briefing = message.content?.[0]?.text?.trim() || 'Unable to generate briefing.';
    const slackMessage = `*📊 Medaltus Ad Portfolio — ${today}*\n\n${briefing}`;

    await postToSlack(slackUrl, slackMessage);
    return res.status(200).json({ ok: true });

  } catch (err) {
    console.error('[daily-briefing.js]', err.message);
    // Try to post the error to Slack so it's visible
    try {
      await postToSlack(slackUrl, `⚠️ Medaltus morning briefing failed: ${err.message}`);
    } catch (_) {}
    return res.status(500).json({ error: err.message });
  }
};
