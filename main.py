#!/usr/bin/env python3
import warnings
import os
import json
import re
from datetime import datetime, timezone
from pathlib import Path
import uuid

# 全ての警告を抑制（特に multiprocessing の resource_tracker 対策）
warnings.filterwarnings("ignore")
os.environ["PYTHONWARNINGS"] = "ignore"

import argparse
import sys
import asyncio
from core.chat_engine import ChatEngine
from vibe_mcp.client import VibeMcpClient


SESSION_ID_RE = r"^[A-Za-z0-9_-]{6,64}$"


class FileSessionStore:
    def __init__(self, session_dir: str | None = None):
        default_dir = Path.home() / ".localLlm" / "sessions"
        self.session_dir = Path(session_dir) if session_dir else default_dir
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        return self.session_dir / f"{session_id}.json"

    def load(self, session_id: str) -> dict | None:
        path = self._session_path(session_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def save(
        self,
        session_id: str,
        messages: list[dict],
        backend: str,
        model: str,
        system_instruction: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        path = self._session_path(session_id)
        existing = None
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                existing = json.load(f)

        payload = {
            "session_id": session_id,
            "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now,
            "backend": backend,
            "model": model,
            "system_instruction": system_instruction,
            "messages": messages,
        }
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def _generate_session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:12]}"


def _validate_session_id(session_id: str) -> bool:
    return re.match(SESSION_ID_RE, session_id) is not None

def _is_truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_prefill_step_size() -> int:
    return _env_int("LOCAL_LLM_PREFILL_STEP_SIZE", _env_int("GEMMA4_PREFILL_STEP_SIZE", 8192))


def _normalize_optional_positive_int(value: int) -> int | None:
    return value if value > 0 else None


def _format_mcp_tool_catalog(tools: list[dict] | None) -> str:
    if not tools:
        return ""

    lines: list[str] = []
    for tool in tools[:80]:
        name = str(tool.get("name", "")).strip()
        if not name:
            continue
        schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        args = ", ".join(properties.keys()) if isinstance(properties, dict) else ""
        description = str(tool.get("description", "") or "").replace("\n", " ").strip()
        if len(description) > 140:
            description = f"{description[:137]}..."
        lines.append(f"- {name}({args}): {description}")

    if len(tools) > len(lines):
        lines.append(f"- ... and {len(tools) - len(lines)} more tools")

    return "\n".join(lines)

