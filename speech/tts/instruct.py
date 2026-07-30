from __future__ import annotations

import hashlib

from speech.tts.schemas import STYLE_PRESETS, SpeechRequest, TTSStyle

_STYLE_LABELS = {
    "emotion": "感情",
    "pace": "話速",
    "pitch": "声の高さ",
    "energy": "声の強さ",
    "accent": "アクセント",
    "intonation": "抑揚",
    "pause": "間の取り方",
}


def build_instruct(request: SpeechRequest) -> tuple[str | None, str]:
    parts: list[str] = []
    if request.instructions and request.instructions.strip():
        parts.append(request.instructions.strip())

    preset = STYLE_PRESETS.get(request.qwen.style_preset or "")
    if preset:
        parts.append(_style_to_instruction(preset))
    if request.qwen.style:
        parts.append(_style_to_instruction(request.qwen.style))

    if abs(request.speed - 1.0) >= 0.01:
        parts.append(_speed_instruction(request.speed))

    instruction = " ".join(part for part in parts if part).strip() or None
    digest = hashlib.sha256((instruction or "").encode("utf-8")).hexdigest()[:16]
    return instruction, digest


def _style_to_instruction(style: TTSStyle) -> str:
    values = style.model_dump(exclude_none=True)
    chunks: list[str] = []
    for key, label in _STYLE_LABELS.items():
        value = values.get(key)
        if value:
            chunks.append(f"{label}は{value}")
    if values.get("whisper") is True:
        chunks.append("ささやき声")
    elif values.get("whisper") is False:
        chunks.append("ささやき声にはしない")
    if not chunks:
        return ""
    return "、".join(chunks) + "で話してください。"


def _speed_instruction(speed: float) -> str:
    if speed < 0.6:
        pace = "非常にゆっくり"
    elif speed < 0.85:
        pace = "ゆっくり"
    elif speed < 0.98:
        pace = "少しゆっくり"
    elif speed <= 1.02:
        pace = "自然な速さ"
    elif speed <= 1.2:
        pace = "少し速く"
    elif speed <= 1.6:
        pace = "速く"
    else:
        pace = "非常に速く"
    return f"{pace}、言葉を明瞭に話してください。"
