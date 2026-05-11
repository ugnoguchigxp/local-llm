#!/usr/bin/env python3
"""CLI for local multilingual-e5-small embeddings."""
from __future__ import annotations

import argparse
import json
import os
import warnings
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 v2 only supports OpenSSL 1\.1\.1\+.*",
)

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_DIR = ROOT_DIR / "models" / "multilingual-e5-small"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="e5embed")
    parser.add_argument(
        "--model-dir",
        default=os.getenv("E5_MODEL_DIR", str(DEFAULT_MODEL_DIR)),
        help="Local model directory (default: models/multilingual-e5-small)",
    )
    parser.add_argument(
        "--type",
        choices=["query", "passage"],
        default="passage",
        help="E5 prefix type.",
    )
    parser.add_argument(
        "--text",
        action="append",
        required=True,
        help="Input text. Repeat --text for multiple inputs.",
    )
    parser.add_argument(
        "--no-normalize",
        action="store_true",
        help="Disable L2 normalization.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    return parser


def get_model_class():
    try:
        from urllib3.exceptions import NotOpenSSLWarning

        warnings.filterwarnings("ignore", category=NotOpenSSLWarning)
    except Exception:
        pass

    from sentence_transformers import SentenceTransformer

    return SentenceTransformer


def run(args: argparse.Namespace) -> list[dict]:
    model_dir = Path(args.model_dir)
    if not model_dir.exists():
        raise FileNotFoundError(
            f"Model not found: {model_dir}\\n"
            "Run: python scripts/download_model.py"
        )

    prefixed = [f"{args.type}: {t}" for t in args.text]

    model_cls = get_model_class()
    model = model_cls(str(model_dir))
    embeddings = model.encode(prefixed, normalize_embeddings=not args.no_normalize)

    return [
        {
            "input": src,
            "prefixed": p,
            "embedding": emb.tolist(),
            "dimension": len(emb),
        }
        for src, p, emb in zip(args.text, prefixed, embeddings)
    ]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rows = run(args)
    if args.pretty:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(rows, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