async def main():
    parser = argparse.ArgumentParser(description="Multi-Backend AI Chat Agent (Local Direct Tooling)")
    parser.add_argument("--backend", choices=["mlx", "qwen", "ollama", "bonsai", "mock"], default="mlx", help="Inference backend")
    parser.add_argument("--model", type=str, help="Model path or name")
    parser.add_argument("--max-tokens", type=int, default=10240)
    parser.add_argument("--temp", type=float, default=0.0)
    parser.add_argument("--verbose", "-v", action="store_true", help="Display debug logs and raw model output")
    parser.add_argument("prompt", nargs="?", help="Single-turn prompt (non-interactive mode)")
    parser.add_argument("--prompt", dest="prompt_opt", type=str, help="Single-turn prompt (non-interactive mode)")
    parser.add_argument("--session-id", type=str, help="Session ID to resume/save chat history")
    parser.add_argument("--session-dir", type=str, help="Directory to store session files")
    parser.add_argument("--no-session", action="store_true", help="Disable session persistence in single-turn mode")
    parser.add_argument("--no-mcp", action="store_true", help="Disable MCP server connections")
    parser.add_argument("--output", choices=["json", "text"], default="json", help="Output format in single-turn mode")
    parser.add_argument("--root", type=str, help="Project root directory for MCP tools")
    parser.add_argument("--mtp", action="store_true", help="Enable Gemma 4 MTP speculative decoding")
    parser.add_argument("--no-mtp", action="store_true", help="Disable Gemma 4 MTP even when GEMMA4_MTP_ENABLED is set")
    parser.add_argument(
        "--draft-model",
        type=str,
        default=os.getenv("GEMMA4_DRAFT_MODEL", "mlx-community/gemma-4-E4B-it-assistant-bf16"),
        help="Gemma 4 MTP assistant/drafter model",
    )
    parser.add_argument(
        "--draft-kind",
        type=str,
        default=os.getenv("GEMMA4_DRAFT_KIND", "mtp"),
        help="Speculative decoding drafter kind",
    )
    parser.add_argument(
        "--draft-block-size",
        type=int,
        default=_env_int("GEMMA4_DRAFT_BLOCK_SIZE", 6),
        help="Number of MTP draft tokens per verification block",
    )
    parser.add_argument(
        "--prefill-step-size",
        type=int,
        default=_env_prefill_step_size(),
        help="Prompt prefill chunk size for MLX-family backends; use 0 to disable chunked prefill",
    )
    parser.add_argument("--thinking", action="store_true", default=_is_truthy_env("LOCAL_LLM_THINKING"), help="Enable thinking mode (allow <think> tags or internal reasoning)")
    args = parser.parse_args()

    # NOTE:
    # In seatbelt/headless sandboxed sessions, MLX may abort the process during
    # Metal device discovery before Python can catch exceptions
    # (see ml-explore/mlx#3148). To avoid repeated crash dialogs and make
    # failure recoverable for callers, block MLX backends by default in that context.
    if (
        args.backend in {"mlx", "bonsai"}
        and os.getenv("CODEX_SANDBOX") == "seatbelt"
        and not _is_truthy_env("LOCAL_LLM_ALLOW_MLX_IN_SEATBELT")
    ):
        print(
            "[Startup Error] MLX backends are disabled in CODEX_SANDBOX=seatbelt to avoid"
            " native aborts during Metal initialization."
            " Use --backend ollama or set LOCAL_LLM_ALLOW_MLX_IN_SEATBELT=1 to override.",
            file=sys.stderr,
        )
        sys.exit(78)

    # バックエンドの動的インポート
    if args.backend in {"mlx", "qwen"}:
        from backends.mlx import MLXBackend
        mtp_enabled = (
            args.backend == "mlx"
            and not args.no_mtp
            and (args.mtp or _is_truthy_env("GEMMA4_MTP_ENABLED"))
        )
        backend = MLXBackend(
            verbose=args.verbose,
            mtp_enabled=mtp_enabled,
            draft_model_path=args.draft_model,
            draft_kind=args.draft_kind,
            draft_block_size=args.draft_block_size,
            prefill_step_size=_normalize_optional_positive_int(args.prefill_step_size),
        )
        if args.backend == "qwen":
            model_path = args.model or os.getenv("QWEN_MODEL", "mlx-community/Qwen3.6-14B-4bit")
        elif args.backend == "mlx":
            model_path = args.model or "mlx-community/gemma-4-e4b-it-4bit"
    elif args.backend == "ollama":
        from backends.ollama import OllamaBackend
        backend = OllamaBackend(verbose=args.verbose)
        model_path = args.model or "llama3"
    elif args.backend == "bonsai":
        from backends.bonsai import BonsaiBackend
        backend = BonsaiBackend(
            verbose=args.verbose,
            prefill_step_size=_normalize_optional_positive_int(args.prefill_step_size),
        )
        model_path = args.model or os.getenv("BONSAI_MODEL", "prism-ml/Ternary-Bonsai-8B-mlx-2bit")
    elif args.backend == "mock":
        from backends.mock_backend import MockBackend
        backend = MockBackend(verbose=args.verbose)
        model_path = args.model or "test-mock-model"

    # モデルのロード
    try:
        if args.verbose:
            print(f"[Debug] Loading backend: {args.backend} with model: {model_path}", file=sys.stderr)
        backend.load_model(model_path)
    except Exception as e:
        print(f"Failed to load model: {e}")
        sys.exit(1)

    # MCP クライアントの初期化 (Gnosis TS Server へのブリッジ)
    mcp_client = None
    root_dir = os.path.abspath(args.root) if args.root else os.getcwd()
    
    # ログ出力先は local-llm リポジトリ配下に固定（Gnosis 配下へは書き込まない）
    configured_debug_dir = os.getenv("LOCAL_LLM_DEBUG_DIR", "").strip()
    if configured_debug_dir:
        debug_log_dir = os.path.abspath(configured_debug_dir)
    else:
        debug_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".debug")
    os.makedirs(debug_log_dir, exist_ok=True)
    debug_log_path = os.path.join(debug_log_dir, "mcp_debug.log")

    if not args.no_mcp:
        try:
            mcp_client = VibeMcpClient(root_dir)
            if args.verbose:
                print(f"[Debug] Starting MCP bridge to {root_dir}...", file=sys.stderr)
            await mcp_client.start()
            if args.verbose and hasattr(mcp_client, "get_startup_errors"):
                startup_errors = mcp_client.get_startup_errors()
                if startup_errors:
                    print(
                        f"[Warning] Some MCP servers failed to start: {startup_errors}",
                        file=sys.stderr,
                    )
        except Exception as e:
            if args.verbose:
                print(f"[Warning] Failed to start MCP bridge: {e}", file=sys.stderr)
            mcp_client = None

    # Engineの初期化
    engine = ChatEngine(
        backend, 
        verbose=args.verbose, 
        mcp_client=mcp_client, 
        debug_log_path=debug_log_path
    )
    
    # MCPツールの取得とキャッシュ
    if mcp_client:
        try:
            engine._mcp_tools_cache = await mcp_client.list_tools()
            with open(debug_log_path, "w") as f:
                f.write(f"Linked {len(engine._mcp_tools_cache)} MCP tools: {[t['name'] for t in engine._mcp_tools_cache]}\n")
            if args.verbose:
                print(f"[Debug] Linked {len(engine._mcp_tools_cache)} MCP tools.", file=sys.stderr)
        except Exception as e:
            if args.verbose:
                print(f"[Warning] Failed to list MCP tools: {e}", file=sys.stderr)
    
    # バックエンドに応じたコンテキスト設定
    current_date = datetime.now().strftime("%Y年%m月%d日")
    mcp_tool_catalog = _format_mcp_tool_catalog(getattr(engine, "_mcp_tools_cache", None))

    if args.backend == "bonsai":
        sys_instr = (
            f"あなたは有能な助手です。本日は {current_date} です。必ず日本語で答えてください。\n"
            "【最短最速で回答せよ】情報の不足がある場合は、前置きや解説（「調べます」「わかりません」等）を一切行わず、即座にツールを呼び出してください。\n"
            "検索が必要な場合は、第一声で必ず以下のタグを出力してください。\n\n"
            "- 検索: <|tool_call|>call:search_web{query:\"検索ワード\"}<tool_call|>\n\n"
            "例: 「今日の東京の天気」→ 即座に以下を出力:\n"
            "<|tool_call|>call:search_web{query:\"今日の東京の天気\"}<tool_call|>"

        )
    elif args.backend in {"mlx", "qwen"}:
        think_instr = "" if args.thinking else "思考過程や <think> タグは出力しないでください。\n\n"
        sys_instr = (
            f"あなたは有能なアシスタントです。本日は {current_date} です。日本語で回答してください。\n"
            f"{think_instr}"
            "【ツール呼び出し形式】\n"
            "必ず以下の形式を使用してください。引数は JSON 形式で、文字列はダブルクォートで囲んでください。\n"
            "<|tool_call|>call:tool_name{arg_name:\"value\"}<tool_call|>\n\n"
            "【基本ツール】\n"
            "1. search_web(query): ウェブ検索。例: <|tool_call|>call:search_web{query:\"検索語\"}<tool_call|>\n"
            "2. fetch_content(url): サイト内容取得。例: <|tool_call|>call:fetch_content{url:\"URL\"}<tool_call|>\n\n"

        )
    else:
        sys_instr = (
            "優秀なAI助手。日本語で答えて。<|tool_call|>形式で検索ツールを使用可能。"

        )

    prompt = args.prompt_opt if args.prompt_opt is not None else args.prompt
    use_single_turn = bool(prompt)
    session_store: FileSessionStore | None = None

    session_id: str | None = args.session_id
    session_created = False
    if session_id:
        if not _validate_session_id(session_id):
            print("Invalid --session-id. Use 6-64 chars: A-Z a-z 0-9 _ -")
            sys.exit(2)
    elif use_single_turn and not args.no_session:
        session_id = _generate_session_id()
        session_created = True

    if session_id and not args.no_session:
        session_store = FileSessionStore(args.session_dir)
        record = session_store.load(session_id)
        if record and isinstance(record.get("messages"), list):
            engine.messages = record["messages"]
            if args.verbose:
                print(f"[Debug] Loaded session: {session_id} ({len(engine.messages)} messages)", file=sys.stderr)
        else:
            engine.reset(sys_instr)
    else:
        engine.reset(sys_instr)

    if use_single_turn:
        response_text = await engine.run_turn(
            prompt,
            max_tokens=args.max_tokens,
            temperature=args.temp,
        )
        with open(debug_log_path, "a") as f:
            f.write(f"\n--- TURN ---\nRaw Response: {response_text}\n")

        if session_store and session_id and not args.no_session:
            session_store.save(
                session_id=session_id,
                messages=engine.messages,
                backend=args.backend,
                model=model_path,
                system_instruction=sys_instr,
            )

        if args.output == "json":
            result = {
                "session_id": session_id,
                "session_created": session_created,
                "backend": args.backend,
                "model": model_path,
                "message_count": len(engine.messages),
                "response": response_text,
            }
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(response_text)
        if mcp_client:
            if args.verbose:
                print("[Debug] Stopping MCP bridge...", file=sys.stderr)
            await mcp_client.stop()
        return

    backend_labels = {
        "mlx": "Gemma 4",
        "qwen": "Qwen 3.6",
        "bonsai": "Bonsai",
        "ollama": "Ollama",
        "mock": "Mock",
    }
    model_label = f"{backend_labels.get(args.backend, args.backend)} ({model_path})"
    print(f"\n=== Chat session started ===")
    print(f"  Model   : {model_label}")
    if session_id:
        print(f"  Session : {session_id}")
    print(f"  Commands: exit · reset · Ctrl+C to quit\n")

    # メインループ
    try:
        while True:
            try:
                loop = asyncio.get_event_loop()
                u_inp = await loop.run_in_executor(None, lambda: input("You: "))
                u_inp = u_inp.strip()
            except EOFError:
                break
            
            if not u_inp: continue
            if u_inp.lower() == "exit": break
            if u_inp.lower() == "reset":
                engine.reset(sys_instr)
                if session_store and session_id and not args.no_session:
                    session_store.save(
                        session_id=session_id,
                        messages=engine.messages,
                        backend=args.backend,
                        model=model_path,
                        system_instruction=sys_instr,
                    )
                print("Chat history reset.")
                continue

            streamed_response = False

            def emit_chunk(chunk: str) -> None:
                nonlocal streamed_response
                if not chunk:
                    return
                sys.stdout.write(chunk)
                sys.stdout.flush()
                streamed_response = True

            response_text = await engine.run_turn(
                u_inp,
                max_tokens=args.max_tokens,
                temperature=args.temp,
                on_chunk=emit_chunk,
            )
            if streamed_response:
                print()
            else:
                print(response_text)
            with open(debug_log_path, "a") as f:
                f.write(f"\n--- TURN ---\nRaw Response: {response_text}\n")
            if session_store and session_id and not args.no_session:
                session_store.save(
                    session_id=session_id,
                    messages=engine.messages,
                    backend=args.backend,
                    model=model_path,
                    system_instruction=sys_instr,
                )
            
    finally:
        # 終了時にリソースを適切に解放
        if mcp_client:
            if args.verbose:
                print("[Debug] Stopping MCP bridge...", file=sys.stderr)
            await mcp_client.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBye.")
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)
    except Exception as e:
        print(f"\n[Fatal Error] {e}")
        sys.exit(1)
