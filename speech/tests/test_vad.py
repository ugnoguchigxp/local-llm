from __future__ import annotations

import numpy as np

from speech.asr.schemas import VADSettings
from speech.asr.vad import EnergyVAD


def test_vad_starts_and_stops() -> None:
    settings = VADSettings(
        threshold=0.01,
        speech_start_ms=100,
        speech_end_ms=200,
        pre_roll_ms=100,
    )
    vad = EnergyVAD(settings)
    speech = np.ones(3200, dtype=np.float32) * 0.1
    silence = np.zeros(4000, dtype=np.float32)

    started = vad.process(speech)
    stopped = vad.process(silence)

    assert started.started is True
    assert started.audio.size > 0
    assert stopped.stopped is True
