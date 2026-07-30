from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


class FunctionDefinition(BaseModel):
    name: str
    description: str | None = None
    parameters: dict[str, Any] | None = None


class ChatTool(BaseModel):
    type: Literal["function"] = "function"
    function: FunctionDefinition


class FunctionToolCall(BaseModel):
    name: str
    arguments: str


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: FunctionToolCall


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    temperature: float = 0.0
    top_p: float | None = None
    stop: str | list[str] | None = None
    max_tokens: int = 1024
    tools: list[ChatTool] | None = None
    tool_choice: str | dict[str, Any] | None = None
    priority: Literal["high", "normal", "low"] = "normal"


class ResponseMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None


class Choice(BaseModel):
    index: int
    message: ResponseMessage
    finish_reason: Literal["stop", "tool_calls", "length", "content_filter"] = "stop"


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage
    contextBudget: dict[str, Any] | None = None


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str


class ModelListResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard]


def create_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def create_tool_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"


def now_epoch() -> int:
    return int(time.time())
