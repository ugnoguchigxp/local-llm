#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
BENCH_SCRIPT = ROOT_DIR / "scripts" / "bench_mtp.py"
ENV_FILE = ROOT_DIR / ".env"


def _read_env_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _upsert_env_key(lines: list[str], key: str, value: str) -> list[str]:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    out: list[str] = []
    replaced = False
    for line in lines:
        if pattern.match(line):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    return out


def _run_benchmark(repeats: int, warmup: int, max_tokens: int) -> dict:
    proc = subprocess.run(
        [
            sys.executable,
            str(BENCH_SCRIPT),
            "--repeats",
            str(repeats),
            "--warmup",
            str(warmup),
            "--max-tokens",
            str(max_tokens),
        ],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "benchmark failed")
    return json.loads(proc.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run MTP on/off benchmark and toggle GEMMA4_MTP_ENABLED in .env"
    )
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--min-speedup", type=float, default=1.10)
    args = parser.parse_args()

    result = _run_benchmark(
        repeats=max(1, args.repeats),
        warmup=max(0, args.warmup),
        max_tokens=max(1, args.max_tokens),
    )
    off_tps = float(result.get("off", {}).get("summary", {}).get("observed_tps_mean") or 0.0)
    on_tps = float(result.get("on", {}).get("summary", {}).get("observed_tps_mean") or 0.0)
    speedup = (on_tps / off_tps) if off_tps > 0 else 0.0

    enable = speedup >= args.min_speedup
    value = "true" if enable else "false"
    lines = _read_env_lines(ENV_FILE)
    lines = _upsert_env_key(lines, "GEMMA4_MTP_ENABLED", value)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "benchmark": result,
                "decision": {
                    "min_speedup": args.min_speedup,
                    "off_observed_tps_mean": off_tps,
                    "on_observed_tps_mean": on_tps,
                    "speedup": speedup,
                    "set_GEMMA4_MTP_ENABLED": value,
                },
                "updated_env": str(ENV_FILE),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
