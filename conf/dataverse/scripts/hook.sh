#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 <DATAVERSE_API_TOKEN> [-u|--url DATAVERSE_URL] [-f|--file WORKFLOW_JSON] [-t|--trigger pre|post]"
  echo "Examples:"
  echo "  $0 \"xxxxxxxx\" --url http://127.0.0.1:8080 --trigger pre --file my-workflow.json"
}

if [[ $# -lt 1 ]]; then
  usage
  exit 1
fi

DV_TOKEN="$1"
shift

DV_URL="http://127.0.0.1:8080"
WORKFLOW_FILE=""
TRIGGER="post"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -u|--url)
      DV_URL="${2:-}"
      shift 2
      ;;
    -f|--file)
      WORKFLOW_FILE="${2:-}"
      shift 2
      ;;
    -t|--trigger)
      TRIGGER="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

case "$TRIGGER" in
  pre)  EVENT="PrePublishDataset" ;;
  post) EVENT="PostPublishDataset" ;;
  *)
    echo "Invalid trigger: $TRIGGER (expected: pre or post)"
    exit 1
    ;;
esac

if [[ -z "$WORKFLOW_FILE" ]]; then
  if [[ "$TRIGGER" == "pre" ]]; then
    WORKFLOW_FILE="prepublish-sync.json"
  else
    WORKFLOW_FILE="postpublish-sync.json"
  fi
fi

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
  "$DV_URL/api/admin/workflows/default/$EVENT" >/dev/null

DEFAULTS="$(curl4 -H "X-Dataverse-key: $DV_TOKEN" "$DV_URL/api/admin/workflows/default/")"

echo "Bootstrapped workflow id=$WF_ID as default for $EVENT using file=$WORKFLOW_FILE"
echo "$DEFAULTS"
