from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from core.daemon import get_local_llm_daemon
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

    tools = _normalize_tools(payload)
    allowed_tool_names = {
        normalize_tool_name(str(tool.get("function", {}).get("name", "")))
        for tool in tools
        if isinstance(tool, dict)
    }

    tool_choice = _normalize_tool_choice(payload)
    if isinstance(tool_choice, str) and tool_choice.lower() == "none":
        tools = []
        allowed_tool_names = set()
    elif isinstance(tool_choice, dict):
        forced_name = tool_choice.get("function", {}).get("name")
        if isinstance(forced_name, str):
            normalized = normalize_tool_name(forced_name)
            tools = [
                tool
                for tool in tools
                if normalize_tool_name(str(tool.get("function", {}).get("name", ""))) == normalized
            ]
            allowed_tool_names = {normalized}

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
            priority,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    parsed_tool_call = parse_tool_call(raw_content, allowed_tool_names=allowed_tool_names or None)
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
        text = sanitize_assistant_text(raw_content)
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
    }
