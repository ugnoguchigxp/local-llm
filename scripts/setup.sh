#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== Local LLM Agent Setup ==="

# 1. 依存コマンドの確認
echo "[1/4] Checking prerequisites..."
for cmd in git python3; do
  if ! command -v $cmd >/dev/null 2>&1; then
    echo "Error: $cmd is not installed." >&2
    exit 1
  fi
done

# 2. 仮想環境の作成
echo "[2/4] Preparing Python virtual environment..."
if [[ ! -d "${ROOT_DIR}/.venv" ]]; then
  python3 -m venv "${ROOT_DIR}/.venv"
  echo "Created virtual environment in ${ROOT_DIR}/.venv"
fi

source "${ROOT_DIR}/.venv/bin/activate"

# 3. ライブラリのインストール
echo "[3/4] Installing dependencies from requirements.txt..."
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r "${ROOT_DIR}/requirements.txt"

# 4. Ollama の確認 (任意)
echo "[4/4] Checking Ollama status..."
if command -v ollama >/dev/null 2>&1; then
  echo "Ollama is installed. You can use 'ollama-v4' script."
else
  echo "Note: Ollama is not found in PATH. Please install it if you want to use the Ollama backend."
fi

# スクリプトに実行権限を付与
chmod +x "${ROOT_DIR}"/scripts/*

echo ""
echo "Setup completed successfully!"
echo "Next steps:"
echo "  1. Run './scripts/install_path.sh' to add commands to your PATH."
echo "  2. Edit '.env' and add your API keys (like BRAVE_SEARCH_API_KEY)."
echo "  3. Start chatting with './scripts/gemma4', './scripts/bonsai', or './scripts/bedrock'."
echo ""
