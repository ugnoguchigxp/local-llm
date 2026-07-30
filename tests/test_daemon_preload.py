from __future__ import annotations

import pytest

from core.context_budget import ContextBudgetExceeded
from core.daemon import ChatPayload, DaemonBusyError, LocalLlmDaemon
from shared.daemon_queue import QueueBusyError, QueueItem


class _FakeManager:
    def __init__(
        self,
        *,
        context_window: int = 176000,
        prompt_token_counts: list[int] | None = None,
    ) -> None:
        self.default_model_path = "fake-model"
        self.loaded_models: list[str] = []
        self._context_window = context_window
        self._prompt_token_counts = list(prompt_token_counts or [10])
        self.generated_messages: list[dict[str, str]] | None = None
        self.generated_max_tokens: int | None = None

    def ensure_loaded(self, model: str | None = None) -> None:
        self.loaded_models.append(model or self.default_model_path)

    def count_prompt_tokens(self, messages, model: str | None = None) -> int:
        if len(self._prompt_token_counts) > 1:
            return self._prompt_token_counts.pop(0)
        return self._prompt_token_counts[0]

    def context_window(self) -> int:
        return self._context_window

    def generate_stream(
        self,
        messages,
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        top_p: float | None = None,
        stop: list[str] | None = None,
    ):
        self.generated_messages = messages
        self.generated_max_tokens = max_tokens
        yield "ok"

    def last_generation_stats(self):
        return {
            "prompt_tokens": 10,
            "completion_tokens": 1,
            "total_tokens": 11,
            "finish_reason": "stop",
        }


class _QueueStub:
    def __init__(self) -> None:
        self.calls: list[tuple[ChatPayload, str, float | None]] = []

    def submit(
        self,
        payload: ChatPayload,
        priority: str = "normal",
        timeout: float | None = None,
        reject_when_busy: bool = False,
    ):
        self.calls.append((payload, priority, timeout))
        return {"ok": True}


class _BusyQueueStub:
    def __init__(self) -> None:
        self.reject_when_busy: bool | None = None

    def submit(
        self,
        payload: ChatPayload,
        priority: str = "normal",
        timeout: float | None = None,
        reject_when_busy: bool = False,
    ):
        self.reject_when_busy = reject_when_busy
        raise QueueBusyError("local-llm daemon is busy")


def test_preload_enqueues_worker_preload_task():
    daemon = LocalLlmDaemon.__new__(LocalLlmDaemon)
    daemon.manager = _FakeManager()
    daemon._queue = _QueueStub()
    daemon._preload_error = "old error"

    daemon.preload()

    assert daemon._preload_error is None
    assert len(daemon._queue.calls) == 1
    payload, priority, timeout = daemon._queue.calls[0]
    assert isinstance(payload, ChatPayload)
    assert payload.preload_only is True
    assert payload.model == "fake-model"
    assert priority == "high"
    assert timeout is not None


def test_handle_chat_preload_only_loads_model_without_generation():
    daemon = LocalLlmDaemon.__new__(LocalLlmDaemon)
    daemon.manager = _FakeManager()
    daemon._preload_error = "old error"

    payload = ChatPayload(
        messages=[],
        model="fake-model",
        max_tokens=1,
        temperature=0.0,
        tools=[],
        preload_only=True,
    )
    item = QueueItem(payload=payload)

    result = daemon._handle_chat(item)

    assert daemon._preload_error is None
    assert daemon.manager.loaded_models == ["fake-model"]
    assert result["preload"] is True
    assert result["content"] == ""


def test_chat_rejects_instead_of_queueing_when_daemon_is_busy():
    daemon = LocalLlmDaemon.__new__(LocalLlmDaemon)
    daemon._queue = _BusyQueueStub()

    with pytest.raises(DaemonBusyError):
        daemon.chat(
            [{"role": "user", "content": "hi"}],
            "fake-model",
            16,
            0.0,
        )

    assert daemon._queue.reject_when_busy is True


