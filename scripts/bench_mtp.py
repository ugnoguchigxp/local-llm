#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from core.model import MLXModelManager


def _build_messages(prompt: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "You are a concise coding assistant."},
        {"role": "user", "content": prompt},
    ]


def _run_one(manager: MLXModelManager, messages: list[dict[str, str]], max_tokens: int) -> dict[str, Any]:
    started = time.perf_counter()
    chunks = list(
        manager.generate_stream(
            messages,
            max_tokens=max_tokens,
            temperature=0.0,
        )
    )
    elapsed = time.perf_counter() - started
    text = "".join(chunks)
    stats = manager.last_generation_stats() or {}
    completion_tokens = int(stats.get("completion_tokens") or 0)
    gen_tps = float(stats.get("generation_tps") or 0.0)
    observed_tps = (completion_tokens / elapsed) if elapsed > 0 and completion_tokens > 0 else 0.0
    return {
        "elapsed_sec": elapsed,
        "completion_tokens": completion_tokens,
        "generation_tps": gen_tps,
        "observed_tps": observed_tps,
        "output_chars": len(text),
        "stats": stats,
    }


def _bench(
    *,
    mtp_enabled: bool,
    repeats: int,
    max_tokens: int,
    prompt: str,
    warmup: int,
) -> dict[str, Any]:
    manager = MLXModelManager(mtp_enabled=mtp_enabled)
    messages = _build_messages(prompt)

    warmups: list[dict[str, Any]] = []
    for _ in range(warmup):
        warmups.append(_run_one(manager, messages, max_tokens=max_tokens))

    runs: list[dict[str, Any]] = []
    for _ in range(repeats):
        runs.append(_run_one(manager, messages, max_tokens=max_tokens))

    elapsed_list = [r["elapsed_sec"] for r in runs]
    gen_tps_list = [r["generation_tps"] for r in runs if r["generation_tps"] > 0]
    observed_tps_list = [r["observed_tps"] for r in runs if r["observed_tps"] > 0]

    return {
        "mtp_enabled": mtp_enabled,
        "repeats": repeats,
        "warmup_runs": warmups,
        "runs": runs,
        "summary": {
            "elapsed_mean_sec": statistics.fmean(elapsed_list) if elapsed_list else None,
            "elapsed_min_sec": min(elapsed_list) if elapsed_list else None,
            "elapsed_max_sec": max(elapsed_list) if elapsed_list else None,
            "generation_tps_mean": statistics.fmean(gen_tps_list) if gen_tps_list else None,
            "observed_tps_mean": statistics.fmean(observed_tps_list) if observed_tps_list else None,
            "completion_tokens_mean": (
                statistics.fmean([r["completion_tokens"] for r in runs]) if runs else None
            ),
        },
        "health": manager.health(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark Gemma4 with/without MTP")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument(
        "--prompt",
        type=str,
        default=(
            "Write a concise but complete implementation plan to add file-edit tool support "
            "to an OpenAI-compatible API server, including tests."
        ),
    )
    args = parser.parse_args()

    result = {
        "env": {
            "GEMMA4_MODEL": os.getenv("GEMMA4_MODEL"),
            "GEMMA4_DRAFT_MODEL": os.getenv("GEMMA4_DRAFT_MODEL"),
            "LOCAL_LLM_PREFILL_STEP_SIZE": os.getenv("LOCAL_LLM_PREFILL_STEP_SIZE"),
            "LOCAL_LLM_CONTEXT_WINDOW": os.getenv("LOCAL_LLM_CONTEXT_WINDOW"),
        },
        "off": _bench(
            mtp_enabled=False,
            repeats=max(1, args.repeats),
            max_tokens=max(1, args.max_tokens),
            prompt=args.prompt,
            warmup=max(0, args.warmup),
        ),
        "on": _bench(
            mtp_enabled=True,
            repeats=max(1, args.repeats),
            max_tokens=max(1, args.max_tokens),
            prompt=args.prompt,
            warmup=max(0, args.warmup),
        ),
    }

    off_tps = result["off"]["summary"]["observed_tps_mean"] or 0.0
    on_tps = result["on"]["summary"]["observed_tps_mean"] or 0.0
    speedup = (on_tps / off_tps) if off_tps > 0 else None
    result["speedup_observed_tps"] = speedup
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
