#!/bin/bash
# Usage:
#   ./update-skinuva-sales.sh shopify 23256.03
#   ./update-skinuva-sales.sh walmart 485.00
#   ./update-skinuva-sales.sh shopify 23256.03 walmart 610.00

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
JSON_FILE="$SCRIPT_DIR/skinuva/data/manual_totals.json"
MONTH=$(date +"%B %Y")

# Read current values
current=$(cat "$JSON_FILE")
shopify=$(echo "$current" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$MONTH',{}).get('shopify',0))")
walmart=$(echo "$current" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$MONTH',{}).get('walmart',0))")

# Apply updates from arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    shopify) shopify="$2"; shift 2 ;;
    walmart) walmart="$2"; shift 2 ;;
    *) echo "Unknown argument: $1. Use: shopify <amount> and/or walmart <amount>"; exit 1 ;;
  esac
done

# Write updated JSON
python3 -c "
import json, sys
try:
    with open('$JSON_FILE') as f:
        data = json.load(f)
except:
    data = {}
data['$MONTH'] = {'walmart': float('$walmart'), 'shopify': float('$shopify')}
with open('$JSON_FILE', 'w') as f:
    json.dump(data, f, indent=2)
print('Updated $MONTH: shopify=\$$shopify, walmart=\$$walmart')
"

cd "$SCRIPT_DIR" && git add skinuva/data/manual_totals.json && git commit -m "chore: update Skinuva sales for $MONTH (shopify=$shopify, walmart=$walmart)" && git pull --rebase && git push
