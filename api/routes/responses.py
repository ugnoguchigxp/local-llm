from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from api.tool_contract import build_tool_retry_message, resolve_tool_choice, validate_tool_arguments
from core.context_budget import ContextBudgetExceeded
from core.daemon import DaemonBusyError, get_local_llm_daemon
from core.provider_profiles import sanitize_for_profile
from core.tool_calling import normalize_tool_name, parse_tool_call, sanitize_assistant_text

router = APIRouter(tags=["responses"])


def _append_debug_log(event: dict[str, Any]) -> None:
    log_file = os.getenv("LOCAL_LLM_DEBUG_LOG_FILE", "").strip()
    if not log_file:
        return
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        return


def _response_id() -> str:
    return f"resp_{uuid.uuid4().hex[:24]}"


def _output_message_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"


def _function_call_id() -> str:
    return f"fc_{uuid.uuid4().hex[:24]}"


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
                continue
            # Responses API often uses typed content parts with nested text.
            if item.get("type") in {"input_text", "output_text", "text"}:
                value = item.get("text")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
    return str(content)


def _normalize_input_to_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    source = payload.get("input")
    if source is None:
        source = payload.get("messages")

    messages: list[dict[str, str]] = []

    if isinstance(source, str):
        messages.append({"role": "user", "content": source})
    elif isinstance(source, dict):
        role = str(source.get("role", "user"))
        messages.append({"role": role, "content": _extract_text(source.get("content"))})
    elif isinstance(source, list):
        for item in source:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue

            if item.get("type") == "message":
                role = str(item.get("role", "user"))
                content = _extract_text(item.get("content"))
                messages.append({"role": role, "content": content})
                continue

            if "role" in item:
                role = str(item.get("role", "user"))
                content = _extract_text(item.get("content"))
                messages.append({"role": role, "content": content})
                continue

            if item.get("type") in {"input_text", "output_text", "text"}:
                messages.append({"role": "user", "content": _extract_text(item)})
                continue

            if "text" in item:
                messages.append({"role": "user", "content": _extract_text(item)})

    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.insert(0, {"role": "system", "content": instructions})

    return [m for m in messages if m.get("content")]


def _normalize_tools(payload: dict[str, Any]) -> list[dict[str, Any]]:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return []

    normalized: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function":
            continue

        function = tool.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str) and name:
                normalized.append(
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": function.get("description"),
                            "parameters": function.get("parameters"),
                        },
                    }
                )
            continue

        name = tool.get("name")
        if isinstance(name, str) and name:
            normalized.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": tool.get("description"),
                        "parameters": tool.get("parameters"),
                    },
                }
            )

    return normalized


def _normalize_tool_choice(payload: dict[str, Any]) -> str | dict[str, Any] | None:
    raw = payload.get("tool_choice")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        if raw.get("type") == "function" and isinstance(raw.get("name"), str):
            return {"type": "function", "function": {"name": raw["name"]}}
        return raw
    return None


def _normalize_stop(raw: Any) -> list[str] | None:
    if isinstance(raw, str):
        return [raw] if raw else None
    if isinstance(raw, list):
        values = [item for item in raw if isinstance(item, str) and item]
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


def _context_budget_http_detail(exc: ContextBudgetExceeded) -> dict[str, Any]:
    return {
        "code": "context_budget_exceeded",
        "message": "context_budget_exceeded",
        "contextBudget": exc.metadata,
    }


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


