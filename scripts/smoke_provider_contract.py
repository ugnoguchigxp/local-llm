#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any


DEFAULT_CHECKS = [
    "models",
    "invalid-tool-choice",
    "system-prompt",
    "json-only",
    "tool-call",
    "tool-result",
    "stream",
    "tool-stream",
    "length",
]


def _request(method: str, url: str, payload: dict[str, Any] | None, timeout: float):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return response.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _json_request(method: str, url: str, payload: dict[str, Any] | None, timeout: float):
    status, body = _request(method, url, payload, timeout)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = {"raw": body}
    return status, parsed


def _chat_payload(model: str, content: str, **extra):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.0,
        "max_tokens": 64,
    }
    payload.update(extra)
    return payload


def check_models(base_url: str, model: str, timeout: float) -> tuple[bool, str]:
    status, body = _json_request("GET", f"{base_url}/models", None, timeout)
    models = [item.get("id") for item in body.get("data", [])] if isinstance(body, dict) else []
    return status == 200 and model in models, f"status={status} models={models}"


def check_invalid_tool_choice(base_url: str, model: str, timeout: float) -> tuple[bool, str]:
    status, body = _json_request(
        "POST",
        f"{base_url}/chat/completions",
        _chat_payload(
            model,
            "test",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "search_web",
                        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                    },
                }
            ],
            tool_choice={"type": "function", "function": {"name": "fetch_content"}},
        ),
        timeout,
    )
    return status == 400 and "tool_choice function" in str(body), f"status={status} body={body}"


def check_system_prompt(base_url: str, model: str, timeout: float) -> tuple[bool, str]:
    status, body = _json_request(
        "POST",
        f"{base_url}/chat/completions",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "Reply with exactly OK."},
                {"role": "user", "content": "Ping"},
            ],
            "temperature": 0.0,
            "max_tokens": 8,
        },
        timeout,
    )
    content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    return status == 200 and "OK" in content, f"status={status} content={content!r}"


def check_json_only(base_url: str, model: str, timeout: float) -> tuple[bool, str]:
    status, body = _json_request(
        "POST",
        f"{base_url}/chat/completions",
        _chat_payload(model, 'Return only this JSON: {"ok":true}', max_tokens=32),
        timeout,
    )
    content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return False, f"status={status} content={content!r}"
    return status == 200 and parsed.get("ok") is True, f"status={status} content={content!r}"


def _tool_def():
    return [
        {
            "type": "function",
            "function": {
                "name": "search_web",
                "description": "search",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ]


def check_tool_call(base_url: str, model: str, timeout: float) -> tuple[bool, str]:
    status, body = _json_request(
        "POST",
        f"{base_url}/chat/completions",
        _chat_payload(
            model,
            "Search for latest Rust release. Use the function.",
            tools=_tool_def(),
            tool_choice="required",
            max_tokens=256,
        ),
        timeout,
    )
    choice = body.get("choices", [{}])[0]
    calls = choice.get("message", {}).get("tool_calls")
    ok = status == 200 and choice.get("finish_reason") == "tool_calls" and bool(calls)
    return ok, f"status={status} finish={choice.get('finish_reason')} calls={calls}"


def check_tool_result(base_url: str, model: str, timeout: float) -> tuple[bool, str]:
    status, body = _json_request(
        "POST",
        f"{base_url}/chat/completions",
        {
            "model": model,
            "messages": [
                {"role": "user", "content": "Search for latest Rust release."},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_test",
                            "type": "function",
                            "function": {"name": "search_web", "arguments": '{"query":"Rust release"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_test", "content": "Rust 1.99.0"},
            ],
            "temperature": 0.0,
            "max_tokens": 48,
        },
        timeout,
    )
    content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    return status == 200 and "1.99" in content, f"status={status} content={content!r}"


def check_stream(base_url: str, model: str, timeout: float) -> tuple[bool, str]:
    status, body = _request(
        "POST",
        f"{base_url}/chat/completions",
        _chat_payload(model, "Say OK.", stream=True, max_tokens=8),
        timeout,
    )
    return status == 200 and "data: [DONE]" in body, f"status={status} prefix={body[:160]!r}"


def check_tool_stream(base_url: str, model: str, timeout: float) -> tuple[bool, str]:
    status, body = _request(
        "POST",
        f"{base_url}/chat/completions",
        _chat_payload(
            model,
            "Search for latest Rust release. Use the function.",
            stream=True,
            tools=_tool_def(),
            tool_choice="required",
            max_tokens=256,
        ),
        timeout,
    )
    ok = status == 200 and '"tool_calls"' in body and '"finish_reason": "tool_calls"' in body
    return ok, f"status={status} prefix={body[:220]!r}"


def check_length(base_url: str, model: str, timeout: float) -> tuple[bool, str]:
    status, body = _json_request(
        "POST",
        f"{base_url}/chat/completions",
        _chat_payload(model, "Write a long paragraph.", max_tokens=1),
        timeout,
    )
    finish = body.get("choices", [{}])[0].get("finish_reason")
    return status == 200 and finish in {"length", "stop"}, f"status={status} finish={finish}"


CHECKS = {
    "models": check_models,
    "invalid-tool-choice": check_invalid_tool_choice,
    "system-prompt": check_system_prompt,
    "json-only": check_json_only,
    "tool-call": check_tool_call,
    "tool-result": check_tool_result,
    "stream": check_stream,
    "tool-stream": check_tool_stream,
    "length": check_length,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:44448/v1")
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--checks",
        default=",".join(DEFAULT_CHECKS),
        help=f"Comma-separated checks. Available: {', '.join(CHECKS)}",
    )
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    selected = [check.strip() for check in args.checks.split(",") if check.strip()]
    results = []
    ok = True
    for check in selected:
        if check not in CHECKS:
            results.append({"check": check, "ok": False, "detail": "unknown check"})
            ok = False
            continue
        try:
            passed, detail = CHECKS[check](base_url, args.model, args.timeout)
        except Exception as exc:  # pragma: no cover - smoke diagnostics
            passed, detail = False, f"{type(exc).__name__}: {exc}"
        results.append({"check": check, "ok": passed, "detail": detail})
        ok = ok and passed

    print(json.dumps({"ok": ok, "results": results}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
