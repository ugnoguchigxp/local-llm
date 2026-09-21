from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from agent_runtime.grok.error_mapping import protocol_error, redact_provider_text


MAX_EVENT_TEXT = 512 * 1024
_TOOL_KINDS = {
    "read",
    "edit",
    "delete",
    "move",
    "search",
    "execute",
    "think",
    "fetch",
    "switch_mode",
    "other",
}
_PLAN_PRIORITIES = {"high", "medium", "low"}
_PLAN_STATUSES = {"pending", "in_progress", "completed"}
_SENSITIVE_KEY = re.compile(r"(?i)(api.?key|authorization|cookie|credential|password|secret|token)")
@dataclass(frozen=True)
class MappedUpdate:
    event_type: str
    data: dict[str, Any]


def map_session_update(update: Any) -> MappedUpdate | None:
    if not isinstance(update, dict):
        raise protocol_error("Grok session update must be an object.")
    kind = update.get("sessionUpdate")
    if not isinstance(kind, str) or not kind:
        raise protocol_error("Grok session update has no discriminator.")

    if kind in {"agent_message_chunk", "agent_thought_chunk"}:
        content = update.get("content")
        if not isinstance(content, dict) or content.get("type") != "text":
            raise protocol_error(f"Grok {kind} content must be text.")
        text = content.get("text")
        if not isinstance(text, str):
            raise protocol_error(f"Grok {kind} text is invalid.")
        if len(text.encode("utf-8")) > MAX_EVENT_TEXT:
            raise protocol_error(f"Grok {kind} text is too large.")
        return MappedUpdate(
            "message.delta" if kind == "agent_message_chunk" else "reasoning.delta",
            {"text": text},
        )

    if kind in {"tool_call", "tool_call_update"}:
        tool_id = update.get("toolCallId")
        if not isinstance(tool_id, str) or not tool_id or len(tool_id) > 1024:
            raise protocol_error("Grok tool update has no toolCallId.")
        status = update.get("status", "pending")
        if not isinstance(status, str) or status not in {
            "pending",
            "in_progress",
            "completed",
            "failed",
        }:
            raise protocol_error("Grok tool update status is invalid.")
        event_type = (
            "tool.completed"
            if status in {"completed", "failed"}
            else "tool.started"
            if kind == "tool_call"
            else "tool.updated"
        )
        data: dict[str, Any] = {"item_id": tool_id, "status": status}
        title = update.get("title")
        if kind == "tool_call" and (not isinstance(title, str) or not title):
            raise protocol_error("Grok tool call has no title.")
        tool_kind = update.get("kind")
        if tool_kind is not None and (
            not isinstance(tool_kind, str) or tool_kind not in _TOOL_KINDS
        ):
            raise protocol_error("Grok tool kind is invalid.")
        for source, target in (("title", "title"), ("kind", "kind")):
            value = update.get(source)
            if isinstance(value, str):
                data[target] = redact_provider_text(value[:4096])
        if status == "failed":
            data["failed"] = True
        return MappedUpdate(event_type, data)

    if kind == "plan":
        entries = update.get("entries")
        if not isinstance(entries, list):
            raise protocol_error("Grok plan entries are invalid.")
        if len(entries) > 100:
            raise protocol_error("Grok plan contains too many entries.")
        safe_entries: list[dict[str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise protocol_error("Grok plan contains an invalid entry.")
            content = entry.get("content")
            priority = entry.get("priority")
            status = entry.get("status")
            if (
                not isinstance(content, str)
                or not isinstance(priority, str)
                or priority not in _PLAN_PRIORITIES
                or not isinstance(status, str)
                or status not in _PLAN_STATUSES
            ):
                raise protocol_error("Grok plan entry is invalid.")
            safe_entries.append(
                {
                    "content": redact_provider_text(content[:4096]),
                    "priority": priority,
                    "status": status,
                }
            )
        return MappedUpdate("plan.updated", {"entries": safe_entries})

    if kind in {"current_model_update", "current_mode_update", "config_option_update"}:
        identity_field = (
            "modelId"
            if kind == "current_model_update"
            else "currentModeId"
            if kind == "current_mode_update"
            else "configOptions"
        )
        identity = update.get(identity_field)
        if kind == "config_option_update":
            if not isinstance(identity, list):
                raise protocol_error("Grok config update is invalid.")
        elif not isinstance(identity, str) or not identity:
            raise protocol_error("Grok model or mode update has no identity.")
        return MappedUpdate(
            "session.invariant_changed",
            {
                "reason": (
                    "model_changed"
                    if kind == "current_model_update"
                    else "mode_changed"
                    if kind == "current_mode_update"
                    else "config_changed"
                )
            },
        )

    if kind in {
        "user_message_chunk",
        "available_commands_update",
    }:
        return None
    return MappedUpdate("provider.event", {"kind": kind[:128]})


def permission_summary(
    params: dict[str, Any],
) -> tuple[str, list[dict[str, str]], dict[str, Any], bool]:
    session_id = params.get("sessionId")
    options = params.get("options")
    if not isinstance(session_id, str) or not session_id:
        raise protocol_error("Grok permission request has no sessionId.")
    if not isinstance(options, list) or not options:
        raise protocol_error("Grok permission request has no options.")
    if len(options) > 64:
        raise protocol_error("Grok permission request has too many options.")
    safe_options: list[dict[str, str]] = []
    option_ids: set[str] = set()
    for option in options:
        if not isinstance(option, dict):
            raise protocol_error("Grok permission option is invalid.")
        option_id = option.get("optionId")
        name = option.get("name")
        kind = option.get("kind")
        if not all(isinstance(value, str) and value for value in (option_id, name, kind)):
            raise protocol_error("Grok permission option identity is invalid.")
        if len(option_id) > 1024 or len(kind) > 128:
            raise protocol_error("Grok permission option identity is too large.")
        if option_id in option_ids:
            raise protocol_error("Grok permission option ids must be unique.")
        option_ids.add(option_id)
        safe_options.append(
            {
                "option_id": option_id,
                "name": redact_provider_text(name[:256]),
                "kind": kind,
            }
        )
    tool_call = params.get("toolCall")
    if not isinstance(tool_call, dict):
        raise protocol_error("Grok permission request has no toolCall.")
    tool_call_id = tool_call.get("toolCallId")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise protocol_error("Grok permission tool call has no toolCallId.")
    subject: dict[str, Any] = {"tool_call_id": tool_call_id[:1024]}
    for source, target in (("title", "title"), ("kind", "kind"), ("status", "status")):
        value = tool_call.get(source)
        if isinstance(value, str):
            subject[target] = redact_provider_text(value[:4096])
    raw_input = tool_call.get("rawInput")
    input_available = raw_input not in (None, "", [], {})
    allow_once_safe = False
    if input_available:
        safe_input, allow_once_safe = _safe_value(raw_input)
        subject["input"] = safe_input
    locations = tool_call.get("locations")
    if isinstance(locations, list):
        safe_locations: list[dict[str, Any]] = []
        for location in locations[:32]:
            if not isinstance(location, dict):
                continue
            path = location.get("path")
            line = location.get("line")
            safe_location: dict[str, Any] = {}
            if isinstance(path, str):
                safe_location["path"] = redact_provider_text(path[:4096])
            if isinstance(line, int) and not isinstance(line, bool) and line >= 0:
                safe_location["line"] = line
            if safe_location:
                safe_locations.append(safe_location)
        if safe_locations:
            subject["locations"] = safe_locations
    subject["input_available"] = input_available
    subject["approval_context_complete"] = allow_once_safe
    return session_id, safe_options, subject, allow_once_safe


def _safe_value(value: Any, *, depth: int = 0) -> tuple[Any, bool]:
    if depth >= 5:
        return "[truncated]", False
    if value is None or isinstance(value, (bool, int)):
        return value, True
    if isinstance(value, float):
        return (
            (value, True)
            if math.isfinite(value)
            else ("[non-finite-number]", False)
        )
    if isinstance(value, str):
        shortened = value[:16_384]
        redacted = redact_provider_text(shortened, limit=16_384)
        return redacted, len(value) <= 16_384 and redacted == shortened
    if isinstance(value, list):
        members = [_safe_value(item, depth=depth + 1) for item in value[:64]]
        return [member for member, _complete in members], len(value) <= 64 and all(
            complete for _member, complete in members
        )
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        complete = len(value) <= 64
        for key, member in list(value.items())[:64]:
            if not isinstance(key, str):
                complete = False
                continue
            safe_key = key[:256]
            if len(key) > 256 or safe_key in result:
                complete = False
            if _SENSITIVE_KEY.search(key):
                result[safe_key] = "[redacted]"
                complete = False
            else:
                safe_member, member_complete = _safe_value(member, depth=depth + 1)
                result[safe_key] = safe_member
                complete = complete and member_complete
        return result, complete
    return f"[{type(value).__name__}]", False