@router.post("/v1/responses")
async def create_response(payload: dict[str, Any]) -> dict[str, Any]:
    daemon = get_local_llm_daemon()
    manager = daemon.manager

    requested_model_raw = payload.get("model")
    requested_model = str(requested_model_raw) if requested_model_raw else None
    try:
        requested_model_path = manager.validate_model(requested_model)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    messages = _normalize_input_to_messages(payload)
    if not messages:
        raise HTTPException(status_code=400, detail="input/messages must not be empty")

    request_tools = _normalize_tools(payload)
    tools, allowed_tool_names, tool_choice_mode, tool_choice_error = resolve_tool_choice(
        request_tools,
        _normalize_tool_choice(payload),
    )
    if tool_choice_error:
        raise HTTPException(status_code=400, detail=tool_choice_error)

    max_output_tokens = payload.get("max_output_tokens")
    if max_output_tokens is None:
        max_output_tokens = payload.get("max_tokens", 1024)
    try:
        max_output_tokens = int(max_output_tokens)
    except (TypeError, ValueError):
        max_output_tokens = 1024

    temperature_raw = payload.get("temperature", 0.0)
    try:
        temperature = float(temperature_raw)
    except (TypeError, ValueError):
        temperature = 0.0
    top_p_raw = payload.get("top_p")
    try:
        top_p = float(top_p_raw) if top_p_raw is not None else None
    except (TypeError, ValueError):
        top_p = None
    stop = _normalize_stop(payload.get("stop"))

    priority = payload.get("priority", "normal")
    if not isinstance(priority, str):
        priority = "normal"

    try:
        result = await asyncio.to_thread(
            daemon.chat,
            messages,
            requested_model_path,
            max_output_tokens,
            temperature,
            tools,
            top_p,
            stop,
            priority,
        )
    except ContextBudgetExceeded as exc:
        raise HTTPException(status_code=400, detail=_context_budget_http_detail(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DaemonBusyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        detail = str(exc)
        if detail.startswith("prompt_too_large:") or detail.startswith("context_length_exceeded:"):
            raise HTTPException(status_code=400, detail=detail) from exc
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"unexpected daemon error: {exc}") from exc

    raw_content = str(result.get("content", ""))
    finish_reason = _finish_reason_from_result(result)
    context_budget = result.get("contextBudget")
    if not isinstance(context_budget, dict):
        context_budget = None
    parsed_tool_call = parse_tool_call(raw_content, allowed_tool_names=allowed_tool_names)
    _append_debug_log(
        {
            "ts": int(time.time()),
            "event": "responses.parse_result",
            "model": requested_model,
            "allowed_tool_names": sorted(list(allowed_tool_names)),
            "request_tool_names": [
                normalize_tool_name(str(tool.get("function", {}).get("name", "")))
                for tool in tools
                if isinstance(tool, dict)
            ],
            "parsed_tool_call_name": parsed_tool_call.get("name") if parsed_tool_call else None,
            "parsed_tool_call_arguments_type": (
                type(parsed_tool_call.get("arguments")).__name__ if parsed_tool_call else None
            ),
            "raw_prefix": raw_content[:220],
        }
    )
    if parsed_tool_call:
        validation_error = validate_tool_arguments(parsed_tool_call, tools)
        if validation_error:
            tool_name = normalize_tool_name(str(parsed_tool_call.get("name", "")))
            retry_result = await asyncio.to_thread(
                daemon.chat,
                [*messages, build_tool_retry_message(tool_name, validation_error)],
                requested_model_path,
                max_output_tokens,
                temperature,
                tools,
                top_p,
                stop,
                priority,
            )
            retry_raw_content = str(retry_result.get("content", ""))
            retry_parsed_tool_call = parse_tool_call(
                retry_raw_content,
                allowed_tool_names=allowed_tool_names,
            )
            retry_validation_error = (
                validate_tool_arguments(retry_parsed_tool_call, tools)
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
                        "event": "responses.invalid_tool_arguments_retry_failed",
                        "model": requested_model,
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

    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    input_tokens = int(usage.get("prompt_tokens") or max(1, len(json.dumps(messages, ensure_ascii=False)) // 4))
    output_tokens = int(usage.get("completion_tokens") or max(1, len(raw_content) // 4))
    total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))

    response_id = _response_id()
    model_id = requested_model or str(payload.get("model") or "")

    if parsed_tool_call:
        name = normalize_tool_name(str(parsed_tool_call.get("name", "")))
        arguments = parsed_tool_call.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}
        fc_id = _function_call_id()
        output = [
            {
                "type": "function_call",
                "id": fc_id,
                "call_id": fc_id,
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
                "status": "completed",
            }
        ]
        output_text = ""
    else:
        text = sanitize_for_profile(sanitize_assistant_text(raw_content), requested_model_path)
        output = [
            {
                "type": "message",
                "id": _output_message_id(),
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                    }
                ],
                "status": "completed",
            }
        ]
        output_text = text

    return {
        "id": response_id,
        "object": "response",
        "status": "completed",
        "model": model_id,
        "output": output,
        "output_text": output_text,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        },
        "finish_reason": "tool_calls" if parsed_tool_call else finish_reason,
        "contextBudget": context_budget,
    }
