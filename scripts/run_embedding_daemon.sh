#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EMBED_DIR="${ROOT_DIR}/embedding"
HOST="${EMBEDDING_API_HOST:-127.0.0.1}"
PORT="${EMBEDDING_API_PORT:-44512}"
MODEL_DIR="${EMBEDDING_MODEL_DIR:-${EMBED_DIR}/models/multilingual-e5-small}"
PYTHON_BIN="${EMBED_DIR}/.venv/bin/python"

if [[ -d "${EMBED_DIR}/.venv" ]]; then
  source "${EMBED_DIR}/.venv/bin/activate"
else
  PYTHON_BIN="python3"
fi

export PYTHONPATH="${ROOT_DIR}:${EMBED_DIR}:${PYTHONPATH:-}"
cd "${EMBED_DIR}"

exec "${PYTHON_BIN}" -m e5embed.daemon --host "${HOST}" --port "${PORT}" --model-dir "${MODEL_DIR}"
