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
amz_total = (entry.get("amazon") or {}).get("totalSales")
shopify   = entry.get("shopify") or 0
walmart   = entry.get("walmart") or 0
if amz_total is None:
    # The pipeline could not obtain a trustworthy brand-filtered Amazon total for
    # this month. Writing shopify+walmart alone would look like a real combined
    # total while silently omitting all Amazon revenue, so leave it null and let
    # the dashboard fall back to the Google Sheet instead.
    entry["combinedTotalSales"] = None
    combined = None
    print("  ⚠  Amazon totalSales unavailable for this month — combinedTotalSales left null")
else:
    combined = round(amz_total + shopify + walmart, 2)
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
# --autostash: without it, ANY unrelated modified file in the working tree makes
# the rebase abort with "cannot pull with rebase: You have unstaged changes",
# which is what made this script fail every time the dashboard files had been
# touched. The CI workflow already uses --autostash for the same reason.
if git pull --rebase --autostash; then
  git push && echo "✓ Pushed. Vercel will redeploy automatically."
else
  # Auto-recover from conflict on skinuva_monthly.json (cron ran at same time)
  CONFLICTS=$(git diff --name-only --diff-filter=U 2>/dev/null)
  if echo "$CONFLICTS" | grep -q "skinuva_monthly.json"; then
    echo "  → Conflict detected — auto-resolving (taking cron data, re-applying our value)..."

    # Take cron's version of skinuva_monthly.json (has fresh Amazon data)
    git checkout --theirs skinuva/data/skinuva_monthly.json

    # Re-apply our channel value on top of cron's version
    python3 - "$CHANNEL" "$AMOUNT" "$MONTH_LABEL" "$MONTHLY" <<'RESOLVE_PYEOF'
import json, sys
channel, amount, month, monthly_path = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4]
with open(monthly_path) as f:
    sm = json.load(f)
if month in sm:
    entry = sm[month]
    entry[channel] = amount
    amz_total = (entry.get("amazon") or {}).get("totalSales")
    shopify   = entry.get("shopify") or 0
    walmart   = entry.get("walmart") or 0
    if amz_total is None:
        # See the note in the first python block: never fabricate a combined total
        # that silently omits Amazon revenue.
        entry["combinedTotalSales"] = None
        combined = None
    else:
        combined = round(amz_total + shopify + walmart, 2)
        entry["combinedTotalSales"] = combined
    with open(monthly_path, "w") as f:
        json.dump(sm, f, indent=2)
    if combined is None:
        print(f"  ✓ Re-applied {channel}=${amount:,.2f} → combinedTotalSales left null "
              f"(Amazon totalSales unavailable for this month)")
    else:
        print(f"  ✓ Re-applied {channel}=${amount:,.2f} → combinedTotalSales=${combined:,.2f}")
else:
    print(f"  ⚠  Month '{month}' not found after conflict resolution")
RESOLVE_PYEOF

    git add skinuva/data/skinuva_monthly.json
    # Also keep ours for manual_totals.json if it conflicted
    if echo "$CONFLICTS" | grep -q "manual_totals.json"; then
      git checkout --ours skinuva/data/manual_totals.json
      git add skinuva/data/manual_totals.json
    fi

    GIT_EDITOR=true git rebase --continue
    git push && echo "✓ Pushed (auto-resolved conflict). Vercel will redeploy automatically."
  else
    echo "✗ Unexpected conflict — run 'git status' to investigate."
    git rebase --abort 2>/dev/null || true
    exit 1
  fi
fi
