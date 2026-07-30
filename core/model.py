from __future__ import annotations

import os
import threading
import time
import warnings
from inspect import Parameter, signature
from typing import Any, Generator

from core.provider_profiles import combined_stop_sequences, get_provider_profile, profile_metadata

DEFAULT_MODEL_PATH = os.getenv("GEMMA4_MODEL", "mlx-community/gemma-4-e4b-it-4bit")
DEFAULT_MODEL_ID = os.getenv("GEMMA4_API_MODEL_ID", "gemma-4-e4b-it")
DEFAULT_QWEN_MODEL_PATH = os.getenv("QWEN_MODEL", "mlx-community/Qwen3.6-14B-4bit")
DEFAULT_QWEN_MODEL_ID = os.getenv("QWEN_API_MODEL_ID", "qwen-3.6-14b-it")
DEFAULT_BONSAI_MODEL_PATH = os.getenv("BONSAI_MODEL", "prism-ml/Ternary-Bonsai-8B-mlx-2bit")
DEFAULT_BONSAI_MODEL_ID = os.getenv("BONSAI_API_MODEL_ID", "bonsai-8b-2bit")
DEFAULT_DRAFT_MODEL_PATH = os.getenv(
    "GEMMA4_DRAFT_MODEL",
    "mlx-community/gemma-4-E4B-it-assistant-bf16",
)
DEFAULT_DRAFT_KIND = os.getenv("GEMMA4_DRAFT_KIND", "mtp")
DEFAULT_PREFILL_STEP_SIZE = 8192
DEFAULT_CONTEXT_WINDOW = 176000


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_optional_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _env_prefill_step_size() -> int | None:
    raw = os.getenv("LOCAL_LLM_PREFILL_STEP_SIZE") or os.getenv("GEMMA4_PREFILL_STEP_SIZE")
    if raw is None or raw.strip() == "":
        return DEFAULT_PREFILL_STEP_SIZE
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_PREFILL_STEP_SIZE
    return value if value > 0 else None


def _token_count_from_encoded(encoded: Any) -> int | None:
    if encoded is None:
        return None
    if hasattr(encoded, "shape"):
        shape = getattr(encoded, "shape", None)
        if shape:
            return int(shape[-1])
    if hasattr(encoded, "input_ids"):
        return _token_count_from_encoded(encoded.input_ids)
    if isinstance(encoded, dict) and "input_ids" in encoded:
        return _token_count_from_encoded(encoded["input_ids"])
    if isinstance(encoded, list):
        if not encoded:
            return 0
        first = encoded[0]
        if isinstance(first, list):
            return len(first)
        return len(encoded)
    return None


def _context_window_from_config(config: Any) -> int | None:
    if config is None:
        return None
    for attr in ("max_position_embeddings", "max_sequence_length", "seq_length"):
        value = getattr(config, attr, None)
        if isinstance(value, int) and value > 0:
            return value
    text_config = getattr(config, "text_config", None)
    if text_config is not None and text_config is not config:
        value = _context_window_from_config(text_config)
        if value is not None:
            return value
    if isinstance(config, dict):
        for key in ("max_position_embeddings", "max_sequence_length", "seq_length"):
            value = config.get(key)
            if isinstance(value, int) and value > 0:
                return value
        value = _context_window_from_config(config.get("text_config"))
        if value is not None:
            return value
    return None


