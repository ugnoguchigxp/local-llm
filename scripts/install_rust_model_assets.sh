#!/usr/bin/env bash
set -euo pipefail

model_root="${LOCAL_LLM_MODEL_ROOT:-${HOME}/Library/Application Support/local-llm/models}"
embedding_dir="${model_root}/multilingual-e5-small-onnx-qint8"
ornith_dir="${model_root}/ornith-1.0-9b-gguf-q4km"
embedding_revision="614241f622f53c4eeff9890bdc4f31cfecc418b3"
ornith_revision="3296bc7a404871a72ac3f1903f561459c09b5c17"

download() {
  local url="$1"
  local destination="$2"
  mkdir -p "$(dirname "${destination}")"
  curl --location --fail --retry 5 --continue-at - --output "${destination}.part" "${url}"
  mv "${destination}.part" "${destination}"
}

install_embedding() {
  local base="https://huggingface.co/intfloat/multilingual-e5-small/resolve/${embedding_revision}/onnx"
  local file
  mkdir -p "${embedding_dir}"
  for file in config.json sentencepiece.bpe.model special_tokens_map.json tokenizer.json tokenizer_config.json; do
    if [[ ! -f "${embedding_dir}/${file}" ]]; then
      download "${base}/${file}" "${embedding_dir}/${file}"
    fi
  done
  if [[ ! -f "${embedding_dir}/model.onnx" ]]; then
    download "${base}/model_qint8_avx512_vnni.onnx" "${embedding_dir}/model.onnx"
  fi
}

install_ornith() {
  local file="ornith-1.0-9b-Q4_K_M.gguf"
  local base="https://huggingface.co/ornith-ai/Ornith-1.0-9B-GGUF/resolve/${ornith_revision}"
  mkdir -p "${ornith_dir}"
  if [[ ! -f "${ornith_dir}/${file}" ]]; then
    download "${base}/${file}" "${ornith_dir}/${file}"
  fi
}

case "${1:-all}" in
  embedding) install_embedding ;;
  ornith) install_ornith ;;
  all)
    install_embedding
    install_ornith
    ;;
  *)
    echo "usage: $0 [all|embedding|ornith]" >&2
    exit 64
    ;;
esac
