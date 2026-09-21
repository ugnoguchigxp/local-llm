#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.auth import is_truthy


TERMINAL_EVENTS = {"turn.completed", "turn.cancelled", "turn.failed", "turn.unqueued"}
ACTIVE_SESSION_STATES = {"running", "waiting_for_approval", "waiting_for_input"}


def headers(*, idempotency: bool = False) -> dict[str, str]:
    result = {"Accept": "application/json"}
    token = os.getenv("LOCAL_LLM_ACCESS_TOKEN", "").strip()
    if token:
        result["Authorization"] = f"Bearer {token}"
    if idempotency:
        result["Idempotency-Key"] = f"grok-smoke-{uuid.uuid4()}"
    return result


def require_ok(response: requests.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{response.request.method} {response.url}: invalid JSON") from exc
    if not response.ok:
        raise RuntimeError(
            f"{response.request.method} {response.url}: HTTP {response.status_code}: "
            f"{json.dumps(body, ensure_ascii=False)}"
        )
    if not isinstance(body, dict):
        raise RuntimeError(f"{response.url}: response must be an object")
    return body


def iter_sse(response: requests.Response) -> Iterator[dict[str, Any]]:
    event_type = "message"
    data_lines: list[str] = []
    for raw in response.iter_lines(decode_unicode=True):
        line = raw or ""
        if not line:
            if data_lines:
                event = json.loads("\n".join(data_lines))
                if isinstance(event, dict):
                    event.setdefault("type", event_type)
                    yield event
            event_type, data_lines = "message", []
        elif not line.startswith(":"):
            field, _, value = line.partition(":")
            if field == "event":
                event_type = value.lstrip()
            elif field == "data":
                data_lines.append(value.lstrip())


def wait_for_terminal(
    base_url: str,
    session_id: str,
    cursor: str,
    *,
    approval_decision: str | None,
    timeout_seconds: int,
) -> tuple[dict[str, Any], str]:
    deadline = time.monotonic() + timeout_seconds
    with requests.get(
        f"{base_url}/v1/agents/sessions/{session_id}/events",
        params={"after": cursor},
        headers={**headers(), "Accept": "text/event-stream"},
        stream=True,
        timeout=(10, timeout_seconds + 10),
    ) as response:
        response.raise_for_status()
        for event in iter_sse(response):
            cursor = str(event.get("cursor", cursor))
            if event.get("type") == "approval.requested":
                data = event.get("data")
                approval_id = data.get("approval_id") if isinstance(data, dict) else None
                if not approval_decision:
                    raise RuntimeError("Grok requested approval but no decision was configured")
                if not isinstance(approval_id, str):
                    raise RuntimeError("approval.requested omitted approval_id")
                require_ok(
                    requests.post(
                        f"{base_url}/v1/agents/sessions/{session_id}"
                        f"/approvals/{approval_id}/decision",
                        headers=headers(idempotency=True),
                        json={"decision": approval_decision},
                        timeout=30,
                    )
                )
            if event.get("type") in TERMINAL_EVENTS:
                return event, cursor
            if time.monotonic() >= deadline:
                raise TimeoutError("Grok smoke turn did not reach a terminal event")
    raise RuntimeError("Grok event stream ended before a terminal event")


def wait_for_session_settled(base_url: str, session_id: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        session = require_ok(
            requests.get(
                f"{base_url}/v1/agents/sessions/{session_id}",
                headers=headers(),
                timeout=10,
            )
        )
        if session.get("status") not in ACTIVE_SESSION_STATES:
            return
        time.sleep(0.2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit opt-in Grok subscription smoke test.")
    parser.add_argument(
        "--base-url", default=os.getenv("LOCAL_LLM_API_BASE", "http://127.0.0.1:44448")
    )
    parser.add_argument("--prompt", default="Reply with exactly: GROK_SMOKE_OK")
    parser.add_argument("--approval", choices=("deny", "allow_once"))
    parser.add_argument("--cancel", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    if not is_truthy(os.getenv("RUN_LIVE_GROK_TESTS")) or not is_truthy(
        os.getenv("ACK_GROK_SUBSCRIPTION_USAGE")
    ):
        print(
            "Refusing to run: set RUN_LIVE_GROK_TESTS=true and "
            "ACK_GROK_SUBSCRIPTION_USAGE=true.",
            file=sys.stderr,
        )
        return 2

    base_url = args.base_url.rstrip("/")
    runtime = require_ok(
        requests.post(
            f"{base_url}/v1/agents/runtimes/grok/preflight",
            headers=headers(),
            timeout=30,
        )
    )
    catalog = require_ok(
        requests.get(f"{base_url}/v1/agents/models?runtime=grok", headers=headers(), timeout=30)
    )
    models = catalog.get("data")
    if not isinstance(models, list) or not models or not isinstance(models[0].get("id"), str):
        raise RuntimeError("Grok Agent catalog is empty or invalid")
    model_id = models[0]["id"]
    session = require_ok(
        requests.post(
            f"{base_url}/v1/agents/sessions",
            headers=headers(idempotency=True),
            json={"runtime": "grok", "model": model_id, "approval_policy": "strict"},
            timeout=30,
        )
    )
    session_id = str(session["id"])
    cursor = str(session["cursor"])
    released = False
    turn_id: str | None = None
    try:
        turn = require_ok(
            requests.post(
                f"{base_url}/v1/agents/sessions/{session_id}/turns",
                headers=headers(idempotency=True),
                json={"input": [{"type": "text", "text": args.prompt}]},
                timeout=30,
            )
        )
        turn_id = str(turn["id"])
        if args.cancel:
            require_ok(
                requests.post(
                    f"{base_url}/v1/agents/sessions/{session_id}/turns/{turn_id}/cancel",
                    headers=headers(idempotency=True),
                    timeout=30,
                )
            )
        terminal, cursor = wait_for_terminal(
            base_url,
            session_id,
            cursor,
            approval_decision=args.approval,
            timeout_seconds=args.timeout,
        )
        turn_id = None
        release = require_ok(
            requests.post(
                f"{base_url}/v1/agents/sessions/{session_id}/release",
                headers=headers(idempotency=True),
                timeout=30,
            )
        )
        released = release.get("status") == "released"
        print(
            json.dumps(
                {
                    "ok": terminal.get("type") in TERMINAL_EVENTS and released,
                    "runtime": runtime,
                    "model": model_id,
                    "session": session_id,
                    "terminal": terminal.get("type"),
                    "cursor": cursor,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if released else 1
    finally:
        if not released:
            if turn_id is not None:
                try:
                    requests.post(
                        f"{base_url}/v1/agents/sessions/{session_id}/turns/{turn_id}/cancel",
                        headers=headers(idempotency=True),
                        timeout=10,
                    )
                except requests.RequestException:
                    pass
                try:
                    wait_for_session_settled(base_url, session_id, 10)
                except (RuntimeError, requests.RequestException):
                    pass
            try:
                requests.post(
                    f"{base_url}/v1/agents/sessions/{session_id}/release",
                    headers=headers(idempotency=True),
                    timeout=10,
                )
            except requests.RequestException:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
