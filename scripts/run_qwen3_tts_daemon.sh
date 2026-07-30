#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${ROOT_DIR}/speech/.venv-tts/bin/python"

if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  source "${ROOT_DIR}/.env"
  set +a
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "TTS environment is missing. Run scripts/setup_speech_envs.sh." >&2
  exit 1
fi

export PYTHONUNBUFFERED=1
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"
export TOKENIZERS_PARALLELISM="${TOKENIZERS_PARALLELISM:-false}"

cd "${ROOT_DIR}"
exec "${PYTHON_BIN}" -m uvicorn speech.tts.app:app \
  --host "${QWEN3_TTS_HOST:-127.0.0.1}" \
  --port "${QWEN3_TTS_PORT:-44520}" \
  --no-access-log
