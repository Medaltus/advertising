#!/bin/bash
# update-skinuva-sales.sh
# Usage:
#   ./update-skinuva-sales.sh shopify 9701.43            # current month
#   ./update-skinuva-sales.sh walmart 635.00             # current month
#   ./update-skinuva-sales.sh shopify 40856.74 "June 2026"  # specific month

set -e

CHANNEL="$1"
AMOUNT="$2"
MONTH_ARG="$3"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MANUAL="$SCRIPT_DIR/skinuva/data/manual_totals.json"
MONTHLY="$SCRIPT_DIR/skinuva/data/skinuva_monthly.json"

if [[ -z "$CHANNEL" || -z "$AMOUNT" ]]; then
  echo "Usage: $0 <shopify|walmart> <amount> [\"Month YYYY\"]"
  echo "  e.g. $0 shopify 9701.43"
  echo "  e.g. $0 shopify 40856.74 \"June 2026\""
  exit 1
fi

if [[ "$CHANNEL" != "shopify" && "$CHANNEL" != "walmart" ]]; then
  echo "Error: channel must be 'shopify' or 'walmart'"
  exit 1
fi

MONTH_LABEL=$(python3 - "$MONTH_ARG" <<'PYEOF'
import sys
from datetime import datetime
arg = sys.argv[1] if len(sys.argv) > 1 else ""
if arg:
    print(arg)
else:
    months = ['January','February','March','April','May','June',
              'July','August','September','October','November','December']
    now = datetime.now()
    print(f"{months[now.month-1]} {now.year}")
PYEOF
)

echo "Month: $MONTH_LABEL"

python3 - "$CHANNEL" "$AMOUNT" "$MONTH_LABEL" "$MANUAL" "$MONTHLY" <<'PYEOF'
import json, sys

channel, amount, month, manual_path, monthly_path = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
print(f"Updating {channel} for {month} → ${amount:,.2f}")

with open(manual_path) as f:
    mt = json.load(f)
if month not in mt:
    mt[month] = {}
mt[month][channel] = amount
with open(manual_path, "w") as f:
    json.dump(mt, f, indent=2)
print(f"  ✓ manual_totals.json updated")

with open(monthly_path) as f:
    sm = json.load(f)
if month not in sm:
    print(f"  ⚠  '{month}' not found in skinuva_monthly.json — skipping recalc")
    sys.exit(0)
entry = sm[month]
entry[channel] = amount
amz_total = (entry.get("amazon") or {}).get("totalSales") or 0
shopify   = entry.get("shopify") or 0
walmart   = entry.get("walmart") or 0
combined  = round(amz_total + shopify + walmart, 2)
entry["combinedTotalSales"] = combined
with open(monthly_path, "w") as f:
    json.dump(sm, f, indent=2)
print(f"  ✓ skinuva_monthly.json updated  (combinedTotalSales = ${combined:,.2f})")
PYEOF

cd "$SCRIPT_DIR"
# Clear any stale lock files left by background processes
rm -f .git/index.lock .git/HEAD.lock .git/packed-refs.lock .git/REBASE_HEAD.lock 2>/dev/null || true
git add skinuva/data/manual_totals.json skinuva/data/skinuva_monthly.json
git commit -m "data: Skinuva ${CHANNEL} for ${MONTH_LABEL} = \$${AMOUNT}"

echo ""
if git pull --rebase && git push; then
  echo "✓ Pushed. Vercel will redeploy automatically."
else
  echo "✗ Push did NOT go through — likely a rebase conflict (e.g. daily cron ran at the same time)."
  echo "  Run 'git status' in this folder to see the conflict, resolve it, then 'git rebase --continue' and 'git push'."
  exit 1
fi
