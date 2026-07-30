from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np
import pytest

from speech.common.settings import CommonSettings


@pytest.fixture
def speech_settings(tmp_path: Path) -> CommonSettings:
    return CommonSettings(
        host="127.0.0.1",
        port=0,
        data_root=tmp_path / "speech-data",
        fake_backend=True,
        queue_size=2,
        inference_timeout_seconds=5.0,
        preload=True,
    )


@pytest.fixture
def wav_bytes() -> bytes:
    sample_rate = 16000
    duration = 1.0
    indexes = np.arange(int(sample_rate * duration), dtype=np.float32)
    audio = (0.12 * np.sin(2 * np.pi * 440 * indexes / sample_rate) * 32767).astype(
        "<i2"
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(audio.tobytes())
    return buffer.getvalue()
