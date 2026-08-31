from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from speech.common.errors import SpeechAPIError
from speech.common.settings import CommonSettings
from speech.tts.instruct import build_instruct
from speech.tts.schemas import SpeechRequest

PRIMARY_MODEL_ID = "qwen3-tts-0.6b-custom-voice"
CUSTOM_MODEL_NAME = "Qwen3-TTS-12Hz-0.6B-CustomVoice-4bit"
BASE_MODEL_NAME = "Qwen3-TTS-12Hz-1.7B-Base-bf16"
DESIGN_MODEL_NAME = "Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16"

SPEAKER_ALIASES = {
    "ono_anna": "Ono_Anna",
    "ono-anna": "Ono_Anna",
    "vivian": "Vivian",
    "serena": "Serena",
    "uncle_fu": "Uncle_Fu",
    "dylan": "Dylan",
    "eric": "Eric",
    "ryan": "Ryan",
    "aiden": "Aiden",
    "sohee": "Sohee",
}


@dataclass(frozen=True)
class AudioChunk:
    audio: np.ndarray
    sample_rate: int
    is_final: bool = False


class TTSBackend(Protocol):
    ready: bool

    def load(self) -> None: ...

    def warmup(self) -> None: ...

    def close(self) -> None: ...

    def reap_secondary(self, max_idle_seconds: float) -> bool: ...

    def stream(
        self,
        request: SpeechRequest,
        voice_profile: dict[str, object] | None = None,
    ) -> Iterator[AudioChunk]: ...

    def design(
        self,
        *,
        text: str,
        description: str,
        language: str,
        candidates: int,
        seed: int | None,
    ) -> list[AudioChunk]: ...

    def model_info(self) -> dict[str, object]: ...


class FakeTTSBackend:
    def __init__(self, sample_rate: int = 24000) -> None:
        self.sample_rate = sample_rate
        self.ready = False

    def load(self) -> None:
        self.ready = True

    def warmup(self) -> None:
        if not self.ready:
            raise RuntimeError("fake TTS backend is not loaded")

    def close(self) -> None:
        self.ready = False

    def reap_secondary(self, max_idle_seconds: float) -> bool:
        del max_idle_seconds
        return False

    def stream(
        self,
        request: SpeechRequest,
        voice_profile: dict[str, object] | None = None,
    ) -> Iterator[AudioChunk]:
        if not self.ready:
            raise RuntimeError("fake TTS backend is not loaded")
        duration = max(0.35, min(4.0, len(request.input) * 0.045 / request.speed))
        frequency = 220.0 + (sum(request.voice_id.encode("utf-8")) % 120)
        if voice_profile:
            frequency += 35.0
        total = int(self.sample_rate * duration)
        chunk_size = int(self.sample_rate * 0.12)
        phase = 0
        while phase < total:
            size = min(chunk_size, total - phase)
            indices = np.arange(phase, phase + size, dtype=np.float32)
            envelope = np.minimum(
                1.0, np.minimum(indices / 400.0, (total - indices) / 400.0)
            )
            audio = (
                0.12
                * envelope
                * np.sin(2.0 * math.pi * frequency * indices / self.sample_rate)
            )
            phase += size
            yield AudioChunk(
                audio=audio.astype(np.float32),
                sample_rate=self.sample_rate,
                is_final=phase >= total,
            )

    def design(
        self,
        *,
        text: str,
        description: str,
        language: str,
        candidates: int,
        seed: int | None,
    ) -> list[AudioChunk]:
        del seed
        results: list[AudioChunk] = []
        for index in range(candidates):
            request = SpeechRequest(
                model=PRIMARY_MODEL_ID,
                input=text,
                voice=f"design_{index}",
                qwen={
                    "language": language,
                    "mode": "voice_design",
                },
                instructions=description,
                response_format="wav",
            )
            audio = np.concatenate([chunk.audio for chunk in self.stream(request)])
            results.append(AudioChunk(audio, self.sample_rate, is_final=True))
        return results

    def model_info(self) -> dict[str, object]:
        return {
            "id": "qwen3-tts-fake",
            "object": "model",
            "owned_by": "local",
            "backend": "fake",
            "sample_rate": self.sample_rate,
        }


