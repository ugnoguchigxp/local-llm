#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
user_id="$(id -u)"
launch_domain="gui/${user_id}"
launch_agent_dir="${HOME}/Library/LaunchAgents"
app_root="${HOME}/Library/Application Support/local-llm"
binary_root="${LOCAL_LLM_BIN_ROOT:-${app_root}/bin}"
runtime_root="${LOCAL_LLM_RUNTIME_ROOT:-${app_root}/runtime/mistralrs-v0.9.2}"
model_root="${LOCAL_LLM_MODEL_ROOT:-${app_root}/models}"
log_root="${LOCAL_LLM_LOG_ROOT:-${HOME}/Library/Logs/local-llm}"
desired_state_file="${ORNITHD_DESIRED_STATE_FILE:-${HOME}/Library/Application Support/contextStill/run/local-model-desired.json}"

embedding_label="com.local-llm.embedding"
ornith_label="com.local-llm.ornith"
embedding_plist="${launch_agent_dir}/${embedding_label}.plist"
ornith_plist="${launch_agent_dir}/${ornith_label}.plist"

write_plist() {
  local plist="$1"
  local label="$2"
  local program="$3"
  local log_file="$4"
  shift 4
  local temporary="${plist}.tmp"
  local index=0

  /usr/bin/plutil -create xml1 "${temporary}"
  /usr/bin/plutil -insert Label -string "${label}" "${temporary}"
  /usr/bin/plutil -insert ProgramArguments -array "${temporary}"
  /usr/bin/plutil -insert "ProgramArguments.${index}" -string "${program}" "${temporary}"
  index=$((index + 1))
  for argument in "$@"; do
    /usr/bin/plutil -insert "ProgramArguments.${index}" -string "${argument}" "${temporary}"
    index=$((index + 1))
  done
  /usr/bin/plutil -insert WorkingDirectory -string "${project_root}" "${temporary}"
  /usr/bin/plutil -insert RunAtLoad -bool true "${temporary}"
  /usr/bin/plutil -insert KeepAlive -bool true "${temporary}"
  /usr/bin/plutil -insert ProcessType -string Background "${temporary}"
  /usr/bin/plutil -insert ThrottleInterval -integer 10 "${temporary}"
  /usr/bin/plutil -insert StandardOutPath -string "${log_file}" "${temporary}"
  /usr/bin/plutil -insert StandardErrorPath -string "${log_file}" "${temporary}"
  /usr/bin/plutil -insert EnvironmentVariables -dictionary "${temporary}"
  /usr/bin/plutil -insert EnvironmentVariables.HOME -string "${HOME}" "${temporary}"
  /usr/bin/plutil -insert EnvironmentVariables.PATH -string "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" "${temporary}"
  chmod 0644 "${temporary}"
  mv "${temporary}" "${plist}"
}

install_plists() {
  mkdir -p "${launch_agent_dir}" "${log_root}"
  write_plist \
    "${embedding_plist}" \
    "${embedding_label}" \
    "${binary_root}/embedding" \
    "${log_root}/embedding.log" \
    --host 127.0.0.1 \
    --port 44512 \
    --model-dir "${model_root}/multilingual-e5-small-onnx-qint8" \
    --manifest "${project_root}/models/manifests/multilingual-e5-small-onnx-qint8.json"

  write_plist \
    "${ornith_plist}" \
    "${ornith_label}" \
    "${binary_root}/ornith" \
    "${log_root}/ornith.log" \
    --mistralrs-bin "${runtime_root}/bin/ornith-engine" \
    --model-file "${model_root}/ornith-1.0-9b-gguf-q4km/ornith-1.0-9b-Q4_K_M.gguf" \
    --manifest "${project_root}/models/manifests/ornith-1.0-9b-gguf-q4km.json" \
    --host 127.0.0.1 \
    --port 44448 \
    --profile standard \
    --long-idle-seconds 30 \
    --desired-state-file "${desired_state_file}"
}

bootout_if_loaded() {
  local label="$1"
  /bin/launchctl bootout "${launch_domain}/${label}" >/dev/null 2>&1 || true
}

enable_label() {
  local label="$1"
  /bin/launchctl enable "${launch_domain}/${label}"
}

disable_label() {
  local label="$1"
  /bin/launchctl disable "${launch_domain}/${label}"
}

wait_for_health() {
  local service="$1"
  local port="$2"
  local attempt
  for attempt in $(seq 1 60); do
    if /usr/bin/curl --silent --fail --max-time 2 "http://127.0.0.1:${port}/health" >/dev/null; then
      echo "${service}: ready on 127.0.0.1:${port}"
      return 0
    fi
    sleep 1
  done
  echo "${service}: health check timed out" >&2
  return 1
}

start_daemons() {
  "${project_root}/scripts/install_rust_daemons.sh"
  "${project_root}/scripts/install_mistralrs.sh"
  install_plists
  bootout_if_loaded "${embedding_label}"
  bootout_if_loaded "${ornith_label}"
  enable_label "${embedding_label}"
  enable_label "${ornith_label}"
  /bin/launchctl bootstrap "${launch_domain}" "${embedding_plist}"
  /bin/launchctl bootstrap "${launch_domain}" "${ornith_plist}"
  wait_for_health embedding 44512
  wait_for_health ornith 44448
}

stop_daemons() {
  bootout_if_loaded "${ornith_label}"
  bootout_if_loaded "${embedding_label}"
  disable_label "${ornith_label}"
  disable_label "${embedding_label}"
  echo "ornith and embedding stopped until the next explicit start"
}

status_one() {
  local service="$1"
  local label="$2"
  local port="$3"
  if /bin/launchctl print "${launch_domain}/${label}" >/dev/null 2>&1; then
    if /usr/bin/curl --silent --fail --max-time 2 "http://127.0.0.1:${port}/health" >/dev/null; then
      echo "${service}: running and ready"
    else
      echo "${service}: running but not ready"
    fi
  else
    echo "${service}: stopped"
  fi
}

case "${1:-status}" in
  start)
    start_daemons
    ;;
  stop)
    stop_daemons
    ;;
  restart)
    stop_daemons
    start_daemons
    ;;
  status)
    status_one embedding "${embedding_label}" 44512
    status_one ornith "${ornith_label}" 44448
    ;;
  *)
    echo "usage: $0 [start|stop|restart|status]" >&2
    exit 64
    ;;
esac
