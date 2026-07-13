// api/sheets.js — Vercel serverless function
// Fetches the Medaltus Advertising Reports Google Sheet and returns it
// as pipe-delimited text that the dashboard parser expects.
//
// Env vars required:
//   GOOGLE_SERVICE_ACCOUNT_JSON  — full JSON key from Google Cloud service account
//   SHEET_ID                     — Google Sheets file ID (optional, falls back to hardcoded)

const { getSheetsClient } = require('./config/_sheets_client');

const SHEET_ID = process.env.SHEET_ID || '11FfiFyI4v40WZNBfe04KiwT5jB1g74L5VPnV2AJ3X6c';

// The dashboard parser expects brand tables with:
//   MONTH | BRAND NAME | SPEND | AD SALES | ACOS | TOTAL SALES | TACOS | ...
//
// The Google Sheet uses an extra leading MONTH/YEAR column:
//   MONTH/YEAR | MONTH | BRAND NAME | SPEND | ...
//
// Brand rows duplicate the month: June 2026 | June 2026 | Amala | ...
// TOTAL rows skip the duplicate:  June 2026 | TOTAL | $3,019.93 | ...
//
// This function normalises the table so the parser always sees:
//   MONTH | BRAND NAME | SPEND | ...   (standard format)
function normalizeRows(rows) {
  if (!rows || rows.length === 0) return rows;

  const headerCells = rows[0].map(c => (c || '').toString().trim().toLowerCase());
  const h0 = headerCells[0] || '';
  const h1 = headerCells[1] || '';

  // Only normalise tables with the extra MONTH/YEAR prefix column
  const hasMonthYearPrefix =
    (h0 === 'month/year' || h0.replace('/', ' ') === 'month year') &&
    (h1 === 'month');

  if (!hasMonthYearPrefix) return rows;

  return rows.map((row, rowIdx) => {
    if (rowIdx === 0) {
      // Header: drop the MONTH/YEAR column, keep MONTH | BRAND NAME | ...
      return row.slice(1);
    }
    const c0 = (row[0] || '').trim();
    const c1 = (row[1] || '').trim();
    // Brand rows: month duplicated in c0 and c1 — drop c0
    // TOTAL rows: c0 is empty (sheet leaves MONTH/YEAR blank), c1 has the month — also drop c0
    if (c0 === c1 || c0 === '') {
      return row.slice(1);
    }
    return row;
  });
}

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

    for (const sheetMeta of meta.data.sheets) {
      const sheetName = sheetMeta.properties.title;
      const gid = sheetMeta.properties.sheetId || '';

      const response = await sheetsApi.spreadsheets.values.get({
        spreadsheetId: SHEET_ID,
        range: sheetName,
        valueRenderOption: 'FORMATTED_VALUE',
      });

      let rows = response.data.values || [];
      if (rows.length === 0) continue;

      // Emit a tab-name marker so the dashboard can route content by tab
      output += `## SHEET:${sheetName}:GID:${gid}\n`;

      // Normalise away the extra MONTH/YEAR prefix column if present
      rows = normalizeRows(rows);

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
