from __future__ import annotations

import os
import threading
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import numpy as np

from speech.asr.schemas import ASRResult, StreamUpdate
from speech.common.errors import SpeechAPIError
from speech.common.settings import CommonSettings

ASR_MODEL_NAME = "Qwen3-ASR-1.7B-8bit"
ALIGNER_MODEL_NAME = "Qwen3-ForcedAligner-0.6B"


class ASRStream(Protocol):
    def feed(self, pcm: np.ndarray) -> StreamUpdate: ...

    def finish(self) -> StreamUpdate: ...


class ASRBackend(Protocol):
    ready: bool

    def load(self) -> None: ...

    def warmup(self) -> None: ...

    def close(self) -> None: ...

    def transcribe(
        self,
        audio_path: Path | np.ndarray,
        *,
        language: str | None,
        prompt: str,
        timestamps: bool,
    ) -> ASRResult: ...

    def stream_file(
        self,
        audio_path: Path,
        *,
        language: str | None,
        prompt: str,
        timestamps: bool = False,
    ) -> Iterator[StreamUpdate]: ...

    def start_stream(
        self,
        *,
        language: str | None,
        prompt: str,
    ) -> ASRStream: ...

    def model_info(self) -> dict[str, object]: ...


class FakeASRStream:
    def __init__(self, language: str | None) -> None:
        self.language = language or "Japanese"
        self.samples = 0
        self.text = ""

    def feed(self, pcm: np.ndarray) -> StreamUpdate:
        self.samples += int(np.asarray(pcm).size)
        seconds = self.samples / 16000.0
        if seconds >= 1.5:
            self.text = "音声認識のストリーミング結果です。"
        elif seconds >= 0.5:
            self.text = "音声認識の"
        return StreamUpdate(self.text, self.language)

    def finish(self) -> StreamUpdate:
        if not self.text:
            self.text = "音声認識結果です。"
        return StreamUpdate(self.text, self.language, is_final=True)


class FakeASRBackend:
    def __init__(self) -> None:
        self.ready = False

    def load(self) -> None:
        self.ready = True

    def warmup(self) -> None:
        if not self.ready:
            raise RuntimeError("fake ASR backend is not loaded")

    def close(self) -> None:
        self.ready = False

    def transcribe(
        self,
        audio_path: Path | np.ndarray,
        *,
        language: str | None,
        prompt: str,
        timestamps: bool,
    ) -> ASRResult:
        duration = (
            np.asarray(audio_path).size / 16000.0
            if isinstance(audio_path, np.ndarray)
            else _wav_duration(audio_path) or 1.0
        )
        text = "これはQwen3音声認識のテストです。"
        if prompt:
            text = f"{text} {prompt.split()[0]}"
        words = []
        if timestamps:
            words = [
                {"text": "これは", "start": 0.0, "end": min(0.4, duration)},
                {"text": "テストです", "start": min(0.4, duration), "end": duration},
            ]
        return ASRResult(
            text=text,
            language=language or "Japanese",
            segments=[
                {
                    "text": text,
                    "start": 0.0,
                    "end": duration,
                    "chunk_index": 0,
                }
            ],
            duration=duration,
            words=words,
            finish_reason="eos",
        )

    def stream_file(
        self,
        audio_path: Path,
        *,
        language: str | None,
        prompt: str,
        timestamps: bool = False,
    ) -> Iterator[StreamUpdate]:
        yield StreamUpdate("これは", language or "Japanese")
        yield StreamUpdate("これはQwen3音声認識の", language or "Japanese")
        final = self.transcribe(
            audio_path,
            language=language,
            prompt=prompt,
            timestamps=timestamps,
        )
        yield StreamUpdate(
            final.text,
            final.language,
            is_final=True,
            segments=final.segments if timestamps else None,
            words=final.words if timestamps else None,
        )

    def start_stream(
        self,
        *,
        language: str | None,
        prompt: str,
    ) -> ASRStream:
        return FakeASRStream(language)

    def model_info(self) -> dict[str, object]:
        return {
            "id": "qwen3-asr-fake",
            "object": "model",
            "owned_by": "local",
            "backend": "fake",
        }


class MLXASRStream:
    def __init__(self, session, state, lock: threading.RLock) -> None:
        self.session = session
        self.state = state
        self.lock = lock

    def feed(self, pcm: np.ndarray) -> StreamUpdate:
        with self.lock:
            self.state = self.session.feed_audio(pcm, self.state)
            return StreamUpdate(self.state.text, self.state.language)

    def finish(self) -> StreamUpdate:
        with self.lock:
            self.state = self.session.finish_streaming(self.state)
            return StreamUpdate(
                self.state.text,
                self.state.language,
                is_final=True,
            )


