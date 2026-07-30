from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.responses import router as responses_router
from core.context_budget import ContextBudgetExceeded
from core.daemon import DaemonBusyError


class _FakeManager:
    model_id = "gemma-4-e4b-it"

    def validate_model(self, requested_model: str | None) -> str:
        return requested_model or self.model_id


class _FakeDaemon:
    def __init__(self, content: str | Exception | list[str] | dict):
        self.manager = _FakeManager()
        self._content = content
        self._index = 0
        self.calls = []

    def _next_content(self):
        if isinstance(self._content, list):
            value = self._content[min(self._index, len(self._content) - 1)]
            self._index += 1
            return value
        return self._content

    def chat(
        self,
        messages,
        model,
        max_tokens,
        temperature,
        tools=None,
        top_p=None,
        stop=None,
        priority="normal",
        timeout=None,
    ):
        content = self._next_content()
        if isinstance(content, Exception):
            raise content
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "tools": tools,
                "top_p": top_p,
                "stop": stop,
                "priority": priority,
                "timeout": timeout,
            }
        )
        if isinstance(content, dict):
            return content
        return {
            "content": content,
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


def test_responses_route_without_tools_does_not_parse_tool_payload():
    fake_daemon = _FakeDaemon('{"name":"search_web","arguments":{"query":"latest rust release"}}')
    client = _build_client(fake_daemon)
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
    assert json.loads(body["output_text"])["name"] == "search_web"
    assert fake_daemon.calls[0]["tools"] == []


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


def test_responses_route_rejects_invalid_tool_choice():
    client = _build_client(_FakeDaemon("ignored"))

    response = client.post(
        "/v1/responses",
        json={
            "model": "gemma-4-e4b-it",
            "input": [{"role": "user", "content": "回答して"}],
            "tools": [
                {
                    "type": "function",
                    "name": "search_web",
                    "description": "search",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                }
            ],
            "tool_choice": {"type": "function", "name": "fetch_content"},
        },
    )

    assert response.status_code == 400
    assert "tool_choice function" in str(response.json().get("detail", ""))


def test_responses_route_required_tool_choice_rejects_missing_tool_call():
    client = _build_client(_FakeDaemon("通常の回答です"))

    response = client.post(
        "/v1/responses",
        json={
            "model": "gemma-4-e4b-it",
            "input": [{"role": "user", "content": "検索して"}],
            "tools": [
                {
                    "type": "function",
                    "name": "search_web",
                    "description": "search",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                }
            ],
            "tool_choice": "required",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "required_tool_call_missing"


def test_responses_route_rejects_invalid_tool_arguments_schema():
    client = _build_client(_FakeDaemon('{"name":"search_web","arguments":{"query":123}}'))

    response = client.post(
        "/v1/responses",
        json={
            "model": "gemma-4-e4b-it",
            "input": [{"role": "user", "content": "検索して"}],
            "tools": [
                {
                    "type": "function",
                    "name": "search_web",
                    "description": "search",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                }
            ],
        },
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_tool_arguments"
    assert detail["toolName"] == "search_web"
    assert "arguments.query" in detail["error"]


def test_responses_route_retries_invalid_tool_arguments_once():
    fake_daemon = _FakeDaemon(
        [
            '{"name":"search_web","arguments":{"query":123}}',
            '{"name":"search_web","arguments":{"query":"latest rust release"}}',
        ]
    )
    client = _build_client(fake_daemon)

    response = client.post(
        "/v1/responses",
        json={
            "model": "gemma-4-e4b-it",
            "input": [{"role": "user", "content": "検索して"}],
            "tools": [
                {
                    "type": "function",
                    "name": "search_web",
                    "description": "search",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    assert len(fake_daemon.calls) == 2
    assert "invalid arguments" in fake_daemon.calls[1]["messages"][-1]["content"]
    body = response.json()
    assert body["output"][0]["type"] == "function_call"
    assert json.loads(body["output"][0]["arguments"])["query"] == "latest rust release"


def test_responses_route_passes_top_p_and_stop_to_daemon():
    fake_daemon = _FakeDaemon("通常の回答です")
    client = _build_client(fake_daemon)

    response = client.post(
        "/v1/responses",
        json={
            "model": "gemma-4-e4b-it",
            "input": [{"role": "user", "content": "回答して"}],
            "top_p": 0.8,
            "stop": ["END"],
        },
    )

    assert response.status_code == 200
    assert fake_daemon.calls[0]["top_p"] == 0.8
    assert fake_daemon.calls[0]["stop"] == ["END"]


def test_responses_route_returns_length_finish_reason():
    client = _build_client(
        _FakeDaemon(
            {
                "content": "途中",
                "finishReason": "length",
                "usage": {"prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13},
            }
        )
    )

    response = client.post(
        "/v1/responses",
        json={
            "model": "gemma-4-e4b-it",
            "input": [{"role": "user", "content": "短く"}],
            "max_output_tokens": 1,
        },
    )

    assert response.status_code == 200
    assert response.json()["finish_reason"] == "length"


def test_responses_route_returns_structured_context_budget_error():
    exc = ContextBudgetExceeded(
        {
            "contextWindowTokens": 176000,
            "safePromptBudgetTokens": 160000,
            "reservedOutputTokens": 16000,
            "estimatedPromptTokensBefore": 181000,
            "estimatedPromptTokensAfter": 181000,
            "compressionApplied": False,
            "compressionPolicy": "gemma4-last-mile-v1",
            "compressedSections": [],
            "droppedFields": [],
            "budgetExceeded": True,
        }
    )
    client = _build_client(_FakeDaemon(exc))

    response = client.post(
        "/v1/responses",
        json={
            "model": "gemma-4-e4b-it",
            "input": [{"role": "user", "content": "回答して"}],
            "max_output_tokens": 16000,
        },
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "context_budget_exceeded"
    assert detail["contextBudget"]["safePromptBudgetTokens"] == 160000
    assert detail["contextBudget"]["budgetExceeded"] is True


def test_responses_route_returns_503_when_daemon_is_busy():
    client = _build_client(_FakeDaemon(DaemonBusyError("local-llm daemon is busy")))

    response = client.post(
        "/v1/responses",
        json={
            "model": "gemma-4-e4b-it",
            "input": [{"role": "user", "content": "回答して"}],
        },
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "local-llm daemon is busy"
