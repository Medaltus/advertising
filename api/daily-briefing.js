// api/daily-briefing.js — Vercel Cron Job
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
      fs.readFileSync(path.join(__dirname, '../data/api_supplement.json'), 'utf8')
    );

    const today        = new Date();
    const currentMonth = today.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
    const dayOfMonth   = today.getDate();
    const daysInMonth  = new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate();
    const dateStr      = today.toLocaleDateString('en-US', { weekday: 'long', month: 'short', day: 'numeric' });

    const md = supp[currentMonth];
    if (!md) {
      await postToSlack(slackUrl, `⚠️ Medaltus briefing: no data for ${currentMonth}`);
      return res.status(200).json({ ok: true, warning: 'no data for current month' });
    }

    const lines = [];

    // ── Header ────────────────────────────────────────────────────────────────
    lines.push(`*📊 Medaltus Portfolio — ${dateStr} (Day ${dayOfMonth} of ${daysInMonth})*`);
    lines.push('');

    // ── Portfolio totals ──────────────────────────────────────────────────────
    lines.push(
      `*MTD:* Spend ${fmt(md.spend)} · Sales ${fmt(md.adSales)} · ACOS ${fmtPct(md.acos)}${acosFlag(md.acos)} · Orders ${md.orders ?? '—'} · TACOS ${fmtPct(md.tacos)}`
    );
    lines.push('');

    // ── Brand breakdown ───────────────────────────────────────────────────────
    const brands = (md.brands || [])
      .filter(b => (b.spend || 0) > 0)
      .sort((a, b) => (b.spend || 0) - (a.spend || 0));

    if (brands.length) {
      lines.push('*Brands:*');
      for (const b of brands) {
        lines.push(
          `${b.name}: ${fmt(b.spend)} spend · ${fmt(b.adSales)} sales · ${fmtPct(b.acos)}${acosFlag(b.acos)} ACOS · ${b.orders ?? 0} orders`
        );
      }
      lines.push('');
    }

    // ── Search term insights ──────────────────────────────────────────────────
    const allTop    = [];
    const allWasted = {};
    const cpcAlerts = [];

    for (const b of md.brands || []) {
      const sti = b.searchTermInsights || {};

      // Aggregate top performers across brands
      for (const t of sti.top_performing || []) {
        allTop.push({ ...t, brand: b.name });
      }

      // Aggregate wasted spend by brand
      const brandWasted = (sti.wasted_spend || []).reduce((s, t) => s + (t.spend || 0), 0);
      if (brandWasted > 5) allWasted[b.name] = brandWasted;

      // CPC spike detection: latest day CPC > 2× 30-day avg
      for (const category of ['top_performing', 'wasted_spend', 'opportunities']) {
        for (const t of sti[category] || []) {
          const daily = t.daily || [];
          if (daily.length < 2) continue;
          const avgCpc    = t.cpc || 0;
          const latestCpc = daily[daily.length - 1].cpc || 0;
          if (avgCpc > 0 && latestCpc > 2 * avgCpc) {
            const pct = Math.round((latestCpc / avgCpc - 1) * 100);
            cpcAlerts.push(`"${t.query}" (${b.name}): $${latestCpc.toFixed(2)} today vs $${avgCpc.toFixed(2)} avg (+${pct}%)`);
          }
        }
      }
    }

    // Top 3 search terms by sales
    allTop.sort((a, b) => (b.sales || 0) - (a.sales || 0));
    if (allTop.length) {
      lines.push('*Top Search Terms:*');
      for (const t of allTop.slice(0, 3)) {
        lines.push(
          `"${t.query}": ${fmt(t.spend)} spend · ${fmt(t.sales)} sales · ${fmtPct(t.acos)} ACOS · ${t.purchases ?? 0} orders`
        );
      }
      lines.push('');
    }

    // Wasted spend summary by brand
    const wastedEntries = Object.entries(allWasted).sort((a, b) => b[1] - a[1]);
    if (wastedEntries.length) {
      lines.push('*Wasted Spend (no-sale terms):* ' +
        wastedEntries.map(([name, amt]) => `${name} ${fmt(amt)}`).join(' · ')
      );
      lines.push('');
    }

    // CPC alerts
    if (cpcAlerts.length) {
      lines.push('*⚠️ CPC Rising (2× avg):*');
      for (const alert of cpcAlerts.slice(0, 5)) {
        lines.push(alert);
      }
    }

    await postToSlack(slackUrl, lines.join('\n'));
    return res.status(200).json({ ok: true });

  } catch (err) {
    console.error('[daily-briefing.js]', err.message);
    try { await postToSlack(process.env.SLACK_WEBHOOK_URL, `⚠️ Medaltus briefing failed: ${err.message}`); } catch (_) {}
    return res.status(500).json({ error: err.message });
  }
};
