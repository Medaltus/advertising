// api/claude.js — Vercel serverless function
// Proxies requests to the Anthropic Claude API.
// Used by the dashboard for AI agents, WLOS generation, and market ticker fallback.
//
// Env vars required:
//   ANTHROPIC_API_KEY — your Anthropic API key

const Anthropic = require('@anthropic-ai/sdk');

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
    const client = new Anthropic({ apiKey });

    const message = await client.messages.create({
      model: 'claude-haiku-4-5-20251001',   // fast & cheap for dashboard AI features
      max_tokens: 1500,
      messages: [{ role: 'user', content: prompt }],
    });

    const text = message.content?.[0]?.text || '';
    res.status(200).json({ text });

  } catch (err) {
    console.error('[claude.js]', err.message);
    res.status(500).json({ error: err.message });
  }
};
