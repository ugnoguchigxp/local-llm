#!/usr/bin/env python3
"""Download intfloat/multilingual-e5-small to a local directory."""

from pathlib import Path

from huggingface_hub import snapshot_download

MODEL_ID = "intfloat/multilingual-e5-small"
LOCAL_DIR = Path(__file__).resolve().parent.parent / "models" / "multilingual-e5-small"
ALLOW_PATTERNS = [
    "1_Pooling/config.json",
    "README.md",
    "config.json",
    "config_sentence_transformers.json",
    "model.safetensors",
    "modules.json",
    "sentence_bert_config.json",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
]


def main() -> None:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    path = snapshot_download(
        repo_id=MODEL_ID,
        local_dir=str(LOCAL_DIR),
        allow_patterns=ALLOW_PATTERNS,
    )
    print(f"Downloaded to: {path}")


if __name__ == "__main__":
    main()
