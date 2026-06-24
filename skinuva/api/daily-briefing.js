// skinuva/api/daily-briefing.js — Vercel Cron Job
// Runs daily at 9am ET. Reads local supplement JSON, formats a KPI briefing,
// and posts it to Slack.
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
    const dateStr     = today.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });

    const summary = supp.summary || {};
    const sti     = supp.searchTermInsights || {};

    const lines = [];

    // ── Header ────────────────────────────────────────────────────────────────
    lines.push(`*📊 Skinuva — ${dateStr} (Day ${dayOfMonth} of ${daysInMonth})*`);
    lines.push('');

    // ── Totals + Pacing ───────────────────────────────────────────────────────
    const acos = summary.acos;
    lines.push(
      `*MTD:* Spend ${fmt(summary.spend)} · Sales ${fmt(summary.sales)} · ACOS ${fmtPct(acos)}${acosFlag(acos)} · Orders ${summary.purchases ?? '—'} · Total Sales ${fmt(summary.totalSales)}`
    );

    const daysElapsed = Math.max(dayOfMonth - 1, 1);
    const projected   = (summary.spend / daysElapsed) * daysInMonth;
    lines.push(`*Pacing:* ~${fmt(projected)} projected EOM at current rate`);
    lines.push('');

    // ── What's Working ────────────────────────────────────────────────────────
    const working  = [];
    const concerns = [];

    if (acos != null && acos <= 25) {
      working.push(`Overall ACOS ${fmtPct(acos)} — well under 30% goal`);
    }

    // Top wasted spend terms for concerns
    const wasted = (sti.wasted_spend || []).slice(0, 3);

    // Top performing terms for what's working
    const top = (sti.top_performing || []).slice(0, 3);
    for (const t of top) {
      if (t.acos != null && t.acos <= 20) {
        working.push(`"${t.query}": ${fmtPct(t.acos)} ACOS · ${t.purchases ?? 0} orders · ${fmt(t.sales)} sales`);
      }
    }

    if (acos != null && acos > 30 && acos <= 40) {
      concerns.push(`Overall ACOS ${fmtPct(acos)} — slightly over 30% goal`);
    } else if (acos != null && acos > 40) {
      concerns.push(`Overall ACOS ${fmtPct(acos)} — significantly over 30% goal`);
    }

    if (working.length) {
      lines.push('*✅ What\'s Working:*');
      for (const w of working) lines.push(w);
      lines.push('');
    }

    // ── Concerns ──────────────────────────────────────────────────────────────
    if (concerns.length || wasted.length) {
      lines.push('*⚠️ Concerns:*');
      for (const c of concerns) lines.push(c);
      if (wasted.length) {
        lines.push('Wasted spend (no-sale terms):');
        for (const t of wasted) {
          lines.push(`  "${t.query}": ${fmt(t.spend)} spend · ${fmt(t.sales)} sales · ${fmtPct(t.acos)} ACOS`);
        }
      }
      lines.push('');
    }

    // ── Scaling Opportunities ─────────────────────────────────────────────────
    const opps = (sti.opportunities || [])
      .filter(o => (o.acos || 0) < 30 && (o.cvr || 0) > 0)
      .sort((a, b) => (b.cvr || 0) - (a.cvr || 0))
      .slice(0, 4);

    if (opps.length) {
      lines.push('*🚀 Scaling Opportunities (high CVR, low spend):*');
      for (const o of opps) {
        const cvrPct = ((o.cvr || 0) * 100).toFixed(0);
        lines.push(`"${o.query}": ${cvrPct}% CVR · ${fmtPct(o.acos)} ACOS · only ${fmt(o.spend)} spent`);
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