class MLXASRBackend:
    def __init__(self, settings: CommonSettings) -> None:
        self.settings = settings
        self.ready = False
        self._session = None
        self._lock = threading.RLock()
        model_root = settings.data_root / "models"
        self.model_path = Path(
            os.getenv(
                "QWEN3_ASR_MODEL",
                str(model_root / "asr" / ASR_MODEL_NAME),
            )
        ).expanduser()
        self.aligner_path = Path(
            os.getenv(
                "QWEN3_ALIGNER_MODEL",
                str(model_root / "aligner" / ALIGNER_MODEL_NAME),
            )
        ).expanduser()

    def load(self) -> None:
        from mlx_qwen3_asr import Session

        if not self.model_path.exists():
            raise FileNotFoundError(
                f"ASR model is missing: {self.model_path}. "
                "Run scripts/download_speech_models.py first."
            )
        self._session = Session(model=str(self.model_path))
        self.ready = True

    def warmup(self) -> None:
        if not self.ready or self._session is None:
            raise RuntimeError("ASR backend is not ready")
        with self._lock:
            self._session.transcribe(
                np.zeros(8000, dtype=np.float32),
                language="Japanese",
                return_chunks=False,
                verbose=False,
            )

    def close(self) -> None:
        with self._lock:
            self._session = None
            self.ready = False
            try:
                import mlx.core as mx

                mx.clear_cache()
            except Exception:
                pass

    def transcribe(
        self,
        audio_path: Path | np.ndarray,
        *,
        language: str | None,
        prompt: str,
        timestamps: bool,
    ) -> ASRResult:
        if not self.ready or self._session is None:
            raise RuntimeError("ASR backend is not ready")
        forced_aligner = None
        if timestamps:
            if not self.aligner_path.exists():
                raise SpeechAPIError(
                    503,
                    f"ForcedAligner model is missing: {self.aligner_path}",
                    "aligner_model_missing",
                    "service_unavailable_error",
                )
            forced_aligner = str(self.aligner_path)
        try:
            with self._lock:
                result = self._session.transcribe(
                    audio_path,
                    context=prompt,
                    language=language,
                    return_timestamps=timestamps,
                    return_chunks=True,
                    forced_aligner=forced_aligner,
                    verbose=False,
                )
        finally:
            if timestamps:
                _clear_mlx_cache()
        words = list(result.segments or [])
        segments = list(result.chunks or [])
        duration = _segments_duration(words, segments)
        if isinstance(audio_path, np.ndarray):
            duration = int(audio_path.size) / 16000.0
        elif duration <= 0:
            duration = _wav_duration(audio_path)
        return ASRResult(
            text=result.text,
            language=result.language,
            segments=segments,
            duration=duration,
            words=words,
            finish_reason=result.finish_reason,
            truncated=result.truncated,
        )

    def stream_file(
        self,
        audio_path: Path,
        *,
        language: str | None,
        prompt: str,
        timestamps: bool = False,
    ) -> Iterator[StreamUpdate]:
        if not self.ready or self._session is None:
            raise RuntimeError("ASR backend is not ready")
        from mlx_qwen3_asr.audio import load_audio_np

        audio = load_audio_np(audio_path)
        state = self._session.init_streaming(
            context=prompt,
            language=language,
            chunk_size_sec=2.0,
            max_context_sec=max(30.0, len(audio) / 16000.0 + 1.0),
            finalization_mode="latency",
            endpointing_mode="energy",
        )
        previous = ""
        for start in range(0, len(audio), 16000):
            with self._lock:
                state = self._session.feed_audio(audio[start : start + 16000], state)
            if state.text != previous:
                previous = state.text
                yield StreamUpdate(state.text, state.language)
        with self._lock:
            state = self._session.finish_streaming(state)
        if _accurate_sse_final_enabled():
            final = self.transcribe(
                audio_path,
                language=language,
                prompt=prompt,
                timestamps=timestamps,
            )
            yield StreamUpdate(
                final.text,
                final.language,
                is_final=True,
                segments=final.segments if timestamps else None,
                words=final.words if timestamps else None,
            )
        else:
            yield StreamUpdate(state.text, state.language, is_final=True)

    def start_stream(
        self,
        *,
        language: str | None,
        prompt: str,
    ) -> ASRStream:
        if not self.ready or self._session is None:
            raise RuntimeError("ASR backend is not ready")
        state = self._session.init_streaming(
            context=prompt,
            language=language,
            chunk_size_sec=1.0,
            max_context_sec=30.0,
            finalization_mode="latency",
            endpointing_mode="energy",
        )
        return MLXASRStream(self._session, state, self._lock)

    def model_info(self) -> dict[str, object]:
        info = self._session.model_info if self._session is not None else {}
        return {
            "id": "qwen3-asr-1.7b",
            "object": "model",
            "owned_by": "local",
            "backend": "mlx-qwen3-asr",
            "path": str(self.model_path),
            **info,
        }


def create_asr_backend(settings: CommonSettings) -> ASRBackend:
    if settings.fake_backend:
        return FakeASRBackend()
    return MLXASRBackend(settings)


def _wav_duration(path: Path) -> float:
    if path.suffix.lower() != ".wav":
        return 0.0
    try:
        with wave.open(str(path), "rb") as handle:
            return handle.getnframes() / max(1, handle.getframerate())
    except (OSError, wave.Error):
        return 0.0


def _segments_duration(
    segments: list[dict] | None,
    chunks: list[dict] | None,
) -> float:
    values = segments or chunks or []
    return max((float(item.get("end", 0.0)) for item in values), default=0.0)


def _accurate_sse_final_enabled() -> bool:
    return os.getenv("QWEN3_ASR_SSE_ACCURATE_FINAL", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _clear_mlx_cache() -> None:
    try:
        import mlx.core as mx

        mx.clear_cache()
    except Exception:
        pass
