#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from core.tool_calling import normalize_tool_name
from tools import fetch_content, search_web

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

SESSION_ID_RE = r"^[A-Za-z0-9_-]{6,64}$"

DEFAULT_SYSTEM_PROMPT = "You are a pragmatic coding assistant. Reply in Japanese unless the user asks otherwise."

DEFAULT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web and return short ranked results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_content",
            "description": "Fetch and extract the main text content of a URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Target URL"},
                },
                "required": ["url"],
            },
        },
    },
]


class FileSessionStore:
    def __init__(self, session_dir: str | None = None):
        default_dir = Path.home() / ".localLlm" / "sessions"
        self.session_dir = Path(session_dir) if session_dir else default_dir
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        return self.session_dir / f"{session_id}.json"

    def load(self, session_id: str) -> dict | None:
        path = self._session_path(session_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, session_id: str, messages: list[dict], model: str, api_base: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        path = self._session_path(session_id)
        existing = None
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                existing = json.load(f)

        payload = {
            "session_id": session_id,
            "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now,
            "model": model,
            "api_base": api_base,
            "messages": messages,
        }
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def _generate_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:12]}"


def _validate_session_id(session_id: str) -> bool:
    return re.match(SESSION_ID_RE, session_id) is not None


def _truthy_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _default_api_base() -> str:
    host = os.getenv("GEMMA4_API_HOST", "127.0.0.1")
    port = os.getenv("GEMMA4_API_PORT", "44448")
    if host == "0.0.0.0":
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def _default_model() -> str:
    return os.getenv("GEMMA4_API_MODEL_ID", "gemma-4-e4b-it")


class LocalLlmApiClient:
    def __init__(self, base_url: str, timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if _truthy_env("LOCAL_LLM_REQUIRE_AUTH", default=False):
            token = os.getenv("LOCAL_LLM_ACCESS_TOKEN", "").strip()
            if not token:
                raise RuntimeError("LOCAL_LLM_REQUIRE_AUTH=true but LOCAL_LLM_ACCESS_TOKEN is empty")
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
            "tools": tools,
            "tool_choice": "auto" if tools else "none",
        }
        try:
            response = requests.post(
                f"{self.base_url}/v1/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Chat completion request failed: {exc}") from exc
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            detail = ""
            try:
                detail = response.text
            except Exception:
                detail = str(exc)
            raise RuntimeError(f"Chat completion request failed: {detail}") from exc
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError("Chat completion request failed: invalid JSON response") from exc


def _parse_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def _execute_local_tool(tool_name: str, arguments: dict[str, Any]) -> str:
    name = normalize_tool_name(tool_name)
    if name == "search_web":
        query = str(arguments.get("query") or arguments.get("q") or "").strip()
        if not query:
            return "Error: query parameter is required"
        return search_web(query)

    if name == "fetch_content":
        url = str(arguments.get("url") or "").strip()
        if not url:
            return "Error: url parameter is required"
        return fetch_content(url)

    return f"Error: unsupported local tool '{tool_name}'"


def _run_turn_via_api(
    client: LocalLlmApiClient,
    *,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    enable_local_tools: bool,
    max_tool_rounds: int,
    verbose: bool,
) -> tuple[str, dict[str, Any]]:
    tools = DEFAULT_TOOLS if enable_local_tools else None

    for _ in range(max_tool_rounds + 1):
        response = client.chat_completion(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
        )

        choices = response.get("choices") or []
        if not choices:
            return "", response
        choice = choices[0]
        finish_reason = str(choice.get("finish_reason") or "stop")
        message = choice.get("message") or {}
        content = str(message.get("content") or "")
        tool_calls = message.get("tool_calls") or []

        if finish_reason == "tool_calls" and enable_local_tools and isinstance(tool_calls, list) and tool_calls:
            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            })

            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    continue
                tool_name = str(function.get("name") or "")
                arguments = _parse_arguments(function.get("arguments"))
                tool_result = _execute_local_tool(tool_name, arguments)
                if verbose:
                    print(f"[Tool] {tool_name}({arguments})", file=sys.stderr)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(tool_call.get("id") or ""),
                        "content": tool_result,
                    }
                )
            continue
        if finish_reason == "tool_calls" and not tool_calls:
            return "Error: tool_calls finish reason was returned without tool_calls payload.", response

        return content, response

    return "上限に達しました。", {"choices": [{"finish_reason": "length", "message": {"content": "上限に達しました。"}}]}


