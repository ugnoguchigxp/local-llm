from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

COMPRESSION_POLICY = "gemma4-last-mile-v1"
_DROP_FIELD_NAMES = {
    "debugRawPayload",
    "debug_raw_payload",
    "rawPayload",
    "raw_payload",
}


class ContextBudgetExceeded(ValueError):
    def __init__(self, metadata: dict[str, Any]) -> None:
        super().__init__("context_budget_exceeded")
        self.metadata = metadata


@dataclass
class CompressionResult:
    messages: list[dict[str, str]]
    compression_applied: bool = False
    compressed_sections: list[str] = field(default_factory=list)
    dropped_fields: list[str] = field(default_factory=list)


def build_budget_metadata(
    *,
    context_window_tokens: int,
    reserved_output_tokens: int,
    safe_prompt_budget_tokens: int | None = None,
    estimated_prompt_tokens_before: int,
    estimated_prompt_tokens_after: int | None = None,
    compression_applied: bool = False,
    compressed_sections: list[str] | None = None,
    dropped_fields: list[str] | None = None,
    budget_exceeded: bool = False,
) -> dict[str, Any]:
    if safe_prompt_budget_tokens is None:
        safe_prompt_budget_tokens = max(0, context_window_tokens - reserved_output_tokens)
    after = (
        estimated_prompt_tokens_before
        if estimated_prompt_tokens_after is None
        else estimated_prompt_tokens_after
    )
    return {
        "contextWindowTokens": context_window_tokens,
        "safePromptBudgetTokens": safe_prompt_budget_tokens,
        "reservedOutputTokens": reserved_output_tokens,
        "estimatedPromptTokensBefore": estimated_prompt_tokens_before,
        "estimatedPromptTokensAfter": after,
        "compressionApplied": compression_applied,
        "compressionPolicy": COMPRESSION_POLICY,
        "compressedSections": compressed_sections or [],
        "droppedFields": dropped_fields or [],
        "budgetExceeded": budget_exceeded,
    }


def compress_messages(
    messages: list[dict[str, str]],
    *,
    safe_prompt_budget_tokens: int,
    estimated_prompt_tokens: int,
) -> CompressionResult:
    """Mechanically shorten compressible context without semantic prioritization."""
    copied = [dict(message) for message in messages]
    if not copied or estimated_prompt_tokens <= safe_prompt_budget_tokens:
        return CompressionResult(messages=copied)

    ratio = max(0.08, min(0.75, safe_prompt_budget_tokens / max(estimated_prompt_tokens, 1)))
    target_chars = max(800, int(_total_content_chars(copied) * ratio * 0.9))
    excess_chars = max(0, _total_content_chars(copied) - target_chars)
    if excess_chars <= 0:
        return CompressionResult(messages=copied)

    compressed_sections: list[str] = []
    dropped_fields: list[str] = []
    protected_indexes = _protected_message_indexes(copied)

    candidates = [
        (index, _section_for_message(message))
        for index, message in enumerate(copied)
        if index not in protected_indexes and _section_for_message(message) is not None
    ]
    candidates.sort(key=lambda item: len(copied[item[0]].get("content", "")), reverse=True)

    remaining_excess = excess_chars
    for index, section in candidates:
        if remaining_excess <= 0:
            break
        content = copied[index].get("content", "")
        if len(content) < 1200:
            continue
        max_chars = max(500, len(content) - remaining_excess)
        compacted, fields = _compress_content(content, section=section or "large_context", max_chars=max_chars)
        if len(compacted) >= len(content):
            continue
        copied[index]["content"] = compacted
        remaining_excess -= len(content) - len(compacted)
        if section and section not in compressed_sections:
            compressed_sections.append(section)
        for field_name in fields:
            if field_name not in dropped_fields:
                dropped_fields.append(field_name)

    return CompressionResult(
        messages=copied,
        compression_applied=bool(compressed_sections or dropped_fields),
        compressed_sections=compressed_sections,
        dropped_fields=dropped_fields,
    )


def _total_content_chars(messages: list[dict[str, str]]) -> int:
    return sum(len(str(message.get("content", ""))) for message in messages)


