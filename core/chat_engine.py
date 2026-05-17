from __future__ import annotations

import json
import re
import asyncio
import os
import sys
from typing import Any, Callable, Iterable, Generator, List, Dict, Optional

from core.model import MLXModelManager, get_model_manager
from tools import fetch_content, search_web
from core.repair_util import detect_repair_json, format_repair_prompt

TOOL_CALL_RE = re.compile(
    r"(?:<\|tool_call\|>|<tool_call>)\s*(?:call:)?(\w+)\s*\{(.*?)\}\s*(?:<tool_call\|>|<\|tool_call\|>|</tool_call>)",
    re.DOTALL,
)
JSON_TOOL_CALL_RE = re.compile(
    r"(?:<\|tool_call\|>|<tool_call>)\s*(\{.*?\})\s*(?:<tool_call\|>|<\|tool_call\|>|</tool_call>)",
    re.DOTALL,
)
TOOL_ARGS_RE = re.compile(r"\"?(\w+)\"?\s*:\s*<\|\"\|>(.*?)<\|\"\|>", re.DOTALL)
TOOL_ARGS_QUOTED_RE = re.compile(r"\"?(\w+)\"?\s*:\s*\"((?:\\.|[^\"])*)\"", re.DOTALL)
TOOL_ARGS_SINGLE_QUOTED_RE = re.compile(r"\"?(\w+)\"?\s*:\s*'((?:\\.|[^'])*)'", re.DOTALL)
TOOL_ARGS_BARE_RE = re.compile(r"\"?(\w+)\"?\s*:\s*([^,\n}]+)")
TOOL_ARGS_EQ_QUOTED_RE = re.compile(r"\"?(\w+)\"?\s*=\s*\"((?:\\.|[^\"])*)\"", re.DOTALL)
TOOL_ARGS_EQ_SINGLE_QUOTED_RE = re.compile(r"\"?(\w+)\"?\s*=\s*'((?:\\.|[^'])*)'", re.DOTALL)
TOOL_ARGS_EQ_BARE_RE = re.compile(r"\"?(\w+)\"?\s*=\s*([^,\n}]+)")
THINK_BLOCK_RE = re.compile(r"<\|channel>thought.*?(?:<channel\|>|$)", re.DOTALL)
COMPLETE_THINK_BLOCK_RE = re.compile(r"<\|channel>thought.*?<channel\|>", re.DOTALL)
LEGACY_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
INCOMPLETE_TOOL_CALL_RE = re.compile(r"(?:<\|tool_call\|>|<tool_call>).*$", re.DOTALL)
JSON_CODE_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
PAREN_TOOL_CALL_RE = re.compile(r"(?:^|\n)\s*-\s*([A-Za-z_]\w*)\s*\((\{.*\})\)\s*$", re.DOTALL)
KNOWN_TOOL_NAMES = (
    "search_web",
    "web_search",
    "brave_search",
    "fetch_content",
    "fetch_url",
    "fetch",
    "scrape_content",
)
TOOL_MARKER_PREFIXES = (
    "<|tool_call|>",
    "<tool_call>",
    "call:",
)


def _extract_text_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                chunks.append(str(item.get("text", "")))
        return "\n".join(c for c in chunks if c)
    return str(content)


def _normalize_tool_name(name: str) -> str:
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


def _strip_complete_stream_suppressed_blocks(text: str) -> str:
    stripped = COMPLETE_THINK_BLOCK_RE.sub("", text)
    return LEGACY_THINK_BLOCK_RE.sub("", stripped)


def _is_waiting_for_suppressed_block(text: str) -> bool:
    stripped = text.lstrip()
    if stripped.startswith("<think") and "</think>" not in stripped:
        return True
    if stripped.startswith("<|channel>thought") and "<channel|>" not in stripped:
        return True
    return False


