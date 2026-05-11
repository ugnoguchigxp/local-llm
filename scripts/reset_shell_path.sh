#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_LLM_SCRIPTS="${ROOT_DIR}/scripts"
EMBED_BIN="${ROOT_DIR}/embedding/.venv/bin"

START_MARKER="# >>> local-llm PATH >>>"
END_MARKER="# <<< local-llm PATH <<<"

FILES=(
  "${HOME}/.zshrc"
  "${HOME}/.zprofile"
  "${HOME}/.bashrc"
  "${HOME}/.bash_profile"
)

cleanup_and_append() {
  local file="$1"
  touch "$file"

  # Remove previous managed block
  awk -v start="$START_MARKER" -v end="$END_MARKER" '
    $0 == start { in_block=1; next }
    $0 == end   { in_block=0; next }
    !in_block   { print }
  ' "$file" > "${file}.tmp"
  mv "${file}.tmp" "$file"

  # Remove old Gnosis PATH lines that pointed to deleted in-repo runtimes
  awk '
    /Code\/gnosis\/services\/local-llm\/scripts/ { next }
    /Code\/gnosis\/services\/embedding\/\.venv\/bin/ { next }
    /Code\/gnosis\/scripts/ { next }
    /# Gnosis Monorepo: local-llm scripts/ { next }
    /# Gnosis Monorepo: embedding tools/ { next }
    /# Gnosis Monorepo: root CLI wrappers/ { next }
    /Code\/localLlm\/scripts/ { next }
    { print }
  ' "$file" > "${file}.tmp"
  mv "${file}.tmp" "$file"

  # Append new managed block
  {
    echo ""
    echo "${START_MARKER}"
    echo "export PATH=\"${LOCAL_LLM_SCRIPTS}:${EMBED_BIN}:\$PATH\""
    echo "${END_MARKER}"
  } >> "$file"
}

for file in "${FILES[@]}"; do
  cleanup_and_append "$file"
done

echo "Updated shell PATH profiles:"
for file in "${FILES[@]}"; do
  echo "  - ${file}"
done

echo ""
echo "Expected command resolution after reloading shell:"
echo "  gemma4 -> ${LOCAL_LLM_SCRIPTS}/gemma4"
echo "  qwen   -> ${LOCAL_LLM_SCRIPTS}/qwen"
echo "  bonsai -> ${LOCAL_LLM_SCRIPTS}/bonsai"
echo "  embed  -> ${EMBED_BIN}/embed"