def main() -> int:
    if load_dotenv is not None:
        load_dotenv()

    parser = argparse.ArgumentParser(description="local-llm API client CLI")
    parser.add_argument("prompt", nargs="?", help="Single-turn prompt")
    parser.add_argument("--prompt", dest="prompt_opt", type=str, help="Single-turn prompt")
    parser.add_argument("--api-base", default=_default_api_base(), help="OpenAI-compatible API base URL")
    parser.add_argument("--model", default=_default_model(), help="Model ID exposed by /v1/models")
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--temp", type=float, default=0.0)
    parser.add_argument("--session-id", type=str, help="Session ID to resume")
    parser.add_argument("--session-dir", type=str, help="Directory to store sessions")
    parser.add_argument("--no-session", action="store_true", help="Disable session persistence")
    parser.add_argument("--output", choices=["json", "text"], default="text", help="Output format in single-turn mode")
    parser.add_argument("--tools", action=argparse.BooleanOptionalAction, default=True, help="Enable local tools (search_web/fetch_content)")
    parser.add_argument("--max-tool-rounds", type=int, default=4, help="Max local tool rounds")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logs")

    # Backward-compatible no-op flags from previous direct-backend CLI.
    parser.add_argument("--backend", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--no-mcp", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--root", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--mtp", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-mtp", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--draft-model", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--draft-kind", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--draft-block-size", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--prefill-step-size", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--thinking", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args()
    prompt = args.prompt_opt if args.prompt_opt is not None else args.prompt

    store = FileSessionStore(args.session_dir)
    session_id = args.session_id
    messages: list[dict[str, Any]] = []

    if not args.no_session:
        if session_id:
            if not _validate_session_id(session_id):
                print("Invalid --session-id format", file=sys.stderr)
                return 2
            loaded = store.load(session_id)
            if loaded:
                messages = list(loaded.get("messages", []))
        else:
            session_id = _generate_session_id()

    if not messages:
        messages = [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}]

    client = LocalLlmApiClient(args.api_base)

    if prompt is not None:
        messages.append({"role": "user", "content": prompt})
        answer, raw_response = _run_turn_via_api(
            client,
            model=args.model,
            messages=messages,
            max_tokens=args.max_tokens,
            temperature=args.temp,
            enable_local_tools=args.tools,
            max_tool_rounds=args.max_tool_rounds,
            verbose=args.verbose,
        )
        messages.append({"role": "assistant", "content": answer})

        if not args.no_session and session_id:
            store.save(session_id, messages, args.model, args.api_base)

        if args.output == "json":
            print(
                json.dumps(
                    {
                        "session_id": session_id,
                        "model": args.model,
                        "api_base": args.api_base,
                        "response": answer,
                        "raw": raw_response,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(answer)
        return 0

    print(f"Connected API: {args.api_base}")
    print(f"Model: {args.model}")
    if session_id and not args.no_session:
        print(f"Session: {session_id}")
    print("Type 'exit' or 'quit' to finish.\n")

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            break

        messages.append({"role": "user", "content": user_input})
        answer, _raw = _run_turn_via_api(
            client,
            model=args.model,
            messages=messages,
            max_tokens=args.max_tokens,
            temperature=args.temp,
            enable_local_tools=args.tools,
            max_tool_rounds=args.max_tool_rounds,
            verbose=args.verbose,
        )
        messages.append({"role": "assistant", "content": answer})
        print(answer)

        if not args.no_session and session_id:
            store.save(session_id, messages, args.model, args.api_base)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
