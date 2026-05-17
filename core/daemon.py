from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.model import MLXModelManager, get_model_manager

try:
    from shared.daemon_queue import QueueItem, ServiceProcessLock, SingleWorkerPriorityQueue
except ModuleNotFoundError:  # pragma: no cover - supports direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from shared.daemon_queue import QueueItem, ServiceProcessLock, SingleWorkerPriorityQueue


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _extract_text_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    chunks.append(str(item.get("text", "")))
                elif "text" in item:
                    chunks.append(str(item.get("text", "")))
            else:
                chunks.append(str(item))
        return "\n".join(chunk for chunk in chunks if chunk)
    return str(content)


def _summarize_tool_arguments(raw_arguments: Any, max_len: int = 220) -> str:
    try:
        parsed = raw_arguments
        if isinstance(parsed, str):
            parsed = json.loads(parsed)
        if isinstance(parsed, dict):
            keys = list(parsed.keys())
            # Avoid leaking large payload values (e.g., edit_file content) into prompt context.
            if not keys:
                summary = "{}"
            else:
                visible_pairs: list[str] = []
                for key in keys:
                    if key in {"content", "new_text", "text", "replacement"}:
                        visible_pairs.append(f"{key}=<omitted>")
                    elif key in {"edits", "operations", "changes"}:
                        value = parsed.get(key)
                        if isinstance(value, list):
                            visible_pairs.append(f"{key}=[{len(value)} items]")
                        else:
                            visible_pairs.append(f"{key}=<omitted>")
                    else:
                        value = parsed.get(key)
                        if isinstance(value, (str, int, float, bool)) or value is None:
                            visible_pairs.append(f"{key}={value!r}")
                        else:
                            visible_pairs.append(f"{key}=<complex>")
                summary = "{ " + ", ".join(visible_pairs) + " }"
        else:
            summary = str(raw_arguments)
    except Exception:
        summary = str(raw_arguments)

    if len(summary) > max_len:
        return summary[: max_len - 3] + "..."
    return summary


def _build_tool_instruction(tools: list[dict[str, Any]]) -> str:
    if not tools:
        return ""

    lines = [
        "You can call functions when needed.",
        "If a function is needed, output only one JSON object in this exact shape:",
        '{"name":"<function_name>","arguments":{"arg":"value"}}',
        "Do not include markdown or explanation when emitting a function call.",
        "Keep arguments compact. For large file changes, avoid embedding huge payloads in one call.",
        "Available functions:",
    ]
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict):
            continue
        name = str(function.get("name", "")).strip()
        if not name:
            continue
        description = str(function.get("description", "") or "").strip()
        parameters = function.get("parameters")
        parameter_keys: list[str] = []
        if isinstance(parameters, dict):
            properties = parameters.get("properties")
            if isinstance(properties, dict):
                parameter_keys = [str(k) for k in properties.keys()]
        args_text = ", ".join(parameter_keys) if parameter_keys else "(no arguments)"
        if description:
            lines.append(f"- {name}({args_text}): {description}")
        else:
            lines.append(f"- {name}({args_text})")

    return "\n".join(lines)


def _normalize_messages(
    messages: list[dict[str, object]],
    tools: list[dict[str, Any]],
) -> list[dict[str, str]]:
    prepared: list[dict[str, str]] = []
    has_system = False

    for message in messages:
        role = str(message.get("role", "user"))
        content = _extract_text_content(message.get("content", ""))
        tool_calls = message.get("tool_calls")

        if role == "tool":
            tool_call_id = str(message.get("tool_call_id") or "").strip()
            if tool_call_id:
                content = f"Tool result ({tool_call_id}):\n{content}"
            else:
                content = f"Tool result:\n{content}"
            role = "user"
        elif role not in {"system", "user", "assistant"}:
            role = "user"
        elif role == "assistant" and isinstance(tool_calls, list) and tool_calls:
            # Preserve function-call intent in model context for follow-up turns.
            call_lines: list[str] = []
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                name = str(function.get("name", "")).strip()
                arguments = str(function.get("arguments", "")).strip()
                if not name:
                    continue
                args_summary = _summarize_tool_arguments(arguments)
                call_lines.append(f"- {name}({args_summary})")
            if call_lines:
                call_summary = "Assistant requested tool calls:\n" + "\n".join(call_lines)
                content = f"{content}\n\n{call_summary}".strip()

        prepared.append({"role": role, "content": content})
        if role == "system":
            has_system = True

    tool_instruction = _build_tool_instruction(tools)
    if tool_instruction:
        if has_system and prepared:
            prepared[0]["content"] = f"{prepared[0]['content']}\n\n{tool_instruction}".strip()
        else:
            prepared.insert(0, {"role": "system", "content": tool_instruction})

    return prepared


@dataclass
class ChatPayload:
    messages: list[dict[str, object]]
    model: str
    max_tokens: int
    temperature: float
    tools: list[dict[str, Any]]
    preload_only: bool = False


