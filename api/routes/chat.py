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
from api.tool_contract import build_tool_retry_message, resolve_tool_choice, validate_tool_arguments
from core.provider_profiles import sanitize_for_profile
from core.context_budget import ContextBudgetExceeded
from core.daemon import DaemonBusyError, get_local_llm_daemon
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


def _max_tokens_for_request(requested_max_tokens: int, has_tools: bool) -> int:
    output_cap = _env_int("LOCAL_LLM_MAX_OUTPUT_TOKENS", 20000)
    tool_call_cap = _env_int("LOCAL_LLM_MAX_TOOL_CALL_TOKENS", max(output_cap, 1024))
    cap = tool_call_cap if has_tools else output_cap
    return max(1, min(int(requested_max_tokens), cap))


def _normalize_stop(stop: str | list[str] | None) -> list[str] | None:
    if isinstance(stop, str):
        return [stop] if stop else None
    if isinstance(stop, list):
        values = [item for item in stop if isinstance(item, str) and item]
        return values or None
    return None


def _finish_reason_from_result(result: dict[str, object]) -> str:
    finish_reason = result.get("finishReason")
    if finish_reason in {"stop", "length", "content_filter"}:
        return str(finish_reason)
    usage = result.get("usage")
    if isinstance(usage, dict) and usage.get("finish_reason") in {"stop", "length", "content_filter"}:
        return str(usage["finish_reason"])
    return "stop"


def _append_debug_log(event: dict[str, Any]) -> None:
    log_file = os.getenv("LOCAL_LLM_DEBUG_LOG_FILE", "").strip()
    if not log_file:
        return
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        return


def _context_budget_http_detail(exc: ContextBudgetExceeded) -> dict[str, Any]:
    return {
        "code": "context_budget_exceeded",
        "message": "context_budget_exceeded",
        "contextBudget": exc.metadata,
    }


def _extract_tool_defs(request: ChatCompletionRequest) -> list[dict[str, Any]]:
    if not request.tools:
        return []
    return [tool.model_dump(mode="python") for tool in request.tools if tool.type == "function"]


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


def _invalid_tool_arguments_detail(tool_name: str, error: str) -> dict[str, Any]:
    return {
        "code": "invalid_tool_arguments",
        "message": "invalid_tool_arguments",
        "toolName": tool_name,
        "error": error,
    }


def _required_tool_missing_detail() -> dict[str, str]:
    return {
        "code": "required_tool_call_missing",
        "message": "tool_choice=required did not produce a tool call",
    }


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
    request_tool_defs = _extract_tool_defs(request)
    tool_defs, allowed_tool_names, tool_choice_mode, tool_choice_error = resolve_tool_choice(
        request_tool_defs,
        request.tool_choice,
    )
    if tool_choice_error:
        raise HTTPException(status_code=400, detail=tool_choice_error)

    max_tokens = _max_tokens_for_request(request.max_tokens, has_tools=bool(tool_defs))

    async def run_chat_once(run_messages: list[dict[str, object]] | None = None) -> dict[str, object]:
        try:
            result = await asyncio.to_thread(
                daemon.chat,
                run_messages or messages,
                requested_model,
                max_tokens,
                request.temperature,
                tool_defs,
                request.top_p,
                _normalize_stop(request.stop),
                request.priority,
            )
            return result
        except ContextBudgetExceeded as exc:
            raise HTTPException(status_code=400, detail=_context_budget_http_detail(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except DaemonBusyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
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

    if request.stream and not tool_defs:
        if daemon.is_busy():
            raise HTTPException(status_code=503, detail="local-llm daemon is busy")

        async def live_event_stream() -> AsyncGenerator[str, None]:
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

            queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

            def produce() -> None:
                try:
                    for piece in daemon.chat_stream(
                        messages,
                        requested_model,
                        max_tokens,
                        request.temperature,
                        [],
                        request.top_p,
                        _normalize_stop(request.stop),
                    ):
                        asyncio.run_coroutine_threadsafe(queue.put(("chunk", piece)), loop)
                    asyncio.run_coroutine_threadsafe(queue.put(("done", None)), loop)
                except Exception as exc:
                    asyncio.run_coroutine_threadsafe(queue.put(("error", str(exc))), loop)

            loop = asyncio.get_running_loop()
            producer_task = asyncio.create_task(asyncio.to_thread(produce))
            try:
                while True:
                    kind, value = await queue.get()
                    if kind == "chunk":
                        content = value or ""
                        if not content:
                            continue
                        chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model_id,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": content},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                        continue
                    if kind == "error":
                        chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model_id,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": f"\n[stream error] {value}"},
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                        break
                    break
            finally:
                await producer_task

            last_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_id,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(last_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(live_event_stream(), media_type="text/event-stream")

    result = await run_chat_once()
    finish_reason = _finish_reason_from_result(result)
    raw_content = str(result.get("content", ""))
    context_budget = result.get("contextBudget")
    if not isinstance(context_budget, dict):
        context_budget = None
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
    if parsed_tool_call:
        validation_error = validate_tool_arguments(parsed_tool_call, tool_defs)
        if validation_error:
            tool_name = normalize_tool_name(str(parsed_tool_call.get("name", "")))
            retry_result = await run_chat_once(
                [*messages, build_tool_retry_message(tool_name, validation_error)]
            )
            retry_raw_content = str(retry_result.get("content", ""))
            retry_parsed_tool_call = parse_tool_call(
                retry_raw_content,
                allowed_tool_names=allowed_tool_names,
            )
            retry_validation_error = (
                validate_tool_arguments(retry_parsed_tool_call, tool_defs)
                if retry_parsed_tool_call
                else "retry did not produce a valid tool call"
            )
            if retry_parsed_tool_call and not retry_validation_error:
                result = retry_result
                raw_content = retry_raw_content
                parsed_tool_call = retry_parsed_tool_call
                finish_reason = _finish_reason_from_result(result)
            else:
                validation_error = retry_validation_error or validation_error
                _append_debug_log(
                    {
                        "ts": int(time.time()),
                        "event": "chat_completions.invalid_tool_arguments_retry_failed",
                        "model": request.model,
                        "tool_name": tool_name,
                        "error": validation_error,
                        "raw_prefix": retry_raw_content[:220],
                    }
                )
                raise HTTPException(
                    status_code=400,
                    detail=_invalid_tool_arguments_detail(tool_name, validation_error),
                )
    elif tool_choice_mode == "required":
        raise HTTPException(status_code=400, detail=_required_tool_missing_detail())

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

            content = sanitize_for_profile(sanitize_assistant_text(raw_content), requested_model)
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
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
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
            contextBudget=context_budget,
        )

    content = sanitize_for_profile(sanitize_assistant_text(raw_content), requested_model)
    return ChatCompletionResponse(
        id=completion_id,
        created=created,
        model=model_id,
        choices=[
                Choice(
                    index=0,
                    message=ResponseMessage(content=content),
                    finish_reason=finish_reason,
                )
        ],
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        ),
        contextBudget=context_budget,
    )
