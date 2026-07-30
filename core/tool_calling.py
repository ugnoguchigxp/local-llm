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
TOOL_ARGS_EQ_QUOTED_RE = re.compile(r'"?(\w+)"?\s*=\s*"((?:\\.|[^\"])*)"', re.DOTALL)
TOOL_ARGS_EQ_SINGLE_QUOTED_RE = re.compile(r"\"?(\w+)\"?\s*=\s*'((?:\\.|[^'])*)'", re.DOTALL)
TOOL_ARGS_EQ_BARE_RE = re.compile(r'"?(\w+)"?\s*=\s*([^,\n}]+)')
THINK_BLOCK_RE = re.compile(r"<\|channel>thought.*?(?:<channel\|>|$)", re.DOTALL)
LEGACY_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
INCOMPLETE_TOOL_CALL_RE = re.compile(r"(?:<\|tool_call\|>|<tool_call>).*$", re.DOTALL)
JSON_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
PAREN_TOOL_CALL_RE = re.compile(r"(?:^|\n)\s*-\s*([A-Za-z_]\w*)\s*\((\{.*\})\)\s*$", re.DOTALL)


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
    if not args:
        for arg_match in TOOL_ARGS_EQ_QUOTED_RE.finditer(args_str):
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
        for arg_match in TOOL_ARGS_EQ_SINGLE_QUOTED_RE.finditer(args_str):
            args[arg_match.group(1)] = arg_match.group(2).replace("\\'", "'").replace('\\n', '\n')
    if not args:
        for arg_match in TOOL_ARGS_EQ_BARE_RE.finditer(args_str):
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


def _json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(v) for v in value]
    return str(value)


def _escape_newlines_inside_strings(text: str) -> str:
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                out.append(ch)
                in_string = False
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                out.append("\\r")
                continue
            out.append(ch)
            continue

        out.append(ch)
        if ch == '"':
            in_string = True
            escaped = False

    return "".join(out)


def _auto_close_json_object(text: str) -> str:
    out = text
    in_string = False
    escaped = False
    depth = 0
    for ch in out:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            escaped = False
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
    if in_string:
        out += '"'
    if depth > 0:
        out += "}" * depth
    return out


def _load_json_lenient(text: str) -> Any | None:
    candidates = [text, text.replace("“", '"').replace("”", '"').replace("’", "'")]
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        escaped = _escape_newlines_inside_strings(candidate)
        try:
            return json.loads(escaped)
        except json.JSONDecodeError:
            pass

        closed = _auto_close_json_object(escaped)
        try:
            return json.loads(closed)
        except json.JSONDecodeError:
            pass
    return None


def _normalize_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, str):
        decoded = _load_json_lenient(arguments)
        if isinstance(decoded, dict):
            arguments = decoded
        else:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    return {str(k): _json_safe_value(v) for k, v in arguments.items()}


def _parse_tool_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None

    func_name = payload.get("name") or payload.get("tool_name") or payload.get("function_name")
    arguments: Any = payload.get("arguments")
    if arguments is None:
        arguments = payload.get("args")
    if arguments is None:
        arguments = payload.get("params")
    if arguments is None:
        arguments = payload.get("parameters")

    if not isinstance(func_name, str) and isinstance(payload.get("function"), dict):
        function = payload["function"]
        func_name = function.get("name") or function.get("tool_name") or function.get("function_name")
        arguments = function.get("arguments", arguments)
        if arguments is None:
            arguments = function.get("args")
        if arguments is None:
            arguments = function.get("params")
        if arguments is None:
            arguments = function.get("parameters")

    if not isinstance(func_name, str) and isinstance(payload.get("tool"), dict):
        tool = payload["tool"]
        func_name = tool.get("name") or tool.get("tool_name") or tool.get("function_name")
        arguments = tool.get("arguments", arguments)
        if arguments is None:
            arguments = tool.get("args")
        if arguments is None:
            arguments = tool.get("params")
        if arguments is None:
            arguments = tool.get("parameters")

    if not isinstance(func_name, str) or not func_name:
        return None

    normalized_args = _normalize_arguments(arguments)
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


