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


def headers(*, idempotency: bool = False) -> dict[str, str]:
    value = {"Accept": "application/json"}
    token = os.getenv("LOCAL_LLM_ACCESS_TOKEN", "").strip()
    if token:
        value["Authorization"] = f"Bearer {token}"
    if idempotency:
        value["Idempotency-Key"] = f"smoke-{uuid.uuid4()}"
    return value


def require_ok(response: requests.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{response.request.method} {response.url}: non-JSON response") from exc
    if not response.ok:
        raise RuntimeError(
            f"{response.request.method} {response.url}: HTTP {response.status_code}: {json.dumps(body, ensure_ascii=False)}"
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
                body = json.loads("\n".join(data_lines))
                if isinstance(body, dict):
                    body.setdefault("type", event_type)
                    yield body
            event_type, data_lines = "message", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.lstrip()
        if field == "event":
            event_type = value
        elif field == "data":
            data_lines.append(value)


def wait_for_terminal(
    base_url: str,
    session_id: str,
    cursor: str,
    *,
    approval_decision: str | None = None,
    timeout_seconds: int = 300,
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
            event_type = event.get("type")
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if event_type == "approval.requested" and approval_decision:
                approval_id = data.get("approval_id")
                if not isinstance(approval_id, str):
                    raise RuntimeError("approval.requested did not include approval_id")
                require_ok(
                    requests.post(
                        f"{base_url}/v1/agents/sessions/{session_id}/approvals/{approval_id}/decision",
                        headers=headers(idempotency=True),
                        json={"decision": approval_decision},
                        timeout=30,
                    )
                )
            if event_type in TERMINAL_EVENTS:
                return event, cursor
            if time.monotonic() >= deadline:
                raise TimeoutError("Muse smoke turn did not reach a terminal event")
    raise RuntimeError("Muse event stream ended before a terminal event")


def start_turn(base_url: str, session_id: str, prompt: str) -> dict[str, Any]:
    return require_ok(
        requests.post(
            f"{base_url}/v1/agents/sessions/{session_id}/turns",
            headers=headers(idempotency=True),
            json={"input": [{"type": "text", "text": prompt}]},
            timeout=30,
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Opt-in Muse subscription Agent API smoke test.")
    parser.add_argument("--base-url", default=os.getenv("LOCAL_LLM_API_BASE", "http://127.0.0.1:44448"))
    parser.add_argument(
        "--full",
        action="store_true",
        help="Also exercise approval deny, approval allow-once, and cancel in the isolated workspace.",
    )
    args = parser.parse_args()
    if not is_truthy(os.getenv("RUN_LIVE_MUSE_TESTS")) or not is_truthy(
        os.getenv("ACK_MUSE_SUBSCRIPTION_USAGE")
    ):
        print(
            "Refusing to run: set RUN_LIVE_MUSE_TESTS=true and ACK_MUSE_SUBSCRIPTION_USAGE=true.",
            file=sys.stderr,
        )
        return 2

    base_url = args.base_url.rstrip("/")
    runtime = require_ok(
        requests.post(
            f"{base_url}/v1/agents/runtimes/muse/preflight",
            headers=headers(),
            timeout=30,
        )
    )
    models = require_ok(requests.get(f"{base_url}/v1/agents/models?runtime=muse", headers=headers(), timeout=30))
    rows = models.get("data")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Muse Agent catalog is empty")
    model_id = rows[0].get("id")
    if not isinstance(model_id, str):
        raise RuntimeError("Muse Agent catalog returned an invalid model id")
    session = require_ok(
        requests.post(
            f"{base_url}/v1/agents/sessions",
            headers=headers(idempotency=True),
            json={
                "runtime": "muse",
                "model": model_id,
                "approval_policy": "strict",
                "workspace": {"mode": "isolated"},
            },
            timeout=30,
        )
    )
    session_id = str(session["id"])
    cursor = str(session["cursor"])
    active_turn_id: str | None = None
    released = False
    try:
        active_turn_id = str(
            start_turn(base_url, session_id, "Reply with exactly: MUSE_SMOKE_OK")["id"]
        )
        terminal, cursor = wait_for_terminal(base_url, session_id, cursor)
        active_turn_id = None
        if terminal["type"] != "turn.completed":
            raise RuntimeError(f"basic turn ended as {terminal['type']}")

        if args.full:
            active_turn_id = str(
                start_turn(
                    base_url,
                    session_id,
                    "Create a file named smoke-denied.txt containing denied in the current workspace.",
                )["id"]
            )
            denied, cursor = wait_for_terminal(
                base_url,
                session_id,
                cursor,
                approval_decision="deny",
            )
            active_turn_id = None
            if denied["type"] not in TERMINAL_EVENTS:
                raise RuntimeError("approval deny turn did not settle")

            active_turn_id = str(
                start_turn(
                    base_url,
                    session_id,
                    "Create a file named smoke-allowed.txt containing allowed in the current workspace.",
                )["id"]
            )
            allowed, cursor = wait_for_terminal(
                base_url,
                session_id,
                cursor,
                approval_decision="allow_once",
            )
            active_turn_id = None
            if allowed["type"] != "turn.completed":
                raise RuntimeError(f"approval allow-once turn ended as {allowed['type']}")

            pending = start_turn(base_url, session_id, "Perform a long analysis until cancelled.")
            turn_id = str(pending["id"])
            active_turn_id = turn_id
            require_ok(
                requests.post(
                    f"{base_url}/v1/agents/sessions/{session_id}/turns/{turn_id}/cancel",
                    headers=headers(idempotency=True),
                    timeout=30,
                )
            )
            cancelled, cursor = wait_for_terminal(base_url, session_id, cursor)
            active_turn_id = None
            if cancelled["type"] != "turn.cancelled":
                raise RuntimeError(f"cancel turn ended as {cancelled['type']}")

        release_response = require_ok(
            requests.post(
                f"{base_url}/v1/agents/sessions/{session_id}/release",
                headers=headers(idempotency=True),
                timeout=30,
            )
        )
        if release_response.get("status") != "released":
            raise RuntimeError("session release did not settle")
        released = True
        resumed = require_ok(
            requests.post(
                f"{base_url}/v1/agents/sessions/{session_id}/resume",
                headers=headers(idempotency=True),
                timeout=30,
            )
        )
        released = False
        cursor = str(resumed["cursor"])
        require_ok(
            requests.post(
                f"{base_url}/v1/agents/sessions/{session_id}/release",
                headers=headers(idempotency=True),
                timeout=30,
            )
        )
        released = True
    finally:
        if not released:
            if active_turn_id is not None:
                try:
                    requests.post(
                        f"{base_url}/v1/agents/sessions/{session_id}/turns/{active_turn_id}/cancel",
                        headers=headers(idempotency=True),
                        timeout=10,
                    )
                except requests.RequestException:
                    pass
            try:
                cleanup = requests.post(
                    f"{base_url}/v1/agents/sessions/{session_id}/release",
                    headers=headers(idempotency=True),
                    timeout=10,
                )
                if not cleanup.ok:
                    print(
                        f"Warning: Muse smoke session cleanup returned HTTP {cleanup.status_code}.",
                        file=sys.stderr,
                    )
            except requests.RequestException as exc:
                print(f"Warning: Muse smoke session cleanup failed: {exc}", file=sys.stderr)
    print(
        json.dumps(
            {
                "ok": True,
                "runtime": runtime,
                "model": model_id,
                "session": session_id,
                "lastCursor": cursor,
                "full": args.full,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
