from __future__ import annotations

from core.daemon import ChatPayload, LocalLlmDaemon
from shared.daemon_queue import QueueItem


class _FakeManager:
    def __init__(self) -> None:
        self.default_model_path = "fake-model"
        self.loaded_models: list[str] = []

    def ensure_loaded(self, model: str | None = None) -> None:
        self.loaded_models.append(model or self.default_model_path)

    def last_generation_stats(self):
        return None


class _QueueStub:
    def __init__(self) -> None:
        self.calls: list[tuple[ChatPayload, str, float | None]] = []

    def submit(self, payload: ChatPayload, priority: str = "normal", timeout: float | None = None):
        self.calls.append((payload, priority, timeout))
        return {"ok": True}


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
