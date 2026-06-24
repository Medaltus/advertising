// skinuva/api/daily-briefing.js — Vercel Cron Job
// Runs daily at 9am ET. Reads local supplement JSON, formats a KPI briefing,
// and posts it to Slack using colored attachment blocks.
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

// ── Slack ─────────────────────────────────────────────────────────────────────

function postToSlack(webhookUrl, payload) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify(payload);
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

function attachment(color, lines) {
  return { color, mrkdwn_in: ['text'], text: lines.join('\n') };
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
    const acos    = summary.acos;

    // ── Header + totals ───────────────────────────────────────────────────────
    const daysElapsed = Math.max(dayOfMonth - 1, 1);
    const projSpend   = (summary.spend / daysElapsed) * daysInMonth;
    const projSales   = ((summary.sales || 0) / daysElapsed) * daysInMonth;
    const projAcos    = projSales > 0 ? (projSpend / projSales) * 100 : null;

    const headerText = [
      `*📊 Skinuva — ${dateStr} (Day ${dayOfMonth} of ${daysInMonth})*`,
      '',
      `*MTD:* Spend ${fmt(summary.spend)} · Sales ${fmt(summary.sales)} · ACOS ${fmtPct(acos)} · Orders ${summary.purchases ?? '—'} · Total Sales ${fmt(summary.totalSales)}`,
      `*Pacing:* ~${fmt(projSpend)} spend · ~${fmt(projSales)} sales · ~${fmtPct(projAcos)} ACOS projected EOM`,
    ].join('\n');

    // ── What's Working ────────────────────────────────────────────────────────
    const working = [];
    if (acos != null && acos <= 25) {
      working.push(`Overall ACOS ${fmtPct(acos)} — well under 30% goal`);
    }
    for (const t of (sti.top_performing || []).slice(0, 3)) {
      if (t.acos != null && t.acos <= 20) {
        working.push(`"${t.query}": ${fmtPct(t.acos)} ACOS · ${t.purchases ?? 0} orders · ${fmt(t.sales)} sales`);
      }
    }

    // ── Concerns ─────────────────────────────────────────────────────────────
    const concernLines = [];
    if (acos != null && acos > 30 && acos <= 40)
      concernLines.push(`Overall ACOS ${fmtPct(acos)} — slightly over 30% goal`);
    else if (acos != null && acos > 40)
      concernLines.push(`Overall ACOS ${fmtPct(acos)} — significantly over 30% goal`);

    const wasted = (sti.wasted_spend || []).slice(0, 3);
    if (wasted.length) {
      concernLines.push('Wasted spend:');
      for (const t of wasted) {
        concernLines.push(`  "${t.query}": ${fmt(t.spend)} · ${fmt(t.sales)} sales · ${fmtPct(t.acos)} ACOS`);
      }
    }

    // ── Scaling Opportunities ─────────────────────────────────────────────────
    const opps = (sti.opportunities || [])
      .filter(o => (o.acos || 0) < 30 && (o.cvr || 0) > 0)
      .sort((a, b) => (b.cvr || 0) - (a.cvr || 0))
      .slice(0, 4);

    // ── Build attachments ─────────────────────────────────────────────────────
    const attachments = [];

    if (working.length)
      attachments.push(attachment('#2eb886', ['*What\'s Working*', ...working]));

    if (concernLines.length)
      attachments.push(attachment('#e01e5a', ['*Concerns*', ...concernLines]));

    if (opps.length) {
      const oppLines = opps.map(o => {
        const cvrPct = ((o.cvr || 0) * 100).toFixed(0);
        return `"${o.query}": ${cvrPct}% CVR · ${fmtPct(o.acos)} ACOS · only ${fmt(o.spend)} spent`;
      });
      attachments.push(attachment('#4a90d9', ['*Scaling Opportunities*', ...oppLines]));
    }

    await postToSlack(slackUrl, {
      text: `Skinuva — ${dateStr}`,
      blocks: [{ type: 'section', text: { type: 'mrkdwn', text: headerText } }],
      attachments,
    });
    return res.status(200).json({ ok: true });

  } catch (err) {
    console.error('[skinuva/daily-briefing.js]', err.message);
    try { await postToSlack(process.env.SLACK_WEBHOOK_URL, { text: `Skinuva briefing failed: ${err.message}` }); } catch (_) {}
    return res.status(500).json({ error: err.message });
  }
};