class MLXTTSBackend:
    def __init__(self, settings: CommonSettings) -> None:
        self.settings = settings
        self.ready = False
        self._custom = None
        self._secondary = None
        self._secondary_kind: str | None = None
        self._secondary_last_used = 0.0
        self._lock = threading.RLock()

        model_root = settings.data_root / "models" / "tts"
        self.custom_path = Path(
            os.getenv("QWEN3_TTS_CUSTOM_MODEL", str(model_root / CUSTOM_MODEL_NAME))
        ).expanduser()
        self.base_path = Path(
            os.getenv("QWEN3_TTS_BASE_MODEL", str(model_root / BASE_MODEL_NAME))
        ).expanduser()
        self.design_path = Path(
            os.getenv("QWEN3_TTS_DESIGN_MODEL", str(model_root / DESIGN_MODEL_NAME))
        ).expanduser()

    def load(self) -> None:
        from mlx_audio.tts.utils import load_model

        if not self.custom_path.exists():
            raise FileNotFoundError(
                f"CustomVoice model is missing: {self.custom_path}. "
                "Run scripts/download_speech_models.py first."
            )
        self._custom = load_model(self.custom_path, lazy=False)
        self.ready = True

    def warmup(self) -> None:
        request = SpeechRequest(
            model=PRIMARY_MODEL_ID,
            input="起動確認。",
            voice="ono_anna",
            response_format="pcm",
            qwen={"language": "Japanese", "streaming_interval": 0.08},
        )
        if not any(chunk.audio.size for chunk in self.stream(request)):
            raise RuntimeError("TTS warmup produced no audio")

    def close(self) -> None:
        with self._lock:
            self._custom = None
            self._secondary = None
            self._secondary_kind = None
            self._secondary_last_used = 0.0
            self.ready = False
            try:
                import mlx.core as mx

                mx.clear_cache()
            except Exception:
                pass

    def reap_secondary(self, max_idle_seconds: float) -> bool:
        with self._lock:
            if self._secondary is None:
                return False
            if time.monotonic() - self._secondary_last_used < max_idle_seconds:
                return False
            self._secondary = None
            self._secondary_kind = None
            self._secondary_last_used = 0.0
            try:
                import mlx.core as mx

                mx.clear_cache()
            except Exception:
                pass
            return True

    def stream(
        self,
        request: SpeechRequest,
        voice_profile: dict[str, object] | None = None,
    ) -> Iterator[AudioChunk]:
        if not self.ready or self._custom is None:
            raise RuntimeError("TTS backend is not ready")
        _seed_mlx(request.qwen.seed)
        instruct, _digest = build_instruct(request)
        language = request.qwen.language
        kwargs = {
            "text": request.input,
            "temperature": request.qwen.temperature,
            "top_p": request.qwen.top_p,
            "top_k": request.qwen.top_k,
            "stream": True,
            "streaming_interval": request.qwen.streaming_interval,
            "verbose": False,
        }

        if voice_profile is not None or request.qwen.mode == "clone":
            if voice_profile is None:
                raise SpeechAPIError(
                    400,
                    "clone mode requires a registered voice profile",
                    "voice_profile_required",
                )
            model = self._load_secondary("base")
            iterator = model.generate(
                **kwargs,
                lang_code=language,
                ref_audio=str(voice_profile["audio_path"]),
                ref_text=str(voice_profile["reference_text"]),
                speed=request.speed,
            )
        elif request.qwen.mode == "voice_design":
            model = self._load_secondary("design")
            if not instruct:
                raise SpeechAPIError(
                    400,
                    "voice_design mode requires instructions",
                    "voice_description_required",
                    param="instructions",
                )
            iterator = model.generate(
                **kwargs,
                lang_code=language,
                instruct=instruct,
            )
        else:
            model = self._custom
            speaker = SPEAKER_ALIASES.get(request.voice_id.lower(), request.voice_id)
            supported = []
            if hasattr(model, "get_supported_speakers"):
                supported = list(model.get_supported_speakers())
            supported_by_name = {
                str(candidate).lower(): str(candidate) for candidate in supported
            }
            if supported_by_name and speaker.lower() not in supported_by_name:
                raise SpeechAPIError(
                    400,
                    f"unsupported built-in voice: {request.voice_id}",
                    "voice_not_found",
                    param="voice",
                )
            speaker = supported_by_name.get(speaker.lower(), speaker)
            iterator = model.generate(
                **kwargs,
                voice=speaker,
                lang_code=language,
                instruct=instruct,
                speed=request.speed,
            )

        yield from _mlx_results(iterator)

    def design(
        self,
        *,
        text: str,
        description: str,
        language: str,
        candidates: int,
        seed: int | None,
    ) -> list[AudioChunk]:
        model = self._load_secondary("design")
        generated: list[AudioChunk] = []
        for index in range(candidates):
            _seed_mlx(None if seed is None else seed + index)
            results = model.generate(
                text=text,
                instruct=description,
                lang_code=language,
                stream=False,
                verbose=False,
            )
            chunks = list(_mlx_results(results))
            if not chunks:
                raise RuntimeError("VoiceDesign produced no audio")
            audio = np.concatenate([chunk.audio for chunk in chunks])
            generated.append(AudioChunk(audio, chunks[0].sample_rate, is_final=True))
        return generated

    def model_info(self) -> dict[str, object]:
        sample_rate = getattr(self._custom, "sample_rate", 24000)
        speakers = []
        if self._custom is not None and hasattr(self._custom, "get_supported_speakers"):
            speakers = list(self._custom.get_supported_speakers())
        return {
            "id": PRIMARY_MODEL_ID,
            "object": "model",
            "owned_by": "local",
            "backend": "mlx-audio",
            "path": str(self.custom_path),
            "sample_rate": sample_rate,
            "speakers": speakers,
            "secondary_model": self._secondary_kind,
        }

    def _load_secondary(self, kind: str):
        from mlx_audio.tts.utils import load_model

        with self._lock:
            if self._secondary is not None and self._secondary_kind == kind:
                self._secondary_last_used = time.monotonic()
                return self._secondary
            self._secondary = None
            self._secondary_kind = None
            try:
                import mlx.core as mx

                mx.clear_cache()
            except Exception:
                pass
            path = self.base_path if kind == "base" else self.design_path
            if not path.exists():
                raise SpeechAPIError(
                    503,
                    f"{kind} TTS model is missing: {path}",
                    "secondary_model_missing",
                    "service_unavailable_error",
                )
            self._secondary = load_model(path, lazy=False)
            self._secondary_kind = kind
            self._secondary_last_used = time.monotonic()
            return self._secondary


def create_tts_backend(settings: CommonSettings) -> TTSBackend:
    if settings.fake_backend:
        return FakeTTSBackend()
    return MLXTTSBackend(settings)


def _mlx_results(iterator) -> Iterator[AudioChunk]:
    for result in iterator:
        audio = np.asarray(result.audio, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            continue
        yield AudioChunk(
            audio=audio,
            sample_rate=int(result.sample_rate),
            is_final=bool(getattr(result, "is_final_chunk", False)),
        )


def _seed_mlx(seed: int | None) -> None:
    if seed is None:
        return
    import mlx.core as mx

    mx.random.seed(seed)
