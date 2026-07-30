from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from speech.common.errors import SpeechAPIError


@dataclass
class InferenceGate:
    concurrency: int = 1
    queue_size: int = 8
    timeout_seconds: float = 300.0
    _active: int = 0
    _waiting: int = 0
    _guard: asyncio.Lock = field(default_factory=asyncio.Lock)
    _semaphore: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(max(1, self.concurrency))

    @property
    def active(self) -> int:
        return self._active

    @property
    def waiting(self) -> int:
        return self._waiting

    async def acquire(self) -> None:
        async with self._guard:
            if self._active + self._waiting >= self.concurrency + self.queue_size:
                raise SpeechAPIError(
                    429,
                    "inference queue is full",
                    "queue_full",
                    "rate_limit_error",
                )
            self._waiting += 1

        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self.timeout_seconds,
            )
        except TimeoutError as exc:
            raise SpeechAPIError(
                504,
                "timed out waiting for inference capacity",
                "queue_timeout",
                "timeout_error",
            ) from exc
        finally:
            async with self._guard:
                self._waiting = max(0, self._waiting - 1)

        async with self._guard:
            self._active += 1

    async def release(self) -> None:
        async with self._guard:
            if self._active > 0:
                self._active -= 1
                self._semaphore.release()