class MLXModelManager:
    """Thread-safe manager for a single MLX model instance."""

    def __init__(
        self,
        default_model_path: str = DEFAULT_MODEL_PATH,
        model_id: str = DEFAULT_MODEL_ID,
        mtp_enabled: bool | None = None,
        draft_model_path: str = DEFAULT_DRAFT_MODEL_PATH,
        draft_kind: str = DEFAULT_DRAFT_KIND,
        draft_block_size: int | None = None,
        prefill_step_size: int | None = None,
        context_window: int | None = None,
    ) -> None:
        self.default_model_path = default_model_path
        self.model_id = model_id
        self.mtp_enabled = (
            _is_truthy(os.getenv("GEMMA4_MTP_ENABLED")) if mtp_enabled is None else mtp_enabled
        )
        self.draft_model_path = draft_model_path
        self.draft_kind = draft_kind
        self.draft_block_size = draft_block_size or _env_int("GEMMA4_DRAFT_BLOCK_SIZE", 6)
        self.prefill_step_size = (
            _env_prefill_step_size() if prefill_step_size is None else prefill_step_size
        )
        self.configured_context_window = (
            _env_optional_int("LOCAL_LLM_CONTEXT_WINDOW")
            if context_window is None
            else context_window
        )
        self.created = int(time.time())

        self._lock = threading.Lock()
        self._model: Any | None = None
        self._tokenizer: Any | None = None
        self._draft_model: Any | None = None
        self._model_path: str | None = None
        self._draft_model_path: str | None = None
        self._model_context_window: int | None = None
        self._last_generation_stats: dict[str, Any] | None = None

    def validate_model(self, requested_model: str | None) -> str:
        available = self.available_models()

        if not requested_model:
            return self.default_model_path

        if requested_model in available:
            return available[requested_model]

        if requested_model in set(available.values()):
            return requested_model

        raise ValueError(f"Unsupported model: {requested_model}")

    def available_models(self) -> dict[str, str]:
        models: dict[str, str] = {self.model_id: self.default_model_path}
        models[DEFAULT_MODEL_PATH] = DEFAULT_MODEL_PATH

        if DEFAULT_QWEN_MODEL_PATH:
            models[DEFAULT_QWEN_MODEL_ID] = DEFAULT_QWEN_MODEL_PATH
            models[DEFAULT_QWEN_MODEL_PATH] = DEFAULT_QWEN_MODEL_PATH
        if DEFAULT_BONSAI_MODEL_PATH:
            models[DEFAULT_BONSAI_MODEL_ID] = DEFAULT_BONSAI_MODEL_PATH
            models[DEFAULT_BONSAI_MODEL_PATH] = DEFAULT_BONSAI_MODEL_PATH

        # Preserve insertion order while de-duplicating by key.
        deduped: dict[str, str] = {}
        for key, value in models.items():
            if key and value:
                deduped[key] = value
        return deduped

    def ensure_loaded(self, model_path: str | None = None) -> None:
        target_model = model_path or self.default_model_path
        with self._lock:
            if (
                self._model is not None
                and self._tokenizer is not None
                and self._model_path == target_model
                and (
                    not self.mtp_enabled
                    or (
                        self._draft_model is not None
                        and self._draft_model_path == self.draft_model_path
                    )
                )
            ):
                return

            from mlx_vlm.utils import load as load_vlm

            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"At least one mel filter has all zero values\..*",
                    category=UserWarning,
                    module=r"transformers\.audio_utils",
                )
                self._model, self._tokenizer = load_vlm(target_model)
            self._model_context_window = _context_window_from_config(
                getattr(self._model, "config", None)
            ) or _context_window_from_config(
                getattr(getattr(self._model, "language_model", None), "config", None)
            )
            if self.mtp_enabled:
                from mlx_vlm.speculative.drafters import load_drafter

                self._draft_model, self.draft_kind = load_drafter(
                    self.draft_model_path,
                    kind=self.draft_kind,
                )
                self._draft_model_path = self.draft_model_path
            else:
                self._draft_model = None
                self._draft_model_path = None

            self._model_path = target_model

    def context_window(self) -> int:
        configured = self.configured_context_window or DEFAULT_CONTEXT_WINDOW
        return configured

    def count_prompt_tokens(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
    ) -> int:
        target_model = self.validate_model(model)
        self.ensure_loaded(target_model)

        with self._lock:
            if self._model is None or self._tokenizer is None:
                raise RuntimeError("Model is not loaded")
            prompt = self._format_prompt(messages)
            return self._count_prompt_tokens(messages, prompt)

    def last_generation_stats(self) -> dict[str, Any] | None:
        return dict(self._last_generation_stats) if self._last_generation_stats else None

    def health(self) -> dict[str, Any]:
        return {
            "loaded": self._model is not None and self._tokenizer is not None,
            "modelPath": self._model_path,
            "modelId": self.model_id,
            "modelContextWindow": self._model_context_window,
            "configuredContextWindow": self.configured_context_window or DEFAULT_CONTEXT_WINDOW,
            "contextWindow": self.context_window(),
            "mtpEnabled": self.mtp_enabled,
            "draftModelPath": self._draft_model_path,
            "draftLoaded": self._draft_model is not None,
            "draftKind": self.draft_kind if self.mtp_enabled else None,
            "draftBlockSize": self.draft_block_size if self.mtp_enabled else None,
            "prefillStepSize": self.prefill_step_size,
            "providerProfile": profile_metadata(self._model_path or self.default_model_path),
        }

    def _format_prompt(self, messages: list[dict[str, Any]]) -> str:
        if self._tokenizer is None:
            raise RuntimeError("Tokenizer is not loaded")
        return self._apply_chat_template(messages, tokenize=False)

    def _chat_template_kwargs(self) -> dict[str, Any]:
        profile = get_provider_profile(self._model_path or self.default_model_path)
        if profile.name != "qwen" or _is_truthy(os.getenv("LOCAL_LLM_ENABLE_THINKING")):
            return {}
        if not self._tokenizer_supports_kwarg("enable_thinking"):
            return {}
        return {"enable_thinking": False}

    def _tokenizer_supports_kwarg(self, name: str) -> bool:
        if self._tokenizer is None:
            return False
        try:
            parameters = signature(self._tokenizer.apply_chat_template).parameters
        except (TypeError, ValueError):
            return True
        return name in parameters or any(
            parameter.kind == Parameter.VAR_KEYWORD for parameter in parameters.values()
        )

    def _apply_chat_template(self, messages: list[dict[str, Any]], *, tokenize: bool) -> Any:
        if self._tokenizer is None:
            raise RuntimeError("Tokenizer is not loaded")
        return self._tokenizer.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=tokenize,
            **self._chat_template_kwargs(),
        )

    def _count_prompt_tokens(self, messages: list[dict[str, Any]], prompt: str) -> int:
        if self._tokenizer is None:
            raise RuntimeError("Tokenizer is not loaded")
        try:
            tokenized = self._apply_chat_template(messages, tokenize=True)
            count = _token_count_from_encoded(tokenized)
            if count is not None:
                return count
        except Exception:
            pass

        tokenizer = getattr(self._tokenizer, "tokenizer", self._tokenizer)
        encoded = tokenizer(prompt, add_special_tokens=False)
        count = _token_count_from_encoded(encoded)
        if count is None:
            raise RuntimeError("Failed to count prompt tokens")
        return count

    def _assert_context_capacity(self, prompt_tokens: int, max_tokens: int) -> None:
        requested_total = prompt_tokens + max(max_tokens, 0)
        context_window = self.context_window()
        if requested_total <= context_window:
            return
        raise ValueError(
            "context_length_exceeded: "
            f"prompt_tokens={prompt_tokens} max_tokens={max_tokens} "
            f"requested_total_tokens={requested_total} context_window={context_window} "
            f"model_context_window={self._model_context_window}"
        )

    def generate_stream(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        top_p: float | None = None,
        stop: list[str] | None = None,
    ) -> Generator[str, None, None]:
        target_model = self.validate_model(model)
        self.ensure_loaded(target_model)

        with self._lock:
            if self._model is None or self._tokenizer is None:
                raise RuntimeError("Model is not loaded")

            prompt = self._format_prompt(messages)
            prompt_tokens = self._count_prompt_tokens(messages, prompt)
            self._assert_context_capacity(prompt_tokens, max_tokens)
            self._last_generation_stats = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": 0,
                "total_tokens": prompt_tokens,
                "finish_reason": "stop",
            }

            from mlx_vlm.generate import stream_generate as vlm_stream_generate

            generation_kwargs = {
                "max_tokens": max_tokens,
                "temperature": temperature,
                "verbose": False,
            }
            if top_p is not None:
                generation_kwargs["top_p"] = top_p
            if self.prefill_step_size is not None:
                generation_kwargs["prefill_step_size"] = self.prefill_step_size
            if self.mtp_enabled:
                if self._draft_model is None:
                    raise RuntimeError("MTP is enabled but the draft model is not loaded")
                generation_kwargs.update(
                    {
                        "draft_model": self._draft_model,
                        "draft_kind": self.draft_kind,
                        "draft_block_size": self.draft_block_size,
                    }
                )

            def _stream_once(kwargs: dict[str, Any]):
                return vlm_stream_generate(
                    self._model,
                    self._tokenizer,
                    prompt=prompt,
                    **kwargs,
                )

            emitted_any = False
            fallback_used = False
            stopped_by_sequence = False
            stop_sequences = combined_stop_sequences(target_model, stop)

            def _consume(responses):
                nonlocal emitted_any, stopped_by_sequence
                for response in responses:
                    generation_tokens = getattr(response, "generation_tokens", None)
                    total_tokens = getattr(response, "total_tokens", None)
                    prompt_count = getattr(response, "prompt_tokens", prompt_tokens)
                    finish_reason = "stop"
                    if generation_tokens is not None and int(generation_tokens) >= max_tokens:
                        finish_reason = "length"
                    if generation_tokens is not None:
                        self._last_generation_stats = {
                            "prompt_tokens": int(prompt_count),
                            "completion_tokens": int(generation_tokens),
                            "total_tokens": int(
                                total_tokens or (int(prompt_count) + int(generation_tokens))
                            ),
                            "prompt_tps": getattr(response, "prompt_tps", None),
                            "generation_tps": getattr(response, "generation_tps", None),
                            "peak_memory_gb": getattr(response, "peak_memory", None),
                            "finish_reason": finish_reason,
                        }
                    chunk = response.text
                    for stop_sequence in stop_sequences:
                        if stop_sequence and stop_sequence in chunk:
                            chunk = chunk.split(stop_sequence, 1)[0]
                            stopped_by_sequence = True
                            break
                    if chunk:
                        emitted_any = True
                        yield chunk
                    if stopped_by_sequence:
                        self._last_generation_stats = {
                            **(self._last_generation_stats or {}),
                            "finish_reason": "stop",
                        }
                        return

            try:
                yield from _consume(_stream_once(generation_kwargs))
            except TypeError as exc:
                removable_keys = [
                    key for key in ("prefill_step_size", "top_p") if key in generation_kwargs
                ]
                matched_key = next((key for key in removable_keys if key in str(exc)), None)
                if matched_key is None or emitted_any:
                    raise
                fallback_kwargs = dict(generation_kwargs)
                fallback_kwargs.pop(matched_key, None)
                fallback_used = True
                yield from _consume(_stream_once(fallback_kwargs))

            if fallback_used and self._last_generation_stats is None:
                self._last_generation_stats = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": 0,
                    "total_tokens": prompt_tokens,
                    "finish_reason": "stop",
                }
            elif self._last_generation_stats is not None and "finish_reason" not in self._last_generation_stats:
                self._last_generation_stats["finish_reason"] = "stop"

    def list_models(self) -> list[dict[str, Any]]:
        visible_ids = []
        for model_id in [self.model_id, DEFAULT_QWEN_MODEL_ID, DEFAULT_BONSAI_MODEL_ID]:
            if model_id and model_id not in visible_ids:
                visible_ids.append(model_id)

        return [
            {
                "id": model_id,
                "object": "model",
                "created": self.created,
                "owned_by": "local-mlx-mtp" if self.mtp_enabled else "local-mlx",
            }
            for model_id in visible_ids
        ]


_MODEL_MANAGER: MLXModelManager | None = None


def get_model_manager() -> MLXModelManager:
    global _MODEL_MANAGER
    if _MODEL_MANAGER is None:
        _MODEL_MANAGER = MLXModelManager()
    return _MODEL_MANAGER