def _protected_message_indexes(messages: list[dict[str, str]]) -> set[int]:
    protected: set[int] = {
        index for index, message in enumerate(messages) if message.get("role") == "system"
    }
    for index in range(len(messages) - 1, -1, -1):
        content = messages[index].get("content", "")
        if not content.startswith("Tool result"):
            protected.add(index)
            break
    return protected


def _section_for_message(message: dict[str, str]) -> str | None:
    content = message.get("content", "")
    lowered = content.lower()
    stripped = content.lstrip()
    if content.startswith("Tool result"):
        return "tool_outputs"
    if len(content) > 2000 and stripped[:1] in {"{", "["}:
        return "large_json_payloads"
    if "debugrawpayload" in content or "debug raw payload" in lowered:
        return "debug_raw_payloads"
    if "file preview" in lowered:
        return "file_previews"
    if "specification preview" in lowered or "spec preview" in lowered:
        return "specification_previews"
    if "historical observation" in lowered or "historical observations" in lowered:
        return "historical_observations"
    if _has_repeated_lines(content):
        return "repeated_evidence"
    return None


def _compress_content(content: str, *, section: str, max_chars: int) -> tuple[str, list[str]]:
    if section == "large_json_payloads":
        json_compacted, dropped_fields = _compact_json_payload(content, max_chars=max_chars)
        if json_compacted is not None:
            return json_compacted, dropped_fields
    folded = _fold_repeated_lines(content)
    if len(folded) < len(content):
        content = folded
    if len(content) <= max_chars:
        return content, []
    return _truncate_with_notice(content, max_chars=max_chars, section=section), []


def _compact_json_payload(content: str, *, max_chars: int) -> tuple[str, list[str]] | tuple[None, list[str]]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None, []

    dropped_fields: list[str] = []
    compacted = _compact_json_value(parsed, dropped_fields=dropped_fields)
    rendered = json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) > max_chars:
        rendered = _truncate_with_notice(rendered, max_chars=max_chars, section="large_json_payloads")
    return rendered, dropped_fields


def _compact_json_value(value: Any, *, dropped_fields: list[str]) -> Any:
    if isinstance(value, dict):
        compacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in _DROP_FIELD_NAMES:
                dropped_fields.append(key_text)
                continue
            compacted[key_text] = _compact_json_value(item, dropped_fields=dropped_fields)
        return compacted
    if isinstance(value, list):
        if len(value) <= 20:
            return [_compact_json_value(item, dropped_fields=dropped_fields) for item in value]
        head = [_compact_json_value(item, dropped_fields=dropped_fields) for item in value[:10]]
        tail = [_compact_json_value(item, dropped_fields=dropped_fields) for item in value[-5:]]
        return head + [{"__compressed__": f"{len(value) - 15} middle items omitted"}] + tail
    if isinstance(value, str) and len(value) > 1600:
        return _truncate_with_notice(value, max_chars=1600, section="json_string")
    return value


def _has_repeated_lines(content: str) -> bool:
    lines = [line for line in content.splitlines() if line.strip()]
    if len(lines) < 20:
        return False
    return len(set(lines)) <= max(3, len(lines) // 3)


def _fold_repeated_lines(content: str) -> str:
    lines = content.splitlines()
    if not lines:
        return content
    folded: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        repeat_count = 1
        while index + repeat_count < len(lines) and lines[index + repeat_count] == line:
            repeat_count += 1
        if repeat_count >= 4:
            folded.append(line)
            folded.append(f"[compressed: previous line repeated {repeat_count - 1} more times]")
        else:
            folded.extend(lines[index : index + repeat_count])
        index += repeat_count
    result = "\n".join(folded)
    return result if len(result) < len(content) else content


def _truncate_with_notice(content: str, *, max_chars: int, section: str) -> str:
    if max_chars <= 240:
        return f"[compressed {section}: {len(content)} chars omitted]"
    head_len = max(120, int(max_chars * 0.7))
    tail_len = max(80, max_chars - head_len - 120)
    head = content[:head_len].rstrip()
    tail = content[-tail_len:].lstrip() if tail_len > 0 else ""
    omitted = max(0, len(content) - len(head) - len(tail))
    notice = f"\n[compressed {section}: {omitted} chars omitted]\n"
    return f"{head}{notice}{tail}".strip()
