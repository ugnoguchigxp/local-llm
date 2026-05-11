#!/usr/bin/env python3
"""Simple one-liner CLI: embed \"text\"."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from e5embed import cli as core


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="embed")
    parser.add_argument(
        "text",
        help="Text to embed.",
    )
    parser.add_argument(
        "--model-dir",
        default=os.getenv("E5_MODEL_DIR", str(core.DEFAULT_MODEL_DIR)),
        help="Local model directory (default: models/multilingual-e5-small)",
    )
    parser.add_argument(
        "--type",
        choices=["query", "passage"],
        default="passage",
        help="E5 prefix type (default: passage).",
    )
    parser.add_argument(
        "--no-normalize",
        action="store_true",
        help="Disable L2 normalization.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    model_dir = Path(args.model_dir)
    if not model_dir.exists():
        raise FileNotFoundError(
            f"Model not found: {model_dir}\\n"
            "Run: python scripts/download_model.py"
        )

    model_cls = core.get_model_class()
    model = model_cls(str(model_dir))
    vector = model.encode(
        [f"{args.type}: {args.text}"],
        normalize_embeddings=not args.no_normalize,
    )[0]
    print(json.dumps(vector.tolist(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
