from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from api.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ResponseMessage,
    Usage,
    create_completion_id,
    now_epoch,
)
from core.daemon import get_local_llm_daemon

router = APIRouter(tags=["chat"])


def _message_to_dict(message) -> dict[str, object]:
    return {
        "role": message.role,
        "content": message.content,
        "name": message.name,
        "tool_call_id": message.tool_call_id,
    }


def _estimate_tokens(text: str) -> int:
    # Rough estimate: ~4 chars/token (OpenAI style rough estimate)
    return max(1, len(text) // 4) if text else 0


def _extract_tool_names(request: ChatCompletionRequest) -> list[str]:
    if not request.tools:
        return []
    return [tool.function.name for tool in request.tools if tool.type == "function"]


def _chunk_text(text: str, chunk_size: int = 24):
    if not text:
        return
    for idx in range(0, len(text), chunk_size):
        yield text[idx : idx + chunk_size]


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
    model_id = manager.model_id
    tool_names = _extract_tool_names(request)
    messages = [_message_to_dict(message) for message in request.messages]

    async def run_chat_once() -> dict[str, object]:
        try:
            result = await asyncio.to_thread(
                daemon.chat,
                messages,
                requested_model,
                request.max_tokens,
                request.temperature,
                tool_names,
                request.priority,
            )
            return result
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    if request.stream:
        async def event_stream() -> AsyncGenerator[str, None]:
            result = await run_chat_once()
            content = str(result["content"])

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

            for piece in _chunk_text(content):
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

    result = await run_chat_once()
    content = str(result["content"])
    prompt_text = "\n".join(str(message.get("content", "")) for message in messages)

    usage = result.get("usage")
    if isinstance(usage, dict):
        prompt_tokens = int(usage.get("prompt_tokens") or _estimate_tokens(prompt_text))
        completion_tokens = int(usage.get("completion_tokens") or _estimate_tokens(content))
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
    else:
        prompt_tokens = _estimate_tokens(prompt_text)
        completion_tokens = _estimate_tokens(content)
        total_tokens = prompt_tokens + completion_tokens

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
