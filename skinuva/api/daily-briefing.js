// skinuva/api/daily-briefing.js — Vercel Cron Job
// Runs daily at 9am ET. Reads local supplement JSON, formats a KPI briefing,
// and posts it to the #claude-advertising-updates Slack channel.
//
// Env vars required:
//   SLACK_WEBHOOK_URL   — Slack Incoming Webhook URL
//   BRIEFING_SECRET     — optional: manual calls must pass ?secret=<value>

const fs    = require('fs');
const path  = require('path');
const https = require('https');

// ── Formatters ────────────────────────────────────────────────────────────────

function fmt(n) {
  if (n == null || isNaN(n)) return '—';
  if (n >= 1000) return '$' + (n / 1000).toFixed(1) + 'k';
  return '$' + Math.round(n);
}

function fmtPct(n) {
  if (n == null || isNaN(n)) return '—';
  return n.toFixed(1) + '%';
}

function acosFlag(acos) {
  if (acos == null) return '';
  if (acos <= 30) return ' ✅';
  if (acos <= 40) return ' ⚠️';
  return ' 🔴';
}

// ── Slack ─────────────────────────────────────────────────────────────────────

function postToSlack(webhookUrl, text) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ text });
    const url  = new URL(webhookUrl);
    const req  = https.request(
      {
        hostname: url.hostname,
        path: url.pathname + url.search,
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
      },
      (res) => { res.resume(); res.on('end', () => resolve(res.statusCode)); }
    );
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

// ── Handler ───────────────────────────────────────────────────────────────────

module.exports = async function handler(req, res) {
  if (req.method !== 'GET' && req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const secret = process.env.BRIEFING_SECRET;
  if (secret) {
    const provided = req.query?.secret || req.headers?.['x-briefing-secret'];
    if (provided !== secret) return res.status(401).json({ error: 'Unauthorized' });
  }

  const slackUrl = process.env.SLACK_WEBHOOK_URL;
  if (!slackUrl) return res.status(500).json({ error: 'SLACK_WEBHOOK_URL env var not set' });

  try {
    const supp = JSON.parse(
      fs.readFileSync(path.join(__dirname, '../data/skinuva_supplement.json'), 'utf8')
    );

    const today       = new Date();
    const dayOfMonth  = today.getDate();
    const daysInMonth = new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate();
    const dateStr     = today.toLocaleDateString('en-US', { weekday: 'long', month: 'short', day: 'numeric' });

    const summary = supp.summary || {};
    const sti     = supp.searchTermInsights || {};

    const lines = [];

    // ── Header ────────────────────────────────────────────────────────────────
    lines.push(`*📊 Skinuva — ${dateStr} (Day ${dayOfMonth} of ${daysInMonth})*`);
    lines.push('');

    // ── Totals ────────────────────────────────────────────────────────────────
    const acos = summary.acos;
    lines.push(
      `*MTD:* Spend ${fmt(summary.spend)} · Sales ${fmt(summary.sales)} · ACOS ${fmtPct(acos)}${acosFlag(acos)} · Orders ${summary.purchases ?? '—'} · Total Sales ${fmt(summary.totalSales)}`
    );
    lines.push('');

    // ── Top search terms ──────────────────────────────────────────────────────
    const top = (sti.top_performing || []).slice(0, 3);
    if (top.length) {
      lines.push('*Top Search Terms:*');
      for (const t of top) {
        lines.push(
          `"${t.query}": ${fmt(t.spend)} spend · ${fmt(t.sales)} sales · ${fmtPct(t.acos)} ACOS · ${t.purchases ?? 0} orders`
        );
      }
      lines.push('');
    }

    // ── Wasted spend ──────────────────────────────────────────────────────────
    const wasted = (sti.wasted_spend || []).slice(0, 3);
    if (wasted.length) {
      lines.push('*Wasted Spend (no-sale terms):*');
      for (const t of wasted) {
        lines.push(
          `"${t.query}": ${fmt(t.spend)} spend · ${fmt(t.sales)} sales · ${fmtPct(t.acos)} ACOS`
        );
      }
    }

    await postToSlack(slackUrl, lines.join('\n'));
    return res.status(200).json({ ok: true });

  } catch (err) {
    console.error('[skinuva/daily-briefing.js]', err.message);
    try { await postToSlack(process.env.SLACK_WEBHOOK_URL, `⚠️ Skinuva briefing failed: ${err.message}`); } catch (_) {}
    return res.status(500).json({ error: err.message });
  }
};
