from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ResponseFormat = Literal["json", "text", "verbose_json", "srt", "vtt"]


@dataclass(frozen=True)
class ASRResult:
    text: str
    language: str
    segments: list[dict[str, object]]
    duration: float
    words: list[dict[str, object]] = field(default_factory=list)
    finish_reason: str | None = None
    truncated: bool = False


@dataclass(frozen=True)
class StreamUpdate:
    text: str
    language: str
    is_final: bool = False
    segments: list[dict[str, object]] | None = None
    words: list[dict[str, object]] | None = None


class VADSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    threshold: float = Field(default=0.012, ge=0.0001, le=0.5)
    speech_start_ms: int = Field(default=150, ge=20, le=2000)
    speech_end_ms: int = Field(default=650, ge=100, le=5000)
    pre_roll_ms: int = Field(default=250, ge=0, le=2000)
    max_utterance_seconds: int = Field(default=30, ge=1, le=120)


class TranscriptionSessionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str | None = "Japanese"
    prompt: str = Field(default="", max_length=4000)
    input_audio_format: Literal["pcm16"] = "pcm16"
    sample_rate: Literal[16000] = 16000
    timestamp_granularities: list[Literal["segment", "word"]] = Field(
        default_factory=list
    )
    vad: VADSettings = Field(default_factory=VADSettings)


class SessionUpdateEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["transcription_session.update"]
    session: TranscriptionSessionSettings


class AudioAppendEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["input_audio_buffer.append"]
    audio: str


class ControlEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "input_audio_buffer.commit",
        "input_audio_buffer.clear",
        "session.close",
    ]
