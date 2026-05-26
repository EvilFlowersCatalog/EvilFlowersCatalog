#!/usr/bin/env sh
set -eu

echo "Starting dv_workflow_bootstrap"

if [ -z "${DATAVERSE_API_TOKEN:-}" ]; then
  echo "DATAVERSE_API_TOKEN is empty. Skipping Dataverse workflow bootstrap."
  exit 0
fi

echo "Using script: ${DATAVERSE_WORKFLOW_SCRIPT:-}"
echo "Using workflow file: ${DATAVERSE_WORKFLOW_FILE:-}"
echo "Using trigger: ${DATAVERSE_WORKFLOW_TRIGGER:-}"

if [ ! -f "${DATAVERSE_WORKFLOW_SCRIPT}" ]; then
  echo "Missing workflow script: ${DATAVERSE_WORKFLOW_SCRIPT}"
  exit 1
fi

if [ -n "${DATAVERSE_WORKFLOW_FILE:-}" ] && [ ! -f "${DATAVERSE_WORKFLOW_FILE}" ]; then
  echo "Missing workflow file: ${DATAVERSE_WORKFLOW_FILE}"
  exit 1
fi

until curl -fsS http://dataverse:8080/api/info/version >/dev/null 2>&1; do
  echo "Waiting for Dataverse API..."
  sleep 2
done

echo "Dataverse API is ready"

if [ -n "${DATAVERSE_WORKFLOW_FILE:-}" ]; then
  exec "${DATAVERSE_WORKFLOW_SCRIPT}" "${DATAVERSE_API_TOKEN}" --url http://dataverse:8080 --trigger "${DATAVERSE_WORKFLOW_TRIGGER}" --file "${DATAVERSE_WORKFLOW_FILE}"
else
  exec "${DATAVERSE_WORKFLOW_SCRIPT}" "${DATAVERSE_API_TOKEN}" --url http://dataverse:8080 --trigger "${DATAVERSE_WORKFLOW_TRIGGER}"
fi