// api/claude.js — Vercel serverless function
// Proxies requests to the Anthropic Claude API via native fetch (Node 18+).
// No SDK dependency — avoids version skew issues.
//
// Env vars required:
//   ANTHROPIC_API_KEY — your Anthropic API key

module.exports = async function handler(req, res) {
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const { prompt } = req.body || {};
  if (!prompt || typeof prompt !== 'string') {
    return res.status(400).json({ error: 'Missing or invalid prompt' });
  }

  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    return res.status(500).json({ error: 'ANTHROPIC_API_KEY env var not set' });
  }

  try {
    const response = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'x-api-key': apiKey,
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        model: 'claude-haiku-4-5-20251001',
        max_tokens: 3000,
        messages: [{ role: 'user', content: prompt }],
      }),
    });

    const data = await response.json();
    if (!response.ok) {
      const msg = data?.error?.message || `HTTP ${response.status}`;
      console.error('[claude.js]', response.status, msg);
      return res.status(500).json({ error: msg });
    }

    const text = data.content?.[0]?.text || '';
    res.status(200).json({ text });

  } catch (err) {
    console.error('[claude.js]', err.message);
    res.status(500).json({ error: err.message });
  }
};
