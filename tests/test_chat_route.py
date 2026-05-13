from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.chat import router as chat_router


class _FakeManager:
    model_id = "gemma-4-e4b-it"

    def validate_model(self, requested_model: str | None) -> str:
        return requested_model or self.model_id


class _FakeDaemon:
    def __init__(self, content: str):
        self.manager = _FakeManager()
        self._content = content

    def chat(self, messages, model, max_tokens, temperature, tools=None, priority="normal", timeout=None):
        return {
            "content": self._content,
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }


def _build_client(fake_daemon: _FakeDaemon) -> TestClient:
    from api.routes import chat as chat_module

    app = FastAPI()
    chat_module.get_local_llm_daemon = lambda: fake_daemon
    app.include_router(chat_router)
    return TestClient(app)


def test_chat_completion_returns_tool_calls():
    client = _build_client(
        _FakeDaemon('{"name":"search_web","arguments":{"query":"latest rust release"}}')
    )

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemma-4-e4b-it",
            "messages": [{"role": "user", "content": "最新のRustのバージョンを調べて"}],
            "tools": [
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
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    tool_calls = body["choices"][0]["message"]["tool_calls"]
    assert isinstance(tool_calls, list) and len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "search_web"


def test_chat_completion_sanitizes_plain_response():
    client = _build_client(_FakeDaemon("<think>hidden</think>最終回答です"))

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemma-4-e4b-it",
            "messages": [{"role": "user", "content": "回答してください"}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["choices"][0]["message"]["content"] == "最終回答です"


def test_chat_completion_rejects_invalid_tool_choice():
    client = _build_client(_FakeDaemon("ignored"))

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemma-4-e4b-it",
            "messages": [{"role": "user", "content": "回答してください"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "search_web",
                        "description": "search",
                        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "fetch_content"}},
        },
    )

    assert response.status_code == 400
    assert "tool_choice function" in str(response.json().get("detail", ""))
