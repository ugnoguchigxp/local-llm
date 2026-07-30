from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np

from speech.asr.schemas import VADSettings


@dataclass(frozen=True)
class VADDecision:
    audio: np.ndarray
    started: bool = False
    stopped: bool = False


@dataclass
class EnergyVAD:
    settings: VADSettings
    sample_rate: int = 16000
    speaking: bool = False
    _speech_samples: int = 0
    _silence_samples: int = 0
    _utterance_samples: int = 0
    _pre_roll: deque[np.ndarray] = field(default_factory=deque)
    _pre_roll_samples: int = 0

    def process(self, audio: np.ndarray) -> VADDecision:
        pcm = np.asarray(audio, dtype=np.float32).reshape(-1)
        if pcm.size == 0:
            return VADDecision(pcm)
        if not self.settings.enabled:
            started = not self.speaking
            self.speaking = True
            self._utterance_samples += int(pcm.size)
            return VADDecision(pcm, started=started)

        rms = float(np.sqrt(np.mean(np.square(pcm), dtype=np.float64)))
        is_speech = rms >= self.settings.threshold

        if not self.speaking:
            self._append_pre_roll(pcm)
            if is_speech:
                self._speech_samples += int(pcm.size)
            else:
                self._speech_samples = 0
            required = int(self.sample_rate * self.settings.speech_start_ms / 1000)
            if self._speech_samples >= required:
                self.speaking = True
                combined = (
                    np.concatenate(list(self._pre_roll)) if self._pre_roll else pcm
                )
                self._pre_roll.clear()
                self._pre_roll_samples = 0
                self._utterance_samples = int(combined.size)
                return VADDecision(combined, started=True)
            return VADDecision(np.array([], dtype=np.float32))

        self._utterance_samples += int(pcm.size)
        if is_speech:
            self._silence_samples = 0
        else:
            self._silence_samples += int(pcm.size)

        end_required = int(self.sample_rate * self.settings.speech_end_ms / 1000)
        max_samples = int(self.sample_rate * self.settings.max_utterance_seconds)
        stopped = (
            self._silence_samples >= end_required
            or self._utterance_samples >= max_samples
        )
        if stopped:
            self._reset_speech()
        return VADDecision(pcm, stopped=stopped)

    def reset(self) -> None:
        self.speaking = False
        self._speech_samples = 0
        self._silence_samples = 0
        self._utterance_samples = 0
        self._pre_roll.clear()
        self._pre_roll_samples = 0

    def _reset_speech(self) -> None:
        self.speaking = False
        self._speech_samples = 0
        self._silence_samples = 0
        self._utterance_samples = 0

    def _append_pre_roll(self, pcm: np.ndarray) -> None:
        self._pre_roll.append(pcm.copy())
        self._pre_roll_samples += int(pcm.size)
        limit = int(self.sample_rate * self.settings.pre_roll_ms / 1000)
        while self._pre_roll and self._pre_roll_samples > limit:
            removed = self._pre_roll.popleft()
            self._pre_roll_samples -= int(removed.size)
