#!/usr/bin/env bash
set -euo pipefail

revision="d184053f2441f897cf81429b98b0d868f4d96ff3"
runtime_root="${LOCAL_LLM_RUNTIME_ROOT:-${HOME}/Library/Application Support/local-llm/runtime/mistralrs-v0.9.2}"
binary="${runtime_root}/bin/mistralrs"
revision_file="${runtime_root}/.revision"

if [[ -x "${binary}" ]] \
  && [[ "$("${binary}" --version)" == "mistralrs 0.9.2" ]] \
  && [[ -f "${revision_file}" ]] \
  && [[ "$(<"${revision_file}")" == "${revision}" ]]; then
  echo "mistralrs 0.9.2 is already installed: ${binary}"
  exit 0
fi

cargo +1.98.0 install \
  --git https://github.com/EricLBuehler/mistral.rs.git \
  --rev "${revision}" \
  --locked \
  --root "${runtime_root}" \
  --no-default-features \
  --features metal \
  mistralrs-cli

"${binary}" --version
printf '%s\n' "${revision}" >"${revision_file}"
