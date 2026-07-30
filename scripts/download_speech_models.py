#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from huggingface_hub import snapshot_download

ROOT_DIR = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT_DIR / "speech" / "models.lock.json"
DEFAULT_DATA_ROOT = (
    Path.home() / "Library" / "Application Support" / "local-llm" / "speech"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download revision-pinned Qwen3 speech models."
    )
    parser.add_argument(
        "--component",
        choices=("all", "tts", "asr"),
        default="all",
    )
    parser.add_argument(
        "--include-quality-reference",
        action="store_true",
        help="Also download the optional ASR BF16 comparison model.",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path(
            os.getenv("SPEECH_DATA_ROOT", str(DEFAULT_DATA_ROOT))
        ).expanduser(),
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Hash every downloaded file after download.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    selected = []
    for model in lock["models"]:
        if args.component != "all" and model["component"] != args.component:
            continue
        if not model.get("required", False) and not args.include_quality_reference:
            continue
        selected.append(model)

    args.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest_path = args.data_root / "models.manifest.json"
    existing_records: dict[str, dict[str, Any]] = {}
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing.get("data_root") == str(args.data_root):
                existing_records = {
                    record["repo_id"]: record
                    for record in existing.get("models", [])
                    if isinstance(record, dict) and record.get("repo_id")
                }
        except (OSError, json.JSONDecodeError):
            existing_records = {}

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "downloaded_at": int(time.time()),
        "data_root": str(args.data_root),
        "models": [],
    }

    for index, model in enumerate(selected, start=1):
        destination = args.data_root / model["directory"]
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        print(
            f"[{index}/{len(selected)}] {model['repo_id']} "
            f"@ {model['revision']} -> {destination}",
            flush=True,
        )
        snapshot_download(
            repo_id=model["repo_id"],
            revision=model["revision"],
            local_dir=destination,
        )
        record = {
            **model,
            "local_path": str(destination),
            "files": _file_records(destination, include_hash=args.verify),
        }
        record["total_bytes"] = sum(item["size"] for item in record["files"])
        existing_records[model["repo_id"]] = record
        manifest["models"] = _ordered_records(lock, existing_records)
        _write_manifest(args.data_root, manifest)

    manifest["models"] = _ordered_records(lock, existing_records)
    _write_manifest(args.data_root, manifest)
    print(f"Model manifest: {args.data_root / 'models.manifest.json'}")
    return 0


def _file_records(root: Path, *, include_hash: bool) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root)
        record: dict[str, Any] = {
            "path": str(relative),
            "size": path.stat().st_size,
        }
        if include_hash:
            record["sha256"] = _sha256(path)
        records.append(record)
    return records


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(data_root: Path, manifest: dict[str, Any]) -> None:
    destination = data_root / "models.manifest.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    destination.chmod(0o600)


def _ordered_records(
    lock: dict[str, Any],
    records: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {**records[model["repo_id"]], **model}
        for model in lock["models"]
        if model["repo_id"] in records
    ]


if __name__ == "__main__":
    sys.exit(main())
