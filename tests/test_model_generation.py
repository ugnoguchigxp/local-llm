from __future__ import annotations

import sys
import types

from core.model import MLXModelManager
from core.provider_profiles import get_provider_profile


class _FakeTokenizer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def apply_chat_template(self, messages, add_generation_prompt, tokenize, **kwargs):
        self.calls.append(dict(kwargs))
        if tokenize:
            return [1, 2, 3]
        return "fake-prompt"


def _install_fake_stream(monkeypatch, chunks):
    captured_kwargs: dict[str, object] = {}

    def fake_stream_generate(model, tokenizer, prompt, **kwargs):
        captured_kwargs.update(kwargs)
        for index, text in enumerate(chunks, start=1):
            yield types.SimpleNamespace(
                text=text,
                prompt_tokens=3,
                generation_tokens=index,
                total_tokens=3 + index,
            )

    fake_module = types.ModuleType("mlx_vlm.generate")
    fake_module.stream_generate = fake_stream_generate
    monkeypatch.setitem(sys.modules, "mlx_vlm.generate", fake_module)
    return captured_kwargs


def _manager() -> MLXModelManager:
    manager = MLXModelManager(
        default_model_path="mlx-community/gemma-4-e4b-it-4bit",
        model_id="gemma-4-e4b-it",
        mtp_enabled=False,
        prefill_step_size=None,
        context_window=8192,
    )
    manager._model = object()
    manager._tokenizer = _FakeTokenizer()
    manager._model_path = "mlx-community/gemma-4-e4b-it-4bit"
    return manager


def test_provider_profile_matches_model_family():
    assert get_provider_profile("mlx-community/Qwen3.6-14B-4bit").name == "qwen"
    assert get_provider_profile("mlx-community/Ornith-1.0-9B-4bit").name == "qwen"
    assert get_provider_profile("mlx-community/gemma-4-e4b-it-4bit").name == "gemma"
    assert get_provider_profile("prism-ml/Ternary-Bonsai-8B-mlx-2bit").name == "bonsai"


def test_generate_stream_passes_top_p_and_reports_length(monkeypatch):
    captured_kwargs = _install_fake_stream(monkeypatch, ["A", "B"])
    manager = _manager()

    streamed = "".join(
        manager.generate_stream(
            [{"role": "user", "content": "hi"}],
            max_tokens=2,
            temperature=0.0,
            top_p=0.8,
        )
    )

    assert streamed == "AB"
    assert captured_kwargs["top_p"] == 0.8
    assert manager.last_generation_stats()["finish_reason"] == "length"


def test_qwen_chat_template_disables_thinking(monkeypatch):
    _install_fake_stream(monkeypatch, ["ok"])
    tokenizer = _FakeTokenizer()
    manager = MLXModelManager(
        default_model_path="mlx-community/Qwen3.5-9B-4bit",
        model_id="qwen-3.5-9b-4bit",
        mtp_enabled=False,
        prefill_step_size=None,
        context_window=8192,
    )
    manager._model = object()
    manager._tokenizer = tokenizer
    manager._model_path = "mlx-community/Qwen3.5-9B-4bit"

    streamed = "".join(
        manager.generate_stream(
            [{"role": "user", "content": "hi"}],
            max_tokens=4,
            temperature=0.0,
        )
    )

    assert streamed == "ok"
    assert tokenizer.calls
    assert all(call.get("enable_thinking") is False for call in tokenizer.calls)


def test_generate_stream_strips_stop_sequence(monkeypatch):
    _install_fake_stream(monkeypatch, ["helloENDignored"])
    manager = _manager()

    streamed = "".join(
        manager.generate_stream(
            [{"role": "user", "content": "hi"}],
            max_tokens=20,
            stop=["END"],
        )
    )

    assert streamed == "hello"
    assert manager.last_generation_stats()["finish_reason"] == "stop"