class LocalLlmDaemon:
    """Single-process, single-generation local LLM daemon."""

    def __init__(
        self,
        manager: MLXModelManager | None = None,
    ) -> None:
        self._lock = ServiceProcessLock("llm-daemon")
        self._lock.acquire()
        try:
            self.manager = manager or get_model_manager()
            self._queue: SingleWorkerPriorityQueue[ChatPayload, dict[str, Any]] = (
                SingleWorkerPriorityQueue("local-llm", self._handle_chat)
            )
            self._started_at = time.time()
            self._preload_error: str | None = None
        except Exception:
            self._lock.release()
            raise

    def preload(self) -> None:
        try:
            preload_timeout = _env_int("LOCAL_LLM_DAEMON_PRELOAD_TIMEOUT_MS", 1_800_000) / 1000
            payload = ChatPayload(
                messages=[],
                model=self.manager.default_model_path,
                max_tokens=1,
                temperature=0.0,
                tools=[],
                preload_only=True,
            )
            self._queue.submit(payload, priority="high", timeout=preload_timeout)
            self._preload_error = None
        except Exception as exc:
            self._preload_error = str(exc)
            raise

    def chat(
        self,
        messages: list[dict[str, object]],
        model: str,
        max_tokens: int,
        temperature: float,
        tools: list[dict[str, Any]] | None = None,
        priority: str = "normal",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        payload = ChatPayload(
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools or [],
            preload_only=False,
        )
        effective_timeout = timeout
        if effective_timeout is None:
            effective_timeout = _env_int("LOCAL_LLM_DAEMON_REQUEST_TIMEOUT_MS", 900_000) / 1000
        return self._queue.submit(payload, priority=priority, timeout=effective_timeout)

    def health(self) -> dict[str, Any]:
        model_health = self.manager.health()
        return {
            "status": "ok" if model_health["loaded"] and self._preload_error is None else "loading",
            "ready": bool(model_health["loaded"] and self._preload_error is None),
            "startedAt": int(self._started_at),
            "preloadError": self._preload_error,
            **model_health,
            **self._queue.health(),
        }

    def shutdown(self) -> None:
        self._queue.shutdown()
        self._lock.release()

    def _handle_chat(self, item: QueueItem[ChatPayload, dict[str, Any]]) -> dict[str, Any]:
        payload = item.payload
        if isinstance(payload, dict):
            messages = payload["messages"]
            model = str(payload["model"])
            max_tokens = int(payload["max_tokens"])
            temperature = float(payload["temperature"])
            tools = payload.get("tools", [])
            preload_only = bool(payload.get("preload_only", False))
        else:
            messages = payload.messages
            model = payload.model
            max_tokens = payload.max_tokens
            temperature = payload.temperature
            tools = payload.tools
            preload_only = payload.preload_only

        if preload_only:
            start = time.perf_counter()
            try:
                self.manager.ensure_loaded(model)
                self._preload_error = None
            except Exception as exc:
                self._preload_error = str(exc)
                raise
            finished = time.perf_counter()
            return {
                "content": "",
                "usage": self.manager.last_generation_stats(),
                "preload": True,
                "queueWaitMs": round((start - item.queued_at) * 1000, 3),
                "generateMs": round((finished - start) * 1000, 3),
            }

        prepared_messages = _normalize_messages(messages, tools if isinstance(tools, list) else [])
        default_cap = _env_int("LOCAL_LLM_MAX_OUTPUT_TOKENS", 512)
        tool_call_cap = _env_int("LOCAL_LLM_MAX_TOOL_CALL_TOKENS", max(default_cap, 1024))
        token_cap = tool_call_cap if tools else default_cap
        max_tokens = max(1, min(max_tokens, token_cap))

        start = time.perf_counter()
        try:
            prompt_tokens = self.manager.count_prompt_tokens(prepared_messages, model=model)
            max_prompt_tokens = _env_int("LOCAL_LLM_MAX_PROMPT_TOKENS", 32768)
            if prompt_tokens > max_prompt_tokens:
                raise ValueError(
                    f"prompt_too_large: prompt_tokens={prompt_tokens} "
                    f"max_prompt_tokens={max_prompt_tokens}"
                )

            content = "".join(
                self.manager.generate_stream(
                    prepared_messages,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            )
            self._preload_error = None
        except Exception as exc:
            self._preload_error = str(exc)
            raise
        finished = time.perf_counter()
        return {
            "content": content,
            "usage": self.manager.last_generation_stats(),
            "queueWaitMs": round((start - item.queued_at) * 1000, 3),
            "generateMs": round((finished - start) * 1000, 3),
        }


_LOCAL_LLM_DAEMON: LocalLlmDaemon | None = None


def get_local_llm_daemon() -> LocalLlmDaemon:
    global _LOCAL_LLM_DAEMON
    if _LOCAL_LLM_DAEMON is None:
        _LOCAL_LLM_DAEMON = LocalLlmDaemon()
    return _LOCAL_LLM_DAEMON
