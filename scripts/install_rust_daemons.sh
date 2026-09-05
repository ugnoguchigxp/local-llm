#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
binary_root="${LOCAL_LLM_BIN_ROOT:-${HOME}/Library/Application Support/local-llm/bin}"

cargo build --release --workspace --manifest-path "${project_root}/Cargo.toml"
mkdir -p "${binary_root}"

install_binary() {
  local name="$1"
  install -m 0755 \
    "${project_root}/target/release/${name}" \
    "${binary_root}/${name}.part"
  mv "${binary_root}/${name}.part" "${binary_root}/${name}"
}

install_binary "ornith"
install_binary "embedding"

# Compatibility paths keep older ContextStill configurations working while
# the executable vnode still has the Activity Monitor-friendly target name.
ln -sfn "ornith" "${binary_root}/ornithd"
ln -sfn "embedding" "${binary_root}/embeddingd"
ln -sfn "ornith" "${project_root}/target/release/ornithd"
ln -sfn "embedding" "${project_root}/target/release/embeddingd"

echo "Installed ornith: ${binary_root}/ornith"
echo "Installed embedding: ${binary_root}/embedding"
