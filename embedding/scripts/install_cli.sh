#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt"
"$VENV_DIR/bin/pip" install -e "$PROJECT_DIR"

mkdir -p "$HOME/.local/bin"
ln -sf "$VENV_DIR/bin/e5embed" "$HOME/.local/bin/e5embed"
ln -sf "$VENV_DIR/bin/embed" "$HOME/.local/bin/embed"
rm -f "$HOME/.local/bin/emmbed"

echo "Installed: $HOME/.local/bin/e5embed -> $VENV_DIR/bin/e5embed"
echo "Installed: $HOME/.local/bin/embed -> $VENV_DIR/bin/embed"
