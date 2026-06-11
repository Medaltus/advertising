// api/config/_sheets_client.js
// Shared Google Sheets authentication helper.
// Files prefixed with _ in api/ are NOT exposed as Vercel endpoints.
//
// Requires env var: GOOGLE_SERVICE_ACCOUNT_JSON

const { google } = require('googleapis');

function getSheetsClient() {
  const keyJson = process.env.GOOGLE_SERVICE_ACCOUNT_JSON;
  if (!keyJson) throw new Error('GOOGLE_SERVICE_ACCOUNT_JSON env var not set');

  const credentials = JSON.parse(keyJson);
  const auth = new google.auth.GoogleAuth({
    credentials,
    scopes: ['https://www.googleapis.com/auth/spreadsheets.readonly'],
  });

  return google.sheets({ version: 'v4', auth });
}

module.exports = { getSheetsClient };
