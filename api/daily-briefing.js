// api/daily-briefing.js — Vercel Cron Job
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

function delta(curr, prev) {
  if (curr == null || prev == null) return '';
  const d    = curr - prev;
  const sign = d > 0 ? '+' : '';
  return ` (${sign}${d.toFixed(1)}pp vs last mo)`;
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
      fs.readFileSync(path.join(__dirname, '../data/api_supplement.json'), 'utf8')
    );

    const today        = new Date();
    const currentMonth = today.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
    const dayOfMonth   = today.getDate();
    const daysInMonth  = new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate();
    const dateStr      = today.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });

    const priorDate  = new Date(today.getFullYear(), today.getMonth() - 1, 1);
    const priorMonth = priorDate.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });

    const md   = supp[currentMonth];
    const prMd = supp[priorMonth] || {};

    if (!md) {
      await postToSlack(slackUrl, { text: `Medaltus briefing: no data for ${currentMonth}` });
      return res.status(200).json({ ok: true, warning: 'no data for current month' });
    }

    const priorBrands = {};
    for (const b of prMd.brands || []) priorBrands[b.name] = b;

    const brands = (md.brands || [])
      .filter(b => (b.spend || 0) > 0)
      .sort((a, b) => (b.spend || 0) - (a.spend || 0));

    // ── Header + totals (plain text block) ───────────────────────────────────
    const daysElapsed    = Math.max(dayOfMonth - 1, 1);
    const projSpend      = (md.spend / daysElapsed) * daysInMonth;
    const projSales      = ((md.adSales || 0) / daysElapsed) * daysInMonth;
    const projAcos       = projSales > 0 ? (projSpend / projSales) * 100 : null;

    const headerText = [
      `*📊 Medaltus Portfolio — ${dateStr} (Day ${dayOfMonth} of ${daysInMonth})*`,
      '',
      `*MTD:* Spend ${fmt(md.spend)} · Sales ${fmt(md.adSales)} · ACOS ${fmtPct(md.acos)} · Orders ${md.orders ?? '—'} · TACOS ${fmtPct(md.tacos)}`,
      `*Pacing:* ~${fmt(projSpend)} spend · ~${fmt(projSales)} sales · ~${fmtPct(projAcos)} ACOS projected EOM`,
    ].join('\n');

    // ── Brand tiers (colored attachments) ────────────────────────────────────
    const greenBrands  = [];  // ≤30% ACOS
    const orangeBrands = [];  // 30–40%
    const redBrands    = [];  // >40%

    const working   = [];
    const concerns  = [];
    const cpcAlerts = [];
    const allWasted = {};

    for (const b of brands) {
      const pr  = priorBrands[b.name];
      const mom = pr ? delta(b.acos, pr.acos) : '';
      const row = `${b.name}: ${fmt(b.spend)} · ${fmtPct(b.acos)} ACOS${mom} · ${b.orders ?? 0} orders`;

      if (b.acos == null || b.acos <= 30) greenBrands.push(row);
      else if (b.acos <= 40)              orangeBrands.push(row);
      else                                redBrands.push(row);

      // What's Working
      if (b.acos != null && b.acos <= 25) {
        const note = pr && pr.acos != null && b.acos < pr.acos
          ? `, improving from ${fmtPct(pr.acos)} last mo` : '';
        working.push(`${b.name} at ${fmtPct(b.acos)} ACOS${note}`);
      }

      // Concerns
      if (b.acos != null && b.acos > 40) {
        const note = pr && pr.acos != null ? ` (was ${fmtPct(pr.acos)} last mo)` : '';
        concerns.push(`${b.name} at ${fmtPct(b.acos)} ACOS${note}`);
      }

      // Wasted spend
      const sti       = b.searchTermInsights || {};
      const wastedAmt = (sti.wasted_spend || []).reduce((s, t) => s + (t.spend || 0), 0);
      if (wastedAmt > 10) allWasted[b.name] = wastedAmt;

      // CPC spikes
      for (const cat of ['top_performing', 'wasted_spend', 'opportunities']) {
        for (const t of sti[cat] || []) {
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

    // ── Scaling opportunities ─────────────────────────────────────────────────
    const allOpps = [];
    for (const b of brands) {
      for (const o of (b.searchTermInsights || {}).opportunities || []) {
        if ((o.acos || 0) < 30 && (o.cvr || 0) > 0) allOpps.push({ ...o, brand: b.name });
      }
    }
    allOpps.sort((a, b) => (b.cvr || 0) - (a.cvr || 0));

    // ── Budget concentration ──────────────────────────────────────────────────
    const totalSpend = md.spend || 1;
    const top3conc   = brands.slice(0, 3)
      .map(b => `${b.name} ${((b.spend || 0) / totalSpend * 100).toFixed(0)}%`)
      .join(' · ');
    const topBrandPct = brands[0] ? (brands[0].spend || 0) / totalSpend * 100 : 0;

    // ── Build attachments ─────────────────────────────────────────────────────
    const attachments = [];

    if (greenBrands.length)
      attachments.push(attachment('#2eb886', ['*On Target (≤30% ACOS)*', ...greenBrands]));

    if (orangeBrands.length)
      attachments.push(attachment('#ecb22e', ['*Over Goal (30–40% ACOS)*', ...orangeBrands]));

    if (redBrands.length)
      attachments.push(attachment('#e01e5a', ['*Critical (>40% ACOS)*', ...redBrands]));

    if (working.length)
      attachments.push(attachment('#2eb886', ['*What\'s Working*', ...working]));

    const concernLines = [...concerns];
    const wastedEntries = Object.entries(allWasted).sort((a, b) => b[1] - a[1]);
    if (wastedEntries.length)
      concernLines.push('Wasted spend: ' + wastedEntries.map(([n, amt]) => `${n} ${fmt(amt)}`).join(' · '));
    for (const alert of cpcAlerts.slice(0, 3))
      concernLines.push(`CPC rising — ${alert}`);
    if (concernLines.length)
      attachments.push(attachment('#e01e5a', ['*Concerns*', ...concernLines]));

    if (allOpps.length) {
      const oppLines = allOpps.slice(0, 4).map(o => {
        const cvrPct = ((o.cvr || 0) * 100).toFixed(0);
        return `"${o.query}" (${o.brand}): ${cvrPct}% CVR · ${fmtPct(o.acos)} ACOS · only ${fmt(o.spend)} spent`;
      });
      attachments.push(attachment('#4a90d9', ['*Scaling Opportunities*', ...oppLines]));
    }

    const concFlag = topBrandPct > 60 ? ' — high concentration' : '';
    attachments.push(attachment('#aaaaaa', [`*Budget Concentration:* ${top3conc}${concFlag}`]));

    await postToSlack(slackUrl, { text: `Medaltus Portfolio — ${dateStr}`, attachments });
    return res.status(200).json({ ok: true });

  } catch (err) {
    console.error('[daily-briefing.js]', err.message);
    try { await postToSlack(process.env.SLACK_WEBHOOK_URL, { text: `Medaltus briefing failed: ${err.message}` }); } catch (_) {}
    return res.status(500).json({ error: err.message });
  }
};
