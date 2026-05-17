from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from api.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ResponseMessage,
    ToolCall,
    FunctionToolCall,
    Usage,
    create_completion_id,
    create_tool_call_id,
    now_epoch,
)
from core.daemon import get_local_llm_daemon
from core.tool_calling import normalize_tool_name, parse_tool_call, sanitize_assistant_text

router = APIRouter(tags=["chat"])


def _message_to_dict(message) -> dict[str, object]:
    tool_calls = None
    if getattr(message, "tool_calls", None):
        tool_calls = [tool_call.model_dump(mode="python") for tool_call in message.tool_calls]
    return {
        "role": message.role,
        "content": message.content,
        "name": message.name,
        "tool_call_id": message.tool_call_id,
        "tool_calls": tool_calls,
    }


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _append_debug_log(event: dict[str, Any]) -> None:
    log_file = os.getenv("LOCAL_LLM_DEBUG_LOG_FILE", "").strip()
    if not log_file:
        return
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        return


def _extract_tool_defs(request: ChatCompletionRequest) -> list[dict[str, Any]]:
    if not request.tools:
        return []
    return [tool.model_dump(mode="python") for tool in request.tools if tool.type == "function"]


def _resolve_allowed_tool_names(request: ChatCompletionRequest) -> tuple[set[str], str | None]:
    tool_choice = request.tool_choice
    if not request.tools:
        if isinstance(tool_choice, dict):
            return set(), "tool_choice requires tools, but no tools were provided"
        if isinstance(tool_choice, str) and tool_choice.lower() in {"required"}:
            return set(), "tool_choice=required requires tools, but no tools were provided"
        return set(), None

    available = {
        normalize_tool_name(tool.function.name)
        for tool in request.tools
        if tool.type == "function" and tool.function.name
    }

    if isinstance(tool_choice, str):
        if tool_choice.lower() == "none":
            return set(), None
        return available, None

    if isinstance(tool_choice, dict):
        function = tool_choice.get("function")
        forced_name = function.get("name") if isinstance(function, dict) else None
        if isinstance(forced_name, str) and forced_name:
            normalized = normalize_tool_name(forced_name)
            if normalized in available:
                return {normalized}, None
            return set(), f"tool_choice function '{forced_name}' is not present in tools"

    return available, None


def _build_tool_call_response(parsed_tool_call: dict[str, Any]) -> tuple[ToolCall, str]:
    raw_name = str(parsed_tool_call.get("name", ""))
    normalized_name = normalize_tool_name(raw_name)
    arguments = parsed_tool_call.get("arguments", {})
    if not isinstance(arguments, dict):
        arguments = {}

    tool_call = ToolCall(
        id=create_tool_call_id(),
        type="function",
        function=FunctionToolCall(
            name=normalized_name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        ),
    )
    return tool_call, normalized_name


@router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(request: ChatCompletionRequest):
    daemon = get_local_llm_daemon()
    manager = daemon.manager

    try:
        requested_model = manager.validate_model(request.model)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if not request.messages:
        raise HTTPException(status_code=400, detail="messages must not be empty")

    completion_id = create_completion_id()
    created = now_epoch()
    model_id = request.model

    messages = [_message_to_dict(message) for message in request.messages]
    allowed_tool_names, tool_choice_error = _resolve_allowed_tool_names(request)
    if tool_choice_error:
        raise HTTPException(status_code=400, detail=tool_choice_error)
    tool_defs = _extract_tool_defs(request)
    if allowed_tool_names:
        tool_defs = [
            tool
            for tool in tool_defs
            if normalize_tool_name(str(tool.get("function", {}).get("name", ""))) in allowed_tool_names
        ]
    else:
        tool_defs = []

    max_tokens = max(1, min(int(request.max_tokens), _env_int("LOCAL_LLM_MAX_OUTPUT_TOKENS", 512)))

    async def run_chat_once() -> dict[str, object]:
        try:
            result = await asyncio.to_thread(
                daemon.chat,
                messages,
                requested_model,
                max_tokens,
                request.temperature,
                tool_defs,
                request.priority,
            )
            return result
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            detail = str(exc)
            if detail.startswith("prompt_too_large:") or detail.startswith("context_length_exceeded:"):
                raise HTTPException(status_code=400, detail=detail) from exc
            raise HTTPException(status_code=500, detail=detail) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"unexpected daemon error: {exc}",
            ) from exc

    result = await run_chat_once()
    raw_content = str(result.get("content", ""))
    parsed_tool_call = parse_tool_call(raw_content, allowed_tool_names=allowed_tool_names)
    _append_debug_log(
        {
            "ts": int(time.time()),
            "event": "chat_completions.parse_result",
            "model": request.model,
            "allowed_tool_names": sorted(list(allowed_tool_names)),
            "request_tool_names": [
                normalize_tool_name(tool.function.name)
                for tool in (request.tools or [])
                if tool.type == "function" and tool.function and tool.function.name
            ],
            "parsed_tool_call_name": parsed_tool_call.get("name") if parsed_tool_call else None,
            "parsed_tool_call_arguments_type": (
                type(parsed_tool_call.get("arguments")).__name__ if parsed_tool_call else None
            ),
            "raw_prefix": raw_content[:220],
        }
    )

    if request.stream:

        async def event_stream() -> AsyncGenerator[str, None]:
            first_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_id,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n"

            if parsed_tool_call:
                tool_call, _ = _build_tool_call_response(parsed_tool_call)
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_id,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": tool_call.id,
                                        "type": "function",
                                        "function": {
                                            "name": tool_call.function.name,
                                            "arguments": tool_call.function.arguments,
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                last_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_id,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                }
                yield f"data: {json.dumps(last_chunk, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            content = sanitize_assistant_text(raw_content)
            for idx in range(0, len(content), 24):
                piece = content[idx : idx + 24]
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_id,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": piece},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

            last_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_id,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(last_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    prompt_text = "\n".join(str(message.get("content", "")) for message in messages)
    usage = result.get("usage")
    if isinstance(usage, dict):
        prompt_tokens = int(usage.get("prompt_tokens") or _estimate_tokens(prompt_text))
        completion_tokens = int(usage.get("completion_tokens") or _estimate_tokens(raw_content))
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
    else:
        prompt_tokens = _estimate_tokens(prompt_text)
        completion_tokens = _estimate_tokens(raw_content)
        total_tokens = prompt_tokens + completion_tokens

    if parsed_tool_call:
        tool_call, _ = _build_tool_call_response(parsed_tool_call)
        return ChatCompletionResponse(
            id=completion_id,
            created=created,
            model=model_id,
            choices=[
                Choice(
                    index=0,
                    message=ResponseMessage(content=None, tool_calls=[tool_call]),
                    finish_reason="tool_calls",
                )
            ],
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            ),
        )

    content = sanitize_assistant_text(raw_content)
    return ChatCompletionResponse(
        id=completion_id,
        created=created,
        model=model_id,
        choices=[
            Choice(
                index=0,
                message=ResponseMessage(content=content),
                finish_reason="stop",
            )
        ],
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        ),
    )
