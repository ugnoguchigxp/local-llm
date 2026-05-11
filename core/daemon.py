from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.chat_engine import ChatEngine
from core.model import MLXModelManager, get_model_manager

try:
    from shared.daemon_queue import QueueItem, SingleWorkerPriorityQueue
except ModuleNotFoundError:  # pragma: no cover - supports direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from shared.daemon_queue import QueueItem, SingleWorkerPriorityQueue


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass
class ChatPayload:
    messages: list[dict[str, object]]
    model: str
    max_tokens: int
    temperature: float
    tool_names: list[str]


class LocalLlmDaemon:
    """Single-process, single-generation local LLM daemon."""

    def __init__(
        self,
        manager: MLXModelManager | None = None,
        chat_engine: ChatEngine | None = None,
    ) -> None:
        self.manager = manager or get_model_manager()
        self.chat_engine = chat_engine or ChatEngine(self.manager)
        self._queue: SingleWorkerPriorityQueue[ChatPayload, dict[str, Any]] = (
            SingleWorkerPriorityQueue("local-llm", self._handle_chat)
        )
        self._started_at = time.time()
        self._preload_error: str | None = None

    def preload(self) -> None:
        try:
            self.manager.ensure_loaded()
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
        tool_names: list[str],
        priority: str = "normal",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        payload = ChatPayload(
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            tool_names=tool_names,
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

    def _handle_chat(self, item: QueueItem[ChatPayload, dict[str, Any]]) -> dict[str, Any]:
        payload = item.payload
        start = time.perf_counter()
        try:
            content = self.chat_engine.run_chat(
                payload.messages,
                model=payload.model,
                max_tokens=payload.max_tokens,
                temperature=payload.temperature,
                tools=payload.tool_names,
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