def _extract_parenthesized_json_payload(text: str) -> tuple[str, str] | None:
    match = PAREN_TOOL_CALL_RE.search(text)
    if match:
        tool_name = match.group(1)
        payload = match.group(2).strip()
        decoded = _load_json_lenient(payload)
        if isinstance(decoded, dict):
            return tool_name, json.dumps(decoded, ensure_ascii=False)

    line_match = re.search(r"-\s*([A-Za-z_]\w*)\s*\(", text)
    if not line_match:
        return None

    tool_name = line_match.group(1)
    start = text.find("{", line_match.start())
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    end = -1
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = idx
                break
    if end == -1:
        payload = text[start:].strip()
        decoded = _load_json_lenient(payload)
        if isinstance(decoded, dict):
            return tool_name, json.dumps(decoded, ensure_ascii=False)
        return None

    payload = text[start : end + 1].strip()
    decoded = _load_json_lenient(payload)
    if not isinstance(decoded, dict):
        return None
    return tool_name, json.dumps(decoded, ensure_ascii=False)


def parse_tool_call(text: str, allowed_tool_names: set[str] | None = None) -> dict[str, Any] | None:
    text = THINK_BLOCK_RE.sub("", text)
    text = LEGACY_THINK_BLOCK_RE.sub("", text)
    match = TOOL_CALL_RE.search(text)
    if not match:
        match = re.search(r"call:(\w+)\s*\{(.*)\}", text, re.DOTALL)
    if match:
        func_name, args_str = match.group(1), match.group(2)
        parsed = {"name": func_name, "arguments": _parse_tool_arguments(args_str)}
        if allowed_tool_names is None or normalize_tool_name(parsed["name"]) in allowed_tool_names:
            return parsed

    callable_match = re.search(r"\b([A-Za-z_]\w*)\s*\(\s*\{(.*)\}\s*\)", text, re.DOTALL)
    if callable_match:
        func_name, args_str = callable_match.group(1), callable_match.group(2)
        parsed = {"name": func_name, "arguments": _parse_tool_arguments(args_str)}
        if parsed["arguments"] and (
            allowed_tool_names is None or normalize_tool_name(parsed["name"]) in allowed_tool_names
        ):
            return parsed

    # Fallback for truncated tool calls like: edit_file({ path='x', mode='w',
    partial_callable_match = re.search(r"\b([A-Za-z_]\w*)\s*\(\s*\{(.*)$", text, re.DOTALL)
    if partial_callable_match:
        func_name, args_str = partial_callable_match.group(1), partial_callable_match.group(2)
        parsed = {"name": func_name, "arguments": _parse_tool_arguments(args_str)}
        if parsed["arguments"] and (
            allowed_tool_names is None or normalize_tool_name(parsed["name"]) in allowed_tool_names
        ):
            return parsed

    json_tag_match = JSON_TOOL_CALL_RE.search(text)
    if json_tag_match:
        decoded = _load_json_lenient(json_tag_match.group(1))
        parsed = _parse_tool_payload(decoded)
        if parsed and (allowed_tool_names is None or normalize_tool_name(parsed["name"]) in allowed_tool_names):
            return parsed

    payload = _extract_json_payload(text)
    if payload:
        decoded = _load_json_lenient(payload)
        parsed = _parse_tool_payload(decoded)
        if parsed and (allowed_tool_names is None or normalize_tool_name(parsed["name"]) in allowed_tool_names):
            return parsed

    paren_payload = _extract_parenthesized_json_payload(text)
    if paren_payload:
        tool_name, payload = paren_payload
        decoded = _load_json_lenient(payload)
        if isinstance(decoded, dict):
            parsed = {"name": tool_name, "arguments": _normalize_arguments(decoded)}
            if allowed_tool_names is None or normalize_tool_name(parsed["name"]) in allowed_tool_names:
                return parsed

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