def test_handle_chat_allows_10k_prompt_under_176k_context(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_MAX_OUTPUT_TOKENS", "20000")
    monkeypatch.setenv("LOCAL_LLM_MAX_PROMPT_TOKENS", "160000")
    daemon = LocalLlmDaemon.__new__(LocalLlmDaemon)
    daemon.manager = _FakeManager(context_window=176000, prompt_token_counts=[10000])
    daemon._preload_error = None

    payload = ChatPayload(
        messages=[{"role": "user", "content": "X" * 40000}],
        model="fake-model",
        max_tokens=16000,
        temperature=0.0,
        tools=[],
    )

    result = daemon._handle_chat(QueueItem(payload=payload))

    assert result["content"] == "ok"
    assert daemon.manager.generated_max_tokens == 16000
    assert result["contextBudget"] == {
        "contextWindowTokens": 176000,
        "safePromptBudgetTokens": 160000,
        "reservedOutputTokens": 16000,
        "estimatedPromptTokensBefore": 10000,
        "estimatedPromptTokensAfter": 10000,
        "compressionApplied": False,
        "compressionPolicy": "gemma4-last-mile-v1",
        "compressedSections": [],
        "droppedFields": [],
        "budgetExceeded": False,
    }


def test_handle_chat_compresses_long_tool_output_before_generation(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_MAX_OUTPUT_TOKENS", "20000")
    monkeypatch.setenv("LOCAL_LLM_MAX_PROMPT_TOKENS", "160000")
    daemon = LocalLlmDaemon.__new__(LocalLlmDaemon)
    daemon.manager = _FakeManager(context_window=176000, prompt_token_counts=[181000, 154000])
    daemon._preload_error = None
    tool_output = "\n".join(["same evidence line"] * 20000)

    payload = ChatPayload(
        messages=[
            {"role": "user", "content": "current request"},
            {"role": "tool", "tool_call_id": "call_1", "content": tool_output},
        ],
        model="fake-model",
        max_tokens=16000,
        temperature=0.0,
        tools=[],
    )

    result = daemon._handle_chat(QueueItem(payload=payload))

    assert result["contextBudget"]["compressionApplied"] is True
    assert result["contextBudget"]["compressedSections"] == ["tool_outputs"]
    assert result["contextBudget"]["estimatedPromptTokensAfter"] == 154000
    assert result["contextBudget"]["budgetExceeded"] is False
    generated_content = "\n".join(message["content"] for message in daemon.manager.generated_messages)
    assert "[compressed:" in generated_content
    assert "current request" in generated_content


def test_handle_chat_raises_structured_context_budget_error_when_still_over_budget(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_MAX_OUTPUT_TOKENS", "20000")
    monkeypatch.setenv("LOCAL_LLM_MAX_PROMPT_TOKENS", "160000")
    daemon = LocalLlmDaemon.__new__(LocalLlmDaemon)
    daemon.manager = _FakeManager(context_window=176000, prompt_token_counts=[181000])
    daemon._preload_error = None

    payload = ChatPayload(
        messages=[{"role": "user", "content": "current request"}],
        model="fake-model",
        max_tokens=16000,
        temperature=0.0,
        tools=[],
    )

    with pytest.raises(ContextBudgetExceeded) as raised:
        daemon._handle_chat(QueueItem(payload=payload))

    metadata = raised.value.metadata
    assert metadata["contextWindowTokens"] == 176000
    assert metadata["safePromptBudgetTokens"] == 160000
    assert metadata["reservedOutputTokens"] == 16000
    assert metadata["estimatedPromptTokensBefore"] == 181000
    assert metadata["budgetExceeded"] is True


def test_handle_chat_caps_prompt_budget_from_env(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_MAX_OUTPUT_TOKENS", "20000")
    monkeypatch.setenv("LOCAL_LLM_MAX_PROMPT_TOKENS", "128000")
    daemon = LocalLlmDaemon.__new__(LocalLlmDaemon)
    daemon.manager = _FakeManager(context_window=172000, prompt_token_counts=[10000])
    daemon._preload_error = None

    payload = ChatPayload(
        messages=[{"role": "user", "content": "hello"}],
        model="fake-model",
        max_tokens=16000,
        temperature=0.0,
        tools=[],
    )

    result = daemon._handle_chat(QueueItem(payload=payload))

    assert result["contextBudget"]["contextWindowTokens"] == 172000
    assert result["contextBudget"]["safePromptBudgetTokens"] == 128000
    assert result["contextBudget"]["reservedOutputTokens"] == 16000
