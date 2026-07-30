#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV_BIN="${UV_BIN:-$(command -v uv || true)}"
PYTHON_BIN="${SPEECH_PYTHON:-$(command -v python3.12 || true)}"

if [[ -z "${UV_BIN}" ]]; then
  echo "uv is required. Install it before running this script." >&2
  exit 1
fi

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "Python 3.12 is required. Install it with: brew install python@3.12" >&2
  exit 1
fi

"${UV_BIN}" venv --python "${PYTHON_BIN}" "${ROOT_DIR}/speech/.venv-tts"
"${UV_BIN}" venv --python "${PYTHON_BIN}" "${ROOT_DIR}/speech/.venv-asr"

"${UV_BIN}" pip sync \
  --python "${ROOT_DIR}/speech/.venv-tts/bin/python" \
  "${ROOT_DIR}/speech/requirements-tts.lock"
"${UV_BIN}" pip sync \
  --python "${ROOT_DIR}/speech/.venv-asr/bin/python" \
  "${ROOT_DIR}/speech/requirements-asr.lock"

echo "Speech environments are ready."
