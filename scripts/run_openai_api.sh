#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Load .env values for runtime options (auth, host/port, etc.)
if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  source "${ROOT_DIR}/.env"
  set +a
fi

HOST="${GEMMA4_API_HOST:-0.0.0.0}"
PORT="${GEMMA4_API_PORT:-44448}"

if [[ -d "${ROOT_DIR}/.venv" ]]; then
  # Ensure local venv binaries are preferred even if activate script is stale.
  export PATH="${ROOT_DIR}/.venv/bin:${PATH}"
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
# Stabilize startup behavior and reduce noisy one-time download logs.
export LOCAL_LLM_DAEMON_PRELOAD="${LOCAL_LLM_DAEMON_PRELOAD:-true}"
export HF_HUB_DISABLE_PROGRESS_BARS="${HF_HUB_DISABLE_PROGRESS_BARS:-1}"

# Use conservative defaults to reduce Metal GPU timeout crashes.
# Set LOCAL_LLM_GPU_SAFE_MODE=false to opt out.
GPU_SAFE_MODE="${LOCAL_LLM_GPU_SAFE_MODE:-true}"
if [[ "${GPU_SAFE_MODE}" == "true" ]]; then
  export LOCAL_LLM_PREFILL_STEP_SIZE="${LOCAL_LLM_PREFILL_STEP_SIZE:-2048}"
  export LOCAL_LLM_MAX_PROMPT_TOKENS="${LOCAL_LLM_MAX_PROMPT_TOKENS:-16384}"
  export LOCAL_LLM_MAX_OUTPUT_TOKENS="${LOCAL_LLM_MAX_OUTPUT_TOKENS:-256}"
  export LOCAL_LLM_CONTEXT_WINDOW="${LOCAL_LLM_CONTEXT_WINDOW:-32768}"
fi

cd "${ROOT_DIR}"

if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
  exec "${ROOT_DIR}/.venv/bin/python" -m uvicorn api.main:app --host "${HOST}" --port "${PORT}"
fi

exec python3 -m uvicorn api.main:app --host "${HOST}" --port "${PORT}"
