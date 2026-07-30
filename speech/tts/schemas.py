from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TTSStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emotion: str | None = None
    pace: str | None = None
    pitch: str | None = None
    energy: str | None = None
    accent: str | None = None
    intonation: str | None = None
    pause: str | None = None
    whisper: bool | None = None


class QwenTTSOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = "Japanese"
    mode: Literal["custom_voice", "clone", "voice_design"] = "custom_voice"
    style_preset: (
        Literal[
            "calm_narration",
            "friendly_agent",
            "urgent_notice",
        ]
        | None
    ) = None
    style: TTSStyle | None = None
    seed: int | None = None
    streaming_interval: float = Field(default=0.32, ge=0.08, le=2.0)
    temperature: float = Field(default=0.9, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, gt=0.0, le=1.0)
    top_k: int = Field(default=50, ge=1, le=500)


class VoiceReference(BaseModel):
    id: str


class SpeechRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    input: str = Field(min_length=1, max_length=20000)
    voice: str | VoiceReference
    instructions: str | None = Field(default=None, max_length=4000)
    response_format: Literal["mp3", "opus", "aac", "flac", "wav", "pcm"] = "mp3"
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    qwen: QwenTTSOptions = Field(default_factory=QwenTTSOptions)

    @field_validator("input")
    @classmethod
    def nonblank_input(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input must not be blank")
        return value

    @field_validator("model")
    @classmethod
    def nonblank_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model must not be blank")
        return value

    @field_validator("voice")
    @classmethod
    def nonblank_voice(cls, value: str | VoiceReference) -> str | VoiceReference:
        voice_id = value.id if isinstance(value, VoiceReference) else value
        if not voice_id.strip():
            raise ValueError("voice must not be blank")
        return value

    @property
    def voice_id(self) -> str:
        if isinstance(self.voice, VoiceReference):
            return self.voice.id
        return self.voice


class VoiceDesignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    preview_text: str = Field(min_length=1, max_length=1000)
    language: str = "Japanese"
    candidates: int = Field(default=1, ge=1, le=3)
    seed: int | None = None


STYLE_PRESETS: dict[str, TTSStyle] = {
    "calm_narration": TTSStyle(
        emotion="穏やか",
        pace="少しゆっくり",
        pitch="やや低め",
        energy="控えめ",
        intonation="自然",
        pause="句読点で明瞭",
    ),
    "friendly_agent": TTSStyle(
        emotion="親しみやすく明るい",
        pace="自然",
        pitch="標準",
        energy="適度",
        intonation="会話的",
    ),
    "urgent_notice": TTSStyle(
        emotion="緊張感がある",
        pace="やや速い",
        pitch="標準",
        energy="強め",
        intonation="明瞭",
        pause="短め",
    ),
}