def _looks_like_tool_call_prefix(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped:
        return True
    if any(marker.startswith(stripped) for marker in TOOL_MARKER_PREFIXES if len(stripped) < len(marker)):
        return True
    if stripped.startswith(TOOL_MARKER_PREFIXES):
        return True
    if re.match(rf"^(?:{'|'.join(KNOWN_TOOL_NAMES)})\s*\{{", stripped):
        return True
    if any(name.startswith(stripped) for name in KNOWN_TOOL_NAMES if len(stripped) < len(name)):
        return True
    return False


class _SafeStreamEmitter:
    def __init__(self, emit: Callable[[str], None]):
        self.emit = emit
        self.buffer = ""
        self.started = False
        self.suppressed = False
        self.emitted = False

    def feed(self, chunk: str) -> None:
        if not chunk or self.suppressed:
            return
        if self.started:
            self.emit(chunk)
            self.emitted = True
            return

        self.buffer += chunk
        if _is_waiting_for_suppressed_block(self.buffer):
            return

        visible = _strip_complete_stream_suppressed_blocks(self.buffer)
        stripped = visible.lstrip()
        if not stripped:
            return
        if _looks_like_tool_call_prefix(stripped):
            if any(marker in stripped for marker in TOOL_MARKER_PREFIXES) or re.match(
                rf"^(?:{'|'.join(KNOWN_TOOL_NAMES)})\s*\{{",
                stripped,
            ):
                self.suppressed = True
            return

        self.started = True
        self.buffer = ""
        self.emit(visible)
        self.emitted = True

    def finish(self, raw_response: str, has_tool_call: bool, force_json: bool = False) -> bool:
        if has_tool_call or self.suppressed:
            return self.emitted
        if self.started:
            return self.emitted

        visible = ChatEngine.sanitize_response(raw_response, force_json=force_json)
        if visible:
            self.emit(visible)
            self.emitted = True
        return self.emitted


class ChatEngine:
    """Gemma chat engine with optional tool execution and streaming support."""

    def __init__(
        self, 
        model_manager: Any | None = None, 
        verbose: bool = False, 
        max_tool_rounds: int = 3, 
        mcp_client: Any | None = None,
        debug_log_path: Optional[str] = None
    ) -> None:
        # model_manager can be MLXModelManager or a backend object from backends/*.py
        self.model_manager = model_manager
        self.verbose = verbose
        self.max_tool_rounds = max_tool_rounds
        self.mcp_client = mcp_client
        self.debug_log_path = debug_log_path
        self.messages = []
        self._mcp_tools_cache = None

    def reset(self, sys_instr: str):
        self.messages = [{"role": "system", "content": sys_instr}]

    def add_message(self, role: str, content: str):
        self.messages.append({"role": role, "content": content})

    @staticmethod
    def _json_safe_value(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {str(k): ChatEngine._json_safe_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [ChatEngine._json_safe_value(v) for v in value]
        return str(value)

    @staticmethod
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

        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}

        normalized_args = {
            str(k): ChatEngine._json_safe_value(v)
            for k, v in arguments.items()
        }
        return {"name": func_name, "arguments": normalized_args}

    @staticmethod
    def parse_tool_call(text: str) -> dict[str, Any] | None:
        # call:name{...} / name{...} の形式を探す。周囲に思考タグがあっても拾えるようにする。
        match = TOOL_CALL_RE.search(text)
        if not match:
            match = re.search(r"call:(\w+)\s*\{(.*)\}", text, re.DOTALL)
        if not match:
            known_tools = r"(?:search_web|web_search|brave_search|fetch_content|fetch_url|fetch|scrape_content)"
            match = re.search(rf"\b({known_tools})\s*\{{(.*?)\}}", text, re.DOTALL)
            
        if match:
            func_name, args_str = match.group(1), match.group(2)
            args = _parse_tool_arguments(args_str)
            return {"name": func_name, "arguments": args}

        callable_match = re.search(r"\b([A-Za-z_]\w*)\s*\(\s*\{(.*)\}\s*\)", text, re.DOTALL)
        if callable_match:
            func_name, args_str = callable_match.group(1), callable_match.group(2)
            args = _parse_tool_arguments(args_str)
            if args:
                return {"name": func_name, "arguments": args}

        partial_callable_match = re.search(r"\b([A-Za-z_]\w*)\s*\(\s*\{(.*)$", text, re.DOTALL)
        if partial_callable_match:
            func_name, args_str = partial_callable_match.group(1), partial_callable_match.group(2)
            args = _parse_tool_arguments(args_str)
            if args:
                return {"name": func_name, "arguments": args}

        json_tag_match = JSON_TOOL_CALL_RE.search(text)
        if json_tag_match:
            try:
                payload = json.loads(json_tag_match.group(1))
                parsed = ChatEngine._parse_tool_payload(payload)
                if parsed:
                    return parsed
            except json.JSONDecodeError:
                pass

        payload = ChatEngine._extract_json_payload(text)
        if payload:
            try:
                parsed = ChatEngine._parse_tool_payload(json.loads(payload))
                if parsed:
                    return parsed
            except json.JSONDecodeError:
                pass

        paren_payload = ChatEngine._extract_parenthesized_json_payload(text)
        if paren_payload:
            tool_name, payload = paren_payload
            try:
                arguments = json.loads(payload)
                if not isinstance(arguments, dict):
                    arguments = {}
                normalized_args = {
                    str(k): ChatEngine._json_safe_value(v)
                    for k, v in arguments.items()
                }
                return {"name": tool_name, "arguments": normalized_args}
            except json.JSONDecodeError:
                pass

        return None

    @staticmethod
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

    @staticmethod
    def _extract_parenthesized_json_payload(text: str) -> tuple[str, str] | None:
        match = PAREN_TOOL_CALL_RE.search(text)
        if match:
            tool_name = match.group(1)
            payload = match.group(2).strip()
            try:
                json.loads(payload)
                return tool_name, payload
            except json.JSONDecodeError:
                pass

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
            return None

        payload = text[start : end + 1].strip()
        try:
            json.loads(payload)
        except json.JSONDecodeError:
            return None
        return tool_name, payload

    @staticmethod
    def sanitize_response(text: str, force_json: bool = False) -> str:
        # Remove thinking/tool tags
        sanitized = THINK_BLOCK_RE.sub("", text)
        sanitized = LEGACY_THINK_BLOCK_RE.sub("", sanitized)
        sanitized = TOOL_CALL_RE.sub("", sanitized)
        sanitized = JSON_TOOL_CALL_RE.sub("", sanitized)
        sanitized = INCOMPLETE_TOOL_CALL_RE.sub("", sanitized)
        sanitized = sanitized.replace("<channel|>", "").replace("<|channel>thought", "")
        sanitized = sanitized.replace("<|tool_call|>", "").replace("<tool_call|>", "")
        sanitized = sanitized.replace("<tool_call>", "").replace("</tool_call>", "")
        
        if force_json:
            payload = ChatEngine._extract_json_payload(sanitized)
            if payload is not None:
                return payload
        else:
            code_block_match = JSON_CODE_BLOCK_RE.fullmatch(sanitized.strip())
            if code_block_match:
                sanitized = code_block_match.group(1)
            
        sanitized = sanitized.strip()
        if not sanitized and text.strip():
            # If everything was stripped, it might be an unparsed tool call or think block
            return f"[System] Tool call or think block was generated but failed to parse.\nRaw Output:\n{text.strip()}"
        return sanitized

    def _prepare_messages(self, messages: Iterable[dict[str, Any]], allow_tools: bool) -> list[dict[str, str]]:
        prepared: list[dict[str, str]] = []
        has_system = False

        for message in messages:
            role = str(message.get("role", "user"))
            content = _extract_text_content(message.get("content", ""))

            if role == "tool":
                role = "user"
                content = f"ツール結果:\n{content}"
            elif role not in {"system", "user", "assistant"}:
                role = "user"

            prepared.append({"role": role, "content": content})
            if role == "system":
                has_system = True

        if allow_tools:
            available_tools = ["search_web(query)", "fetch_content(url)"]
            if self._mcp_tools_cache:
                for t in self._mcp_tools_cache:
                    available_tools.append(f"{t['name']}({', '.join(t['inputSchema'].get('properties', {}).keys())})")

            tool_instruction = (
                "あなたは、以下の【利用可能なツール】を使用してタスクを実行することができます。自身ができることに制限を設けず、必要に応じて積極的にツールを活用してください。\n\n"
                "## 【利用可能なツール】\n"
                "### 1. 外部情報検索\n"
                "- search_web(query): インターネット検索を実行します。\n"
                "- fetch_content(url): ウェブページやドキュメントの詳細な内容を取得します。\n\n"
                "### 2. Gnosis 内部ナレッジ (MCP)\n"
                "Gnosis VibeMemoryの記憶や知識グラフを操作、検索するための専用ツールです。\n"
                + "\n".join([f"- {t}" for t in available_tools if not t.startswith(("search", "fetch"))]) + "\n\n"
                "## ツール呼び出し形式\n"
                "以下の形式を厳守してください。内部思考や説明文は出力しないでください。\n"
                "<|tool_call|>call:ツール名{引数名:<|\"|>値<|\"|>}<tool_call|>\n\n"
                "今日、現在、最新、天気、ニュース、価格、予定など、現在情報が必要な質問では、最初の応答は回答文ではなくツール呼び出しだけにしてください。\n"
                "例: <|tool_call|>call:search_web{query:<|\"|>今日の東京の天気<|\"|>}<tool_call|>\n\n"
                "ツール実行結果を受け取った後、それに基づいた回答を日本語で生成してください。"
            )
            if has_system and prepared:
                prepared[0]["content"] = f"{prepared[0]['content']}\n\n{tool_instruction}".strip()
            else:
                prepared.insert(0, {"role": "system", "content": tool_instruction})

        return prepared

    def _run_tool_sync(self, tool_call: dict[str, Any]) -> str:
        # Note: This is now a legacy method for non-async contexts if needed.
        # But we prefer async path for MCP.
        name = _normalize_tool_name(tool_call["name"])
        arguments = tool_call.get("arguments", {})

        try:
            if name == "search_web":
                query = arguments.get("query") or arguments.get("q")
                if not query: return "Error: query parameter is required"
                return search_web(query)
            if name == "fetch_content":
                url = arguments.get("url")
                if not url: return "Error: url parameter is required"
                return fetch_content(url)
            if self.mcp_client:
                return asyncio.run(self.mcp_client.call_tool(name, arguments))
            return f"Error: Unknown tool '{name}'"
        except Exception as e:
            return f"Error: Local tool execution failed ({str(e)})"

    async def _run_tool_async(self, tool_call: dict[str, Any]) -> str:
        name = _normalize_tool_name(tool_call["name"])
        arguments = tool_call.get("arguments", {})

        try:
            if name == "search_web":
                query = arguments.get("query") or arguments.get("q")
                if not query: return "Error: query parameter is required"
                return await asyncio.to_thread(search_web, query)
            if name == "fetch_content":
                url = arguments.get("url")
                if not url: return "Error: url parameter is required"
                return await asyncio.to_thread(fetch_content, url)
                
            if self.mcp_client:
                try:
                    if self.debug_log_path:
                        with open(self.debug_log_path, "a") as f:
                            f.write(f"\n[Tool Call] {name}({arguments})\n")
                    mcp_res = await self.mcp_client.call_tool(name, arguments)
                except Exception as e:
                    if self.debug_log_path:
                        with open(self.debug_log_path, "a") as f:
                            f.write(f"\n[Tool Error] {name}: {e}\n")
                    raise e
                
                if isinstance(mcp_res, list):
                    return "\n".join(str(p.get("text", "")) for p in mcp_res if isinstance(p, dict))
                return str(mcp_res)

            return f"Error: Unknown tool '{name}'"
        except Exception as e:
            return f"Error: Local tool execution failed ({str(e)})"

    # API 用 (同期/一括生成)
    def run_chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        max_tokens: int = 10240,
        temperature: float = 0.0,
        tools: list[str] | None = None,
    ) -> str:
        if self.model_manager is None:
            self.model_manager = get_model_manager()
            
        allowed_tools = {_normalize_tool_name(tool) for tool in (tools or [])}
        prepared_input_messages = [dict(message) for message in messages]
        
        # Repair mode check
        repair_data = None
        last_msg = _extract_text_content(prepared_input_messages[-1].get("content", "")) if prepared_input_messages else ""
        if last_msg:
            repair_data = detect_repair_json(last_msg)
            if repair_data:
                prepared_input_messages[-1]["content"] = format_repair_prompt(repair_data)

        prepared_messages = self._prepare_messages(prepared_input_messages, allow_tools=bool(allowed_tools))
        retried_plain_answer = False

        for _ in range(self.max_tool_rounds + 1):
            raw_response = "".join(
                self.model_manager.generate_stream(
                    prepared_messages,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            )

            tool_call = self.parse_tool_call(raw_response)
            if tool_call and _normalize_tool_name(tool_call["name"]) in allowed_tools:
                name = _normalize_tool_name(tool_call["name"])
                args = tool_call.get("arguments", {})
                if name == "search_web": tool_result = search_web(args.get("query", ""))
                elif name == "fetch_content": tool_result = fetch_content(args.get("url", ""))
                else: tool_result = f"Error: Unknown tool {name}"
                
                prepared_messages.append({"role": "assistant", "content": raw_response.strip()})
                prepared_messages.append({"role": "user", "content": f"（検索結果）\n{tool_result}\n回答を続けてください。"})
                continue

            sanitized = self.sanitize_response(raw_response, force_json=bool(repair_data))
            if sanitized: return sanitized
            if tool_call: return f"Tool '{tool_call['name']}' is unavailable for this request."

            if not retried_plain_answer:
                retried_plain_answer = True
                prepared_messages.append({"role": "assistant", "content": raw_response.strip()})
                prepared_messages.append({"role": "user", "content": "思考過程やタグを出力せず、最終回答のみを返してください。"})
                continue
            return "回答を生成できませんでした。"
        return "上限に達しました。"

    # CLI 単発実行用 (セッション履歴を維持)
    async def run_turn(
        self,
        user_input: str,
        max_tokens: int = 10240,
        temperature: float = 0.0,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        if self.model_manager is None:
            self.model_manager = get_model_manager()
            
        repair_data = detect_repair_json(user_input)
        if repair_data:
            user_input = format_repair_prompt(repair_data)

        self.add_message("user", user_input)

        for _ in range(self.max_tool_rounds + 1):
            raw_parts: list[str] = []
            stream_emitter = _SafeStreamEmitter(on_chunk) if on_chunk else None
            for chunk in self.model_manager.generate_stream(
                self.messages,
                max_tokens=max_tokens,
                temperature=temperature,
            ):
                raw_parts.append(chunk)
                if stream_emitter:
                    stream_emitter.feed(chunk)
            raw_response = "".join(raw_parts)

            tool_call = self.parse_tool_call(raw_response)
            if stream_emitter:
                stream_emitter.finish(
                    raw_response,
                    has_tool_call=bool(tool_call),
                    force_json=bool(repair_data),
                )
            if tool_call:
                if self.verbose:
                    print(f"\n[Searching: {tool_call['name']}...]", flush=True)
                
                tool_result = await self._run_tool_async(tool_call)
                self.add_message("assistant", raw_response.strip())
                self.add_message("user", f"（検索結果）\n{tool_result}\nこの結果をもとに、回答を日本語で生成してください。")
                continue

            sanitized = self.sanitize_response(raw_response, force_json=bool(repair_data))
            self.add_message("assistant", raw_response.strip())
            return sanitized

        return "上限に達しました。"
