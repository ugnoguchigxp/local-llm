#!/usr/bin/env bash
set -euo pipefail

revision="d184053f2441f897cf81429b98b0d868f4d96ff3"
runtime_root="${LOCAL_LLM_RUNTIME_ROOT:-${HOME}/Library/Application Support/local-llm/runtime/mistralrs-v0.9.2}"
engine_binary="${runtime_root}/bin/ornith-engine"
legacy_binary="${runtime_root}/bin/mistralrs"
revision_file="${runtime_root}/.revision"

is_expected_version() {
  local candidate="$1"
  [[ -x "${candidate}" ]] \
    && [[ "$("${candidate}" --version)" == "mistralrs 0.9.2" ]]
}

install_legacy_alias() {
  mkdir -p "${runtime_root}/bin"
  ln -sfn "ornith-engine" "${legacy_binary}"
}

if is_expected_version "${engine_binary}" \
  && [[ -f "${revision_file}" ]] \
  && [[ "$(<"${revision_file}")" == "${revision}" ]]; then
  install_legacy_alias
  echo "Ornith engine is already installed: ${engine_binary}"
  exit 0
fi

if [[ ! -e "${engine_binary}" ]] \
  && [[ ! -L "${legacy_binary}" ]] \
  && is_expected_version "${legacy_binary}" \
  && [[ -f "${revision_file}" ]] \
  && [[ "$(<"${revision_file}")" == "${revision}" ]]; then
  mv "${legacy_binary}" "${engine_binary}"
  install_legacy_alias
  echo "Migrated mistralrs process name to ornith-engine: ${engine_binary}"
  exit 0
fi

build_root="$(mktemp -d)"
cleanup() {
  rm -rf "${build_root}"
}
trap cleanup EXIT
cargo +1.98.0 install \
  --git https://github.com/EricLBuehler/mistral.rs.git \
  --rev "${revision}" \
  --locked \
  --root "${build_root}" \
  --no-default-features \
  --features metal \
  mistralrs-cli

mkdir -p "${runtime_root}/bin"
install -m 0755 "${build_root}/bin/mistralrs" "${engine_binary}.part"
mv "${engine_binary}.part" "${engine_binary}"
install_legacy_alias
"${engine_binary}" --version
printf '%s\n' "${revision}" >"${revision_file}"
