#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from core.chat_engine import ChatEngine, _SafeStreamEmitter, _normalize_tool_name  # noqa: E402
from core.model import MLXModelManager  # noqa: E402
from backends.bonsai import BonsaiBackend  # noqa: E402


def assert_equal(actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def main() -> None:
    emitted = """<think>
ユーザーは現在の天気を知りたいようです。
</think>
<|tool_call|>search_web{query:"今日の東京の天気"}<tool_call|>"""
    parsed = ChatEngine.parse_tool_call(emitted)
    assert_equal(parsed, {"name": "search_web", "arguments": {"query": "今日の東京の天気"}})

    parsed = ChatEngine.parse_tool_call(
        '<|tool_call|>brave_search{query:<|"|>Gemma 4 MTP<|"|>}<tool_call|>'
    )
    assert_equal(parsed, {"name": "brave_search", "arguments": {"query": "Gemma 4 MTP"}})
    assert_equal(_normalize_tool_name(parsed["name"]), "search_web")

    parsed = ChatEngine.parse_tool_call('<tool_call>fetch{url:"https://example.com"}</tool_call>')
    assert_equal(parsed, {"name": "fetch", "arguments": {"url": "https://example.com"}})
    assert_equal(_normalize_tool_name(parsed["name"]), "fetch_content")

    emitted_chunks: list[str] = []
    emitter = _SafeStreamEmitter(emitted_chunks.append)
    for chunk in ["こ", "ん", "に", "ちは"]:
        emitter.feed(chunk)
    assert_equal(emitter.finish("こんにちは", has_tool_call=False), True)
    assert_equal("".join(emitted_chunks), "こんにちは")

    emitted_chunks = []
    emitter = _SafeStreamEmitter(emitted_chunks.append)
    for chunk in ["<think>hidden</think>", '<|tool_call|>search_web{query:"天気"}<tool_call|>']:
        emitter.feed(chunk)
    assert_equal(
        emitter.finish(
            '<think>hidden</think><|tool_call|>search_web{query:"天気"}<tool_call|>',
            has_tool_call=True,
        ),
        False,
    )
    assert_equal("".join(emitted_chunks), "")

    emitted_chunks = []
    emitter = _SafeStreamEmitter(emitted_chunks.append)
    for chunk in ["<think>hidden", "</think>", "最終回答"]:
        emitter.feed(chunk)
    assert_equal(emitter.finish("<think>hidden</think>最終回答", has_tool_call=False), True)
    assert_equal("".join(emitted_chunks), "最終回答")

    class FakeStreamingModel:
        def generate_stream(self, messages, **kwargs):
            yield "逐"
            yield "次"
            yield "表示"

    async def assert_run_turn_streams() -> None:
        engine = ChatEngine(model_manager=FakeStreamingModel())
        engine.reset("system")
        emitted: list[str] = []
        response = await engine.run_turn("hello", max_tokens=8, on_chunk=emitted.append)
        assert_equal(response, "逐次表示")
        assert_equal("".join(emitted), "逐次表示")

    asyncio.run(assert_run_turn_streams())

    bonsai = BonsaiBackend(prefill_step_size=4096)
    assert_equal(bonsai.mtp_enabled, False)
    assert_equal(bonsai.prefill_step_size, 4096)
    assert_equal(BonsaiBackend().prefill_step_size, 8192)

    manager = MLXModelManager(prefill_step_size=4096)
    assert_equal(manager.health()["prefillStepSize"], 4096)
    assert_equal(manager.health()["contextWindow"], 131072)

    class FakeTokenizer:
        def apply_chat_template(self, messages, add_generation_prompt, tokenize):
            if tokenize:
                return [1, 2, 3]
            return "fake-prompt"

    captured_kwargs: dict[str, object] = {}

    def fake_stream_generate(model, tokenizer, prompt, **kwargs):
        captured_kwargs.update(kwargs)
        assert_equal(prompt, "fake-prompt")
        yield types.SimpleNamespace(text="A")
        yield types.SimpleNamespace(text="")
        yield types.SimpleNamespace(text="B")

    fake_module = types.ModuleType("mlx_vlm.generate")
    fake_module.stream_generate = fake_stream_generate
    original_module = sys.modules.get("mlx_vlm.generate")
    sys.modules["mlx_vlm.generate"] = fake_module
    try:
        manager = MLXModelManager(
            default_model_path="fake-model",
            model_id="fake-model",
            mtp_enabled=False,
            prefill_step_size=4096,
            context_window=8192,
        )
        manager._model = object()
        manager._tokenizer = FakeTokenizer()
        manager._model_path = "fake-model"
        streamed = "".join(manager.generate_stream([{"role": "user", "content": "hi"}]))
        assert_equal(streamed, "AB")
        assert_equal(captured_kwargs["prefill_step_size"], 4096)
        assert_equal(manager.last_generation_stats()["prompt_tokens"], 3)

        small_context = MLXModelManager(
            default_model_path="fake-model",
            model_id="fake-model",
            mtp_enabled=False,
            context_window=4,
        )
        small_context._model = object()
        small_context._tokenizer = FakeTokenizer()
        small_context._model_path = "fake-model"
        try:
            list(small_context.generate_stream([{"role": "user", "content": "hi"}], max_tokens=2))
        except ValueError as exc:
            assert "context_length_exceeded" in str(exc)
        else:
            raise AssertionError("expected context_length_exceeded")
    finally:
        if original_module is None:
            sys.modules.pop("mlx_vlm.generate", None)
        else:
            sys.modules["mlx_vlm.generate"] = original_module

    print("tool parsing ok")


if __name__ == "__main__":
    main()
