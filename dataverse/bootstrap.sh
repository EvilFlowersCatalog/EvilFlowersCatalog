#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <DATAVERSE_API_TOKEN> [DATAVERSE_URL] [WORKFLOW_FILE]"
  echo "Example: $0 \"xxxxxxxx\" http://127.0.0.1:8080 postpublish-sync.json"
  exit 1
fi

DV_TOKEN="$1"
DV_URL="${2:-http://127.0.0.1:8080}"
WORKFLOW_FILE="${3:-postpublish-sync.json}"

if [[ ! -f "$WORKFLOW_FILE" ]]; then
  echo "Missing workflow file: $WORKFLOW_FILE"
  exit 1
fi

curl4() {
  curl -4 -sS --http1.1 --retry 20 --retry-all-errors --retry-delay 2 "$@"
}

for _ in $(seq 1 60); do
  if curl4 --max-time 3 "$DV_URL/api/info/version" >/dev/null; then
    break
  fi
  sleep 2
done

CREATE_JSON="$(curl4 -X POST \
  -H "X-Dataverse-key: $DV_TOKEN" \
  -H "Content-Type: application/json" \
  --data-binary @"$WORKFLOW_FILE" \
  "$DV_URL/api/admin/workflows")"

WF_ID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["data"]["id"])' "$CREATE_JSON")"

curl4 -X PUT \
  -H "X-Dataverse-key: $DV_TOKEN" \
  -H "Content-Type: text/plain" \
  --data "$WF_ID" \
  "$DV_URL/api/admin/workflows/default/PostPublishDataset" >/dev/null

DEFAULTS="$(curl4 -H "X-Dataverse-key: $DV_TOKEN" "$DV_URL/api/admin/workflows/default/")"

echo "Bootstrapped workflow id=$WF_ID as default for PostPublishDataset"
echo "$DEFAULTS"

