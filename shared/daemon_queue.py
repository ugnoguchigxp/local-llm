"""Small priority queue for single-worker local daemons."""
from __future__ import annotations

import itertools
import queue
import time
from dataclasses import dataclass, field
from threading import Event, Lock, Thread
from typing import Any, Callable, Generic, TypeVar

PRIORITIES = {
    "high": 0,
    "normal": 10,
    "low": 20,
}
PRIORITY_LABELS = {value: key for key, value in PRIORITIES.items()}

PayloadT = TypeVar("PayloadT")
ResultT = TypeVar("ResultT")


@dataclass
class QueueItem(Generic[PayloadT, ResultT]):
    payload: PayloadT
    queued_at: float = field(default_factory=time.perf_counter)
    event: Event = field(default_factory=Event)
    result: ResultT | None = None
    error: str | None = None
    cancelled: bool = False


class SingleWorkerPriorityQueue(Generic[PayloadT, ResultT]):
    """Accept concurrent callers while processing one daemon task at a time."""

    def __init__(
        self,
        name: str,
        handler: Callable[[QueueItem[PayloadT, ResultT]], ResultT],
    ) -> None:
        self.name = name
        self._handler = handler
        self._queue: queue.PriorityQueue[
            tuple[int, int, QueueItem[PayloadT, ResultT] | None]
        ] = queue.PriorityQueue()
        self._sequence = itertools.count()
        self._state_lock = Lock()
        self._in_flight = 0
        self._processed_count = 0
        self._failed_count = 0
        self._cancelled_count = 0
        self._active_priority: int | None = None
        self._worker = Thread(target=self._run_worker, name=f"{name}-worker", daemon=True)
        self._worker.start()

    def submit(
        self,
        payload: PayloadT,
        priority: str = "normal",
        timeout: float | None = None,
    ) -> ResultT:
        if priority not in PRIORITIES:
            raise ValueError("priority must be 'high', 'normal', or 'low'")

        item: QueueItem[PayloadT, ResultT] = QueueItem(payload=payload)
        self._queue.put((PRIORITIES[priority], next(self._sequence), item))

        if not item.event.wait(timeout):
            item.cancelled = True
            raise TimeoutError(f"{self.name} daemon request timed out")
        if item.error:
            raise RuntimeError(item.error)
        if item.result is None:
            raise RuntimeError(f"{self.name} daemon returned no result")
        return item.result

    def health(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "queueSize": self._queue.qsize(),
                "inFlight": self._in_flight,
                "activePriority": (
                    PRIORITY_LABELS.get(self._active_priority)
                    if self._active_priority is not None
                    else None
                ),
                "processedCount": self._processed_count,
                "failedCount": self._failed_count,
                "cancelledCount": self._cancelled_count,
                "workerAlive": self._worker.is_alive(),
            }

    def shutdown(self) -> None:
        self._queue.put((10_000, next(self._sequence), None))
        self._worker.join(timeout=2)

    def _run_worker(self) -> None:
        while True:
            priority, _sequence, item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return

            try:
                if item.cancelled:
                    with self._state_lock:
                        self._cancelled_count += 1
                    continue

                with self._state_lock:
                    self._in_flight += 1
                    self._active_priority = priority

                item.result = self._handler(item)
                with self._state_lock:
                    self._processed_count += 1
            except Exception as exc:  # pragma: no cover - defensive daemon boundary
                item.error = str(exc)
                with self._state_lock:
                    self._failed_count += 1
            finally:
                with self._state_lock:
                    if self._in_flight > 0:
                        self._in_flight -= 1
                    if self._in_flight == 0:
                        self._active_priority = None
                item.event.set()
                self._queue.task_done()
