from __future__ import annotations

import json
import re
from typing import Any

TOOL_CALL_RE = re.compile(
    r"(?:<\|tool_call\|>|<tool_call>)\s*(?:call:)?(\w+)\s*\{(.*?)\}\s*(?:<tool_call\|>|<\|tool_call\|>|</tool_call>)",
    re.DOTALL,
)
JSON_TOOL_CALL_RE = re.compile(
    r"(?:<\|tool_call\|>|<tool_call>)\s*(\{.*?\})\s*(?:<tool_call\|>|<\|tool_call\|>|</tool_call>)",
    re.DOTALL,
)
TOOL_ARGS_RE = re.compile(r'"?(\w+)"?\s*:\s*<\|"\|>(.*?)<\|"\|>', re.DOTALL)
TOOL_ARGS_QUOTED_RE = re.compile(r'"?(\w+)"?\s*:\s*"((?:\\.|[^\"])*)"', re.DOTALL)
TOOL_ARGS_SINGLE_QUOTED_RE = re.compile(r"\"?(\w+)\"?\s*:\s*'((?:\\.|[^'])*)'", re.DOTALL)
TOOL_ARGS_BARE_RE = re.compile(r'"?(\w+)"?\s*:\s*([^,\n}]+)')
THINK_BLOCK_RE = re.compile(r"<\|channel>thought.*?(?:<channel\|>|$)", re.DOTALL)
LEGACY_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
INCOMPLETE_TOOL_CALL_RE = re.compile(r"(?:<\|tool_call\|>|<tool_call>).*$", re.DOTALL)
JSON_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def normalize_tool_name(name: str) -> str:
    aliases = {
        "brave_search": "search_web",
        "web_search": "search_web",
        "search_web": "search_web",
        "fetch": "fetch_content",
        "scrape_content": "fetch_content",
        "fetch_url": "fetch_content",
        "fetch_content": "fetch_content",
    }
    return aliases.get(name, name)


def _parse_tool_arguments(args_str: str) -> dict[str, str]:
    args: dict[str, str] = {}

    for arg_match in TOOL_ARGS_RE.finditer(args_str):
        args[arg_match.group(1)] = arg_match.group(2)
    if not args:
        for arg_match in TOOL_ARGS_QUOTED_RE.finditer(args_str):
            val = arg_match.group(2)
            try:
                args[arg_match.group(1)] = json.loads(f'"{val}"')
            except Exception:
                args[arg_match.group(1)] = (
                    val.replace('\\"', '"')
                    .replace('\\n', '\n')
                    .replace('\\t', '\t')
                    .replace('\\\\', '\\')
                )
    if not args:
        for arg_match in TOOL_ARGS_SINGLE_QUOTED_RE.finditer(args_str):
            args[arg_match.group(1)] = arg_match.group(2).replace("\\'", "'").replace('\\n', '\n')
    if not args:
        for arg_match in TOOL_ARGS_BARE_RE.finditer(args_str):
            args[arg_match.group(1)] = arg_match.group(2).strip()
    if not args and args_str.strip():
        try:
            candidate = "{" + args_str + "}"
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                args = {str(k): str(v) for k, v in parsed.items()}
        except json.JSONDecodeError:
            pass

    return args


def _parse_tool_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None

    func_name = payload.get("name")
    arguments: Any = payload.get("arguments", {})

    if not isinstance(func_name, str) and isinstance(payload.get("function"), dict):
        function = payload["function"]
        func_name = function.get("name")
        arguments = function.get("arguments", arguments)

    if not isinstance(func_name, str) or not func_name:
        return None

    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}

    normalized_args = {str(k): str(v) for k, v in arguments.items()}
    return {"name": func_name, "arguments": normalized_args}


def _extract_json_payload(text: str) -> str | None:
    code_block_match = JSON_CODE_BLOCK_RE.search(text)
    if code_block_match:
        candidate = code_block_match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1].strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    return None


def parse_tool_call(text: str, allowed_tool_names: set[str] | None = None) -> dict[str, Any] | None:
    match = TOOL_CALL_RE.search(text)
    if not match:
        match = re.search(r"call:(\w+)\s*\{(.*)\}", text, re.DOTALL)
    if match:
        func_name, args_str = match.group(1), match.group(2)
        parsed = {"name": func_name, "arguments": _parse_tool_arguments(args_str)}
        if allowed_tool_names is None or normalize_tool_name(parsed["name"]) in allowed_tool_names:
            return parsed

    json_tag_match = JSON_TOOL_CALL_RE.search(text)
    if json_tag_match:
        try:
            parsed = _parse_tool_payload(json.loads(json_tag_match.group(1)))
            if parsed and (allowed_tool_names is None or normalize_tool_name(parsed["name"]) in allowed_tool_names):
                return parsed
        except json.JSONDecodeError:
            pass

    payload = _extract_json_payload(text)
    if payload:
        try:
            parsed = _parse_tool_payload(json.loads(payload))
            if parsed and (allowed_tool_names is None or normalize_tool_name(parsed["name"]) in allowed_tool_names):
                return parsed
        except json.JSONDecodeError:
            pass

    return None


def sanitize_assistant_text(text: str) -> str:
    sanitized = THINK_BLOCK_RE.sub("", text)
    sanitized = LEGACY_THINK_BLOCK_RE.sub("", sanitized)
    sanitized = TOOL_CALL_RE.sub("", sanitized)
    sanitized = JSON_TOOL_CALL_RE.sub("", sanitized)
    sanitized = INCOMPLETE_TOOL_CALL_RE.sub("", sanitized)
    sanitized = sanitized.replace("<channel|>", "").replace("<|channel>thought", "")
    sanitized = sanitized.replace("<|tool_call|>", "").replace("<tool_call|>", "")
    sanitized = sanitized.replace("<tool_call>", "").replace("</tool_call>", "")

    code_block_match = JSON_CODE_BLOCK_RE.fullmatch(sanitized.strip())
    if code_block_match:
        sanitized = code_block_match.group(1)

    return sanitized.strip()
