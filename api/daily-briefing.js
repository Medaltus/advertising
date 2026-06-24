// api/daily-briefing.js — Vercel Cron Job
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

function delta(curr, prev) {
  if (curr == null || prev == null) return '';
  const d = curr - prev;
  const sign = d > 0 ? '+' : '';
  return ` (${sign}${d.toFixed(1)}pp vs last mo)`;
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
    const dateStr      = today.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });

    // Prior month for MoM comparisons
    const priorDate  = new Date(today.getFullYear(), today.getMonth() - 1, 1);
    const priorMonth = priorDate.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });

    const md   = supp[currentMonth];
    const prMd = supp[priorMonth] || {};

    if (!md) {
      await postToSlack(slackUrl, `⚠️ Medaltus briefing: no data for ${currentMonth}`);
      return res.status(200).json({ ok: true, warning: 'no data for current month' });
    }

    // Prior month brand lookup
    const priorBrands = {};
    for (const b of prMd.brands || []) priorBrands[b.name] = b;

    const lines = [];

    // ── Header ────────────────────────────────────────────────────────────────
    lines.push(`*📊 Medaltus Portfolio — ${dateStr} (Day ${dayOfMonth} of ${daysInMonth})*`);
    lines.push('');

    // ── Portfolio totals ──────────────────────────────────────────────────────
    lines.push(
      `*MTD:* Spend ${fmt(md.spend)} · Sales ${fmt(md.adSales)} · ACOS ${fmtPct(md.acos)}${acosFlag(md.acos)} · Orders ${md.orders ?? '—'} · TACOS ${fmtPct(md.tacos)}`
    );

    // Spend pacing
    const daysElapsed = Math.max(dayOfMonth - 1, 1);
    const dailyRate   = md.spend / daysElapsed;
    const projected   = dailyRate * daysInMonth;
    lines.push(`*Pacing:* ~${fmt(projected)} projected EOM at current rate`);
    lines.push('');

    // ── Brand breakdown ───────────────────────────────────────────────────────
    const brands = (md.brands || [])
      .filter(b => (b.spend || 0) > 0)
      .sort((a, b) => (b.spend || 0) - (a.spend || 0));

    if (brands.length) {
      lines.push('*Brands:*');
      for (const b of brands) {
        const pr   = priorBrands[b.name];
        const mom  = pr ? delta(b.acos, pr.acos) : '';
        lines.push(
          `${b.name}: ${fmt(b.spend)} · ${fmtPct(b.acos)}${acosFlag(b.acos)} ACOS${mom} · ${b.orders ?? 0} orders`
        );
      }
      lines.push('');
    }

    // ── What's Working / Concerns ─────────────────────────────────────────────
    const working  = [];
    const concerns = [];
    const cpcAlerts = [];
    const allWasted = {};

    for (const b of brands) {
      const pr = priorBrands[b.name];

      // What's Working: ACOS ≤ 25%
      if (b.acos != null && b.acos <= 25) {
        const note = pr && pr.acos != null
          ? (b.acos < pr.acos ? `, improving from ${fmtPct(pr.acos)} last mo` : '')
          : '';
        working.push(`${b.name} at ${fmtPct(b.acos)} ACOS${note}`);
      }

      // Concerns: ACOS > 40%
      if (b.acos != null && b.acos > 40) {
        const note = pr && pr.acos != null ? ` (was ${fmtPct(pr.acos)} last mo)` : '';
        concerns.push(`${b.name} at ${fmtPct(b.acos)} ACOS${note}`);
      }

      // Wasted spend totals
      const sti = b.searchTermInsights || {};
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

    if (working.length) {
      lines.push('*✅ What\'s Working:*');
      for (const w of working) lines.push(w);
      lines.push('');
    }

    // Concerns: high ACOS brands + wasted spend
    const wastedEntries = Object.entries(allWasted).sort((a, b) => b[1] - a[1]);
    if (concerns.length || wastedEntries.length || cpcAlerts.length) {
      lines.push('*⚠️ Concerns:*');
      for (const c of concerns) lines.push(c);
      if (wastedEntries.length) {
        lines.push(
          `Wasted spend: ` + wastedEntries.map(([n, amt]) => `${n} ${fmt(amt)}`).join(' · ')
        );
      }
      for (const alert of cpcAlerts.slice(0, 3)) {
        lines.push(`CPC rising — ${alert}`);
      }
      lines.push('');
    }

    // ── Scaling Opportunities ─────────────────────────────────────────────────
    // High-CVR, low-spend terms worth increasing bids on (ACOS < 30%)
    const allOpps = [];
    for (const b of brands) {
      const opps = (b.searchTermInsights || {}).opportunities || [];
      for (const o of opps) {
        if ((o.acos || 0) < 30 && (o.cvr || 0) > 0) {
          allOpps.push({ ...o, brand: b.name });
        }
      }
    }
    allOpps.sort((a, b) => (b.cvr || 0) - (a.cvr || 0));
    if (allOpps.length) {
      lines.push('*🚀 Scaling Opportunities (high CVR, low spend):*');
      for (const o of allOpps.slice(0, 4)) {
        const cvrPct = ((o.cvr || 0) * 100).toFixed(0);
        lines.push(`"${o.query}" (${o.brand}): ${cvrPct}% CVR · ${fmtPct(o.acos)} ACOS · only ${fmt(o.spend)} spent`);
      }
      lines.push('');
    }

    // ── Budget Concentration ──────────────────────────────────────────────────
    const totalSpend = md.spend || 1;
    const topBrand   = brands[0];
    if (topBrand) {
      const topPct = ((topBrand.spend || 0) / totalSpend * 100).toFixed(1);
      const top3   = brands.slice(0, 3).map(b => `${b.name} ${((b.spend||0)/totalSpend*100).toFixed(0)}%`).join(' · ');
      const flag   = parseFloat(topPct) > 60 ? ' ⚠️' : '';
      lines.push(`*Budget Concentration:* ${top3}${flag}`);
    }

    await postToSlack(slackUrl, lines.join('\n'));
    return res.status(200).json({ ok: true });

  } catch (err) {
    console.error('[daily-briefing.js]', err.message);
    try { await postToSlack(process.env.SLACK_WEBHOOK_URL, `⚠️ Medaltus briefing failed: ${err.message}`); } catch (_) {}
    return res.status(500).json({ error: err.message });
  }
};
