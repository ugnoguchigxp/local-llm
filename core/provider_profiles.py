from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    match_terms: tuple[str, ...]
    default_temperature: float = 0.0
    default_top_p: float = 0.95
    thinking_suppression: str = (
        "Do not include hidden reasoning, <think> blocks, thought channels, or special tokens "
        "in the assistant response."
    )
    stop_sequences: tuple[str, ...] = field(default_factory=tuple)
    special_tokens: tuple[str, ...] = field(default_factory=tuple)
    model_status: str = "experimental"

    def matches(self, model: str | None) -> bool:
        candidate = (model or "").lower()
        return any(term in candidate for term in self.match_terms)


GENERIC_PROFILE = ProviderProfile(
    name="generic",
    match_terms=(),
    stop_sequences=("<|im_end|>", "<|endoftext|>", "</s>"),
    special_tokens=("<|im_end|>", "<|endoftext|>", "<|assistant|>", "<|user|>", "<|system|>"),
    model_status="experimental",
)

PROFILES: tuple[ProviderProfile, ...] = (
    ProviderProfile(
        name="qwen",
        match_terms=("qwen", "ornith"),
        thinking_suppression=(
            "Do not use thinking mode. Do not emit <think> blocks, thought channels, or "
            "reasoning text. Return only final answer text or the required function-call JSON."
        ),
        stop_sequences=("<|im_end|>", "<|endoftext|>"),
        special_tokens=(
            "<|im_end|>",
            "<|endoftext|>",
            "<|assistant|>",
            "<|user|>",
            "<|system|>",
            "<|tool|>",
        ),
        model_status="recommended-after-smoke",
    ),
    ProviderProfile(
        name="gemma",
        match_terms=("gemma",),
        stop_sequences=("<end_of_turn>", "<eos>", "</s>"),
        special_tokens=("<end_of_turn>", "<start_of_turn>", "<eos>", "</s>"),
        model_status="recommended-after-smoke",
    ),
    ProviderProfile(
        name="bonsai",
        match_terms=("bonsai",),
        stop_sequences=("<|im_end|>", "<|endoftext|>", "</s>"),
        special_tokens=("<|im_end|>", "<|endoftext|>", "</s>"),
        model_status="experimental",
    ),
)


def get_provider_profile(model: str | None) -> ProviderProfile:
    for profile in PROFILES:
        if profile.matches(model):
            return profile
    return GENERIC_PROFILE


def unique_strings(values: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def combined_stop_sequences(
    model: str | None,
    request_stop: list[str] | None = None,
) -> list[str]:
    profile = get_provider_profile(model)
    return unique_strings([*(request_stop or []), *profile.stop_sequences])


def sanitize_for_profile(text: str, model: str | None = None) -> str:
    profile = get_provider_profile(model)
    sanitized = text
    for token in profile.special_tokens:
        sanitized = sanitized.replace(token, "")
    return sanitized.strip()


def profile_metadata(model: str | None) -> dict[str, Any]:
    profile = get_provider_profile(model)
    return {
        "profile": profile.name,
        "defaultTemperature": profile.default_temperature,
        "defaultTopP": profile.default_top_p,
        "stopSequences": list(profile.stop_sequences),
        "modelStatus": profile.model_status,
    }
