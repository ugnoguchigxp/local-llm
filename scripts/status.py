#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared.auth import auth_status, require_auth


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str


def load_env(root_dir: Path) -> None:
    env_path = root_dir / ".env"
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def make_headers(include_auth: bool) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    if include_auth:
        token = os.getenv("LOCAL_LLM_ACCESS_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def request_json(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 3.0,
) -> tuple[int, dict[str, Any] | list[Any] | str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url=url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            try:
                parsed = json.loads(payload) if payload else {}
            except json.JSONDecodeError:
                parsed = payload
            return response.getcode(), parsed
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8") if exc.fp else ""
        try:
            parsed = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            parsed = payload
        return exc.code, parsed
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)


def check_auth_config() -> CheckResult:
    status = auth_status()
    if status["status"] == "error":
        return CheckResult("auth config", "fail", str(status["message"]))
    if status["required"]:
        return CheckResult("auth config", "ok", "LOCAL_LLM_REQUIRE_AUTH=true")
    return CheckResult("auth config", "warn", "LOCAL_LLM_REQUIRE_AUTH=false")


def check_llm_health(base_url: str, timeout: float) -> CheckResult:
    code, payload = request_json("GET", f"{base_url}/health", timeout=timeout)
    if code == 200:
        state = payload.get("status") if isinstance(payload, dict) else None
        return CheckResult("llm health", "ok", f"{code} status={state}")
    return CheckResult("llm health", "fail", f"HTTP {code}")


def check_llm_models(base_url: str, timeout: float) -> CheckResult:
    code, payload = request_json(
        "GET",
        f"{base_url}/v1/models",
        headers=make_headers(require_auth()),
        timeout=timeout,
    )
    if code == 200:
        size = len(payload.get("data", [])) if isinstance(payload, dict) else "?"
        return CheckResult("llm models", "ok", f"HTTP 200 models={size}")
    return CheckResult("llm models", "fail", f"HTTP {code}")


def check_embedding_health(base_url: str, timeout: float) -> CheckResult:
    code, payload = request_json("GET", f"{base_url}/health", timeout=timeout)
    if code == 200:
        queue_size = payload.get("queueSize") if isinstance(payload, dict) else None
        return CheckResult("embedding health", "ok", f"{code} queueSize={queue_size}")
    return CheckResult("embedding health", "fail", f"HTTP {code}")


def check_embedding_embed(base_url: str, timeout: float) -> CheckResult:
    code, payload = request_json(
        "POST",
        f"{base_url}/embed",
        body={"texts": ["status probe"], "type": "query", "normalize": True, "priority": "normal"},
        headers=make_headers(require_auth()),
        timeout=timeout,
    )
    if code == 200:
        if isinstance(payload, dict) and isinstance(payload.get("embeddings"), list):
            dimension = payload.get("dimension")
            return CheckResult("embedding /embed", "ok", f"HTTP 200 dimension={dimension}")
        return CheckResult("embedding /embed", "fail", "HTTP 200 but invalid response shape")
    return CheckResult("embedding /embed", "fail", f"HTTP {code}")


def summarize(checks: list[CheckResult]) -> str:
    if any(check.status == "fail" for check in checks):
        return "fail"
    if any(check.status == "warn" for check in checks):
        return "warn"
    return "ok"


def print_human(checks: list[CheckResult], overall: str) -> None:
    print(f"[{overall.upper()}] local-llm runtime status")
    for check in checks:
        print(f"- [{check.status.upper()}] {check.name}: {check.detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="local-llm self diagnostics")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on warning")
    parser.add_argument("--timeout", type=float, default=3.0, help="HTTP timeout seconds")
    args = parser.parse_args(argv)

    load_env(ROOT_DIR)

    llm_host = os.getenv("GEMMA4_API_HOST", "127.0.0.1")
    llm_port = os.getenv("GEMMA4_API_PORT", "44448")
    llm_base = f"http://{llm_host}:{llm_port}"

    embed_host = os.getenv("EMBEDDING_API_HOST", "127.0.0.1")
    embed_port = os.getenv("EMBEDDING_API_PORT", "44512")
    embed_base = f"http://{embed_host}:{embed_port}"

    checks = [
        check_auth_config(),
        check_llm_health(llm_base, args.timeout),
        check_llm_models(llm_base, args.timeout),
        check_embedding_health(embed_base, args.timeout),
        check_embedding_embed(embed_base, args.timeout),
    ]
    overall = summarize(checks)

    result = {
        "status": overall,
        "auth": auth_status(),
        "endpoints": {
            "llm": llm_base,
            "embedding": embed_base,
        },
        "checks": [
            {"name": check.name, "status": check.status, "detail": check.detail} for check in checks
        ],
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print_human(checks, overall)

    if overall == "fail":
        return 1
    if args.strict and overall == "warn":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
