// api/sheets.js — Vercel serverless function
// Fetches the Medaltus Advertising Reports Google Sheet and returns it
// as pipe-delimited text that the dashboard parser expects.
//
// Env vars required:
//   GOOGLE_SERVICE_ACCOUNT_JSON  — full JSON key from Google Cloud service account
//   SHEET_ID                     — Google Sheets file ID (optional, falls back to hardcoded)

const { getSheetsClient } = require('./config/_sheets_client');

const SHEET_ID = process.env.SHEET_ID || '11FfiFyI4v40WZNBfe04KiwT5jB1g74L5VPnV2AJ3X6c';

module.exports = async function handler(req, res) {
  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  try {
    const sheetsApi = getSheetsClient();

    // Get list of all sheets in the spreadsheet
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
      if (rows.length === 0) continue;

      // Find the max column count across all rows
      const maxCols = Math.max(...rows.map(r => r.length));

      // Convert each row to a pipe-delimited line (markdown table format)
      const tableLines = rows.map(row => {
        const padded = [...row, ...Array(maxCols - row.length).fill('')];
        return '| ' + padded.join(' | ') + ' |';
      });

      output += tableLines.join('\n') + '\n\n';
    }

    // Cache for 5 min at CDN, serve stale for another 10 min while revalidating
    res.setHeader('Cache-Control', 's-maxage=300, stale-while-revalidate=600');
    res.setHeader('Content-Type', 'text/plain; charset=utf-8');
    res.status(200).send(output);

  } catch (err) {
    console.error('[sheets.js]', err.message);
    res.status(500).json({ error: err.message });
  }
};
