from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.responses import router as responses_router


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
                "prompt_tokens": 12,
                "completion_tokens": 7,
                "total_tokens": 19,
            },
        }


def _build_client(fake_daemon: _FakeDaemon) -> TestClient:
    from api.routes import responses as responses_module

    app = FastAPI()
    responses_module.get_local_llm_daemon = lambda: fake_daemon
    app.include_router(responses_router)
    return TestClient(app)


def test_responses_route_accepts_tool_name_payload():
    client = _build_client(
        _FakeDaemon('{"tool_name":"list_directory","arguments":{"path":"/Users/y.noguchi/Code/local-llm"}}')
    )
    response = client.post(
        "/v1/responses",
        json={
            "model": "gemma-4-e4b-it",
            "input": [{"role": "user", "content": "プロジェクトの中身を確認して"}],
            "tools": [
                {
                    "type": "function",
                    "name": "list_directory",
                    "description": "list directory",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["output"][0]["type"] == "function_call"
    assert body["output"][0]["name"] == "list_directory"
    assert body["output"][0]["arguments"] == '{"path": "/Users/y.noguchi/Code/local-llm"}'
    assert body["output_text"] == ""


def test_responses_route_returns_message_without_tool_call():
    client = _build_client(_FakeDaemon("通常の回答です"))
    response = client.post(
        "/v1/responses",
        json={
            "model": "gemma-4-e4b-it",
            "input": [{"role": "user", "content": "回答して"}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["output"][0]["type"] == "message"
    assert body["output"][0]["content"][0]["text"] == "通常の回答です"


def test_responses_route_keeps_nested_tool_arguments_as_json():
    client = _build_client(
        _FakeDaemon(
            '{"tool_name":"edit_file","arguments":{"path":"README.md","edits":[{"old_text":"A","new_text":"B"}],"dry_run":false,"limit":2}}'
        )
    )
    response = client.post(
        "/v1/responses",
        json={
            "model": "gemma-4-e4b-it",
            "input": [{"role": "user", "content": "READMEを更新して"}],
            "tools": [
                {
                    "type": "function",
                    "name": "edit_file",
                    "description": "edit file",
                    "parameters": {"type": "object"},
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["output"][0]["type"] == "function_call"
    decoded = json.loads(body["output"][0]["arguments"])
    assert decoded["path"] == "README.md"
    assert decoded["edits"] == [{"old_text": "A", "new_text": "B"}]
    assert decoded["dry_run"] is False
    assert decoded["limit"] == 2


def test_responses_route_parses_assistant_requested_tool_calls_text():
    client = _build_client(
        _FakeDaemon(
            'Assistant requested tool calls:\n- edit_file({"path":"hoge.md","mode":"w","content":"Hello"})'
        )
    )
    response = client.post(
        "/v1/responses",
        json={
            "model": "gemma-4-e4b-it",
            "input": [{"role": "user", "content": "hoge.md を作って"}],
            "tools": [
                {
                    "type": "function",
                    "name": "edit_file",
                    "description": "edit file",
                    "parameters": {"type": "object"},
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["output"][0]["type"] == "function_call"
    decoded = json.loads(body["output"][0]["arguments"])
    assert decoded["path"] == "hoge.md"
    assert decoded["mode"] == "w"
    assert decoded["content"] == "Hello"


def test_responses_route_parses_python_kwargs_style_tool_call_text():
    client = _build_client(
        _FakeDaemon(
            "Assistant requested tool calls:\n"
            "- edit_file({ display_description='Creating hoge.md', path='hoge.md', mode='w', content='Hello' })"
        )
    )
    response = client.post(
        "/v1/responses",
        json={
            "model": "gemma-4-e4b-it",
            "input": [{"role": "user", "content": "hoge.md を作って"}],
            "tools": [
                {
                    "type": "function",
                    "name": "edit_file",
                    "description": "edit file",
                    "parameters": {"type": "object"},
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["output"][0]["type"] == "function_call"
    decoded = json.loads(body["output"][0]["arguments"])
    assert decoded["path"] == "hoge.md"
    assert decoded["mode"] == "w"
    assert decoded["content"] == "Hello"
