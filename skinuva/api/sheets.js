// api/sheets.js — Vercel serverless function (Skinuva)
// Fetches the Skinuva Advertising Reports Google Sheet and returns it
// as pipe-delimited text that the dashboard parser expects.
//
// Env vars required:
//   GOOGLE_SERVICE_ACCOUNT_JSON  — full JSON key from Google Cloud service account

const { getSheetsClient } = require('./config/_sheets_client');

const SHEET_ID = '1CITWFOhGXFSyXFE0j-Rze-oNfmmDqHXFUUSHGDeA7UI';

function normalizeRows(rows) {
  if (!rows || rows.length === 0) return rows;

  const headerCells = rows[0].map(c => (c || '').toString().trim().toLowerCase());
  const h0 = headerCells[0] || '';
  const h1 = headerCells[1] || '';

  const hasMonthYearPrefix =
    (h0 === 'month/year' || h0.replace('/', ' ') === 'month year') &&
    (h1 === 'month');

  if (!hasMonthYearPrefix) return rows;

  return rows.map((row, rowIdx) => {
    if (rowIdx === 0) return row.slice(1);
    const c0 = (row[0] || '').trim();
    const c1 = (row[1] || '').trim();
    if (c0 === c1 || c0 === '') return row.slice(1);
    return row;
  });
}

module.exports = async function handler(req, res) {
  if (req.method !== 'GET') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  try {
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

      let rows = response.data.values || [];
      if (rows.length === 0) continue;

      rows = normalizeRows(rows);

      const maxCols = Math.max(...rows.map(r => r.length));
      const tableLines = rows.map(row => {
        const padded = [...row, ...Array(maxCols - row.length).fill('')];
        return '| ' + padded.join(' | ') + ' |';
      });

      output += tableLines.join('\n') + '\n\n';
    }

    res.setHeader('Cache-Control', 's-maxage=300, stale-while-revalidate=600');
    res.setHeader('Content-Type', 'text/plain; charset=utf-8');
    res.status(200).send(output);

  } catch (err) {
    console.error('[sheets.js]', err.message);
    res.status(500).json({ error: err.message });
  }
};
