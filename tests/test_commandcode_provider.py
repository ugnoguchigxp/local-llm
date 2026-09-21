from __future__ import annotations

import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from api.auth import require_api_auth
from api.main import app
from core.commandcode_provider import (
    COMMANDCODE_MODEL_ID,
    COMMANDCODE_UPSTREAM_MODEL_ID,
    CommandCodeConfig,
    CommandCodeProvider,
)


def _config() -> CommandCodeConfig:
    return CommandCodeConfig(
        enabled=True,
        base_url="https://api.commandcode.test/provider/v1",
        timeout_seconds=30,
        api_key="test-secret",
        credential_source="test",
    )


def test_config_reuses_commandcode_auth_file(monkeypatch, tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"apiKey": "from-file"}), encoding="utf-8")
    auth_file.chmod(0o600)
    monkeypatch.setenv("LOCAL_LLM_COMMANDCODE_ENABLED", "true")
    monkeypatch.setenv("LOCAL_LLM_COMMANDCODE_AUTH_FILE", str(auth_file))
    monkeypatch.delenv("COMMAND_CODE_API_KEY", raising=False)
    monkeypatch.delenv("COMMANDCODE_API_KEY", raising=False)

    config = CommandCodeConfig.from_env()

    assert config.api_key == "from-file"
    assert config.credential_source == "commandcode_auth_file"
    assert config.status()["status"] == "configured"


def test_provider_maps_public_model_and_removes_local_priority():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer test-secret"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-upstream",
                "object": "chat.completion",
                "model": COMMANDCODE_UPSTREAM_MODEL_ID,
                "choices": [],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
        )

    provider = CommandCodeProvider(_config(), transport=httpx.MockTransport(handler))
    response = asyncio.run(
        provider.request(
            "chat/completions",
            {"model": COMMANDCODE_MODEL_ID, "messages": [], "priority": "normal"},
        )
    )

    assert response.status_code == 200
    assert captured["model"] == COMMANDCODE_UPSTREAM_MODEL_ID
    assert "priority" not in captured


def test_remote_model_routes_work_while_local_inference_is_disabled(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == COMMANDCODE_UPSTREAM_MODEL_ID
        assert payload["reasoning_effort"] == "high"
        assert payload["thinking"] == {"type": "enabled"}
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-upstream",
                "object": "chat.completion",
                "created": 1,
                "model": COMMANDCODE_UPSTREAM_MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    provider = CommandCodeProvider(_config(), transport=httpx.MockTransport(handler))
    monkeypatch.setenv("LOCAL_INFERENCE_ENABLED", "false")
    monkeypatch.setenv("LOCAL_LLM_COMMANDCODE_ENABLED", "true")
    monkeypatch.setattr("api.commandcode_proxy.CommandCodeProvider", lambda: provider)
    monkeypatch.setitem(app.dependency_overrides, require_api_auth, lambda: None)
    client = TestClient(app)

    models = client.get("/v1/models")
    assert models.status_code == 200
    assert [item["id"] for item in models.json()["data"]] == [COMMANDCODE_MODEL_ID]

    completion = client.post(
        "/v1/chat/completions",
        json={
            "model": COMMANDCODE_MODEL_ID,
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "high",
            "thinking": {"type": "enabled"},
        },
    )
    assert completion.status_code == 200
    assert completion.json()["choices"][0]["message"]["content"] == "ok"


def test_remote_responses_stream_is_forwarded(monkeypatch):
    class EventStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.completed"}\n\n'
            yield b"data: [DONE]\n\n"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=EventStream(),
        )

    provider = CommandCodeProvider(_config(), transport=httpx.MockTransport(handler))
    monkeypatch.setenv("LOCAL_INFERENCE_ENABLED", "false")
    monkeypatch.setenv("LOCAL_LLM_COMMANDCODE_ENABLED", "true")
    monkeypatch.setattr("api.commandcode_proxy.CommandCodeProvider", lambda: provider)
    monkeypatch.setitem(app.dependency_overrides, require_api_auth, lambda: None)

    with TestClient(app).stream(
        "POST",
        "/v1/responses",
        json={"model": COMMANDCODE_MODEL_ID, "input": "hello", "stream": True},
    ) as response:
        assert response.status_code == 200
        assert "response.completed" in "".join(response.iter_text())
