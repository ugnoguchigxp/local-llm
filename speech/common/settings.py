from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_ROOT = (
    Path.home() / "Library" / "Application Support" / "local-llm" / "speech"
)


def truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class CommonSettings:
    host: str
    port: int
    data_root: Path
    fake_backend: bool
    queue_size: int
    inference_timeout_seconds: float
    preload: bool

    def ensure_directories(self) -> None:
        for path in (
            self.data_root,
            self.data_root / "models",
            self.data_root / "voices",
            self.data_root / "cache",
            self.data_root / "tmp",
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                path.chmod(0o700)
            except OSError:
                pass


def data_root() -> Path:
    raw = os.getenv("SPEECH_DATA_ROOT", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_DATA_ROOT


def tts_settings() -> CommonSettings:
    return CommonSettings(
        host=os.getenv("QWEN3_TTS_HOST", "127.0.0.1"),
        port=int(os.getenv("QWEN3_TTS_PORT", "44520")),
        data_root=data_root(),
        fake_backend=truthy(os.getenv("SPEECH_FAKE_BACKEND"), default=False),
        queue_size=max(0, int(os.getenv("QWEN3_TTS_QUEUE_SIZE", "8"))),
        inference_timeout_seconds=float(os.getenv("QWEN3_TTS_TIMEOUT_SECONDS", "300")),
        preload=truthy(os.getenv("QWEN3_TTS_PRELOAD"), default=True),
    )


def asr_settings() -> CommonSettings:
    return CommonSettings(
        host=os.getenv("QWEN3_ASR_HOST", "127.0.0.1"),
        port=int(os.getenv("QWEN3_ASR_PORT", "44521")),
        data_root=data_root(),
        fake_backend=truthy(os.getenv("SPEECH_FAKE_BACKEND"), default=False),
        queue_size=max(0, int(os.getenv("QWEN3_ASR_QUEUE_SIZE", "8"))),
        inference_timeout_seconds=float(os.getenv("QWEN3_ASR_TIMEOUT_SECONDS", "300")),
        preload=truthy(os.getenv("QWEN3_ASR_PRELOAD"), default=True),
    )
