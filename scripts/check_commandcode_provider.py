#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.commandcode_provider import (
    COMMANDCODE_UPSTREAM_MODEL_ID,
    CommandCodeConfig,
    CommandCodeProvider,
)


async def preflight(config: CommandCodeConfig) -> dict[str, object]:
    response = await CommandCodeProvider(config).list_models()
    result: dict[str, object] = {"statusCode": response.status_code}
    try:
        body = json.loads(response.content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        result["status"] = "invalid_response"
        return result
    models = body.get("data") if isinstance(body, dict) else None
    ids = {
        item.get("id")
        for item in models or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    result["modelAvailable"] = COMMANDCODE_UPSTREAM_MODEL_ID in ids
    result["status"] = "ready" if response.status_code == 200 and result["modelAvailable"] else "unavailable"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check Command Code proxy configuration without generating tokens.",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Fetch the upstream model catalog; this does not run a model turn.",
    )
    args = parser.parse_args()
    load_dotenv(REPO_ROOT / ".env")
    try:
        config = CommandCodeConfig.from_env()
    except ValueError as exc:
        print(json.dumps({"status": "invalid_config", "detail": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    report: dict[str, object] = config.status()
    exit_code = 0 if config.enabled and config.api_key else 1
    if args.preflight and exit_code == 0:
        try:
            preflight_report = asyncio.run(preflight(config))
            report["preflight"] = preflight_report
            if preflight_report.get("status") != "ready":
                exit_code = 1
        except Exception as exc:
            report["preflight"] = {"status": "error", "detail": str(exc)}
            exit_code = 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
