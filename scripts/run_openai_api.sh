#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${GEMMA4_API_HOST:-0.0.0.0}"
PORT="${GEMMA4_API_PORT:-44448}"

if [[ -d "${ROOT_DIR}/.venv" ]]; then
  source "${ROOT_DIR}/.venv/bin/activate"
fi

# Load local .env to check if the daemon should be enabled
if [[ -f "${ROOT_DIR}/.env" ]]; then
  ENABLED=$(grep "^LOCAL_LLM_ENABLED=" "${ROOT_DIR}/.env" | cut -d'=' -f2 | tr -d ' \r\n' || true)
  if [[ -z "${ENABLED}" ]]; then
    ENABLED=$(grep "^GNOSIS_LOCAL_LLM_ENABLED=" "${ROOT_DIR}/.env" | cut -d'=' -f2 | tr -d ' \r\n' || true)
  fi
  if [[ "${ENABLED}" == "false" ]]; then
    echo "Local LLM API is disabled via LOCAL_LLM_ENABLED=false. Exiting."
    exit 0
  fi
fi

export PYTHONPATH="${ROOT_DIR}:${PYTHONPATH:-}"
cd "${ROOT_DIR}"

exec uvicorn api.main:app --host "${HOST}" --port "${PORT}"
