from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")
logger = logging.getLogger(__name__)


@dataclass
class DeferredCleanup:
    callback: Callable[[], Awaitable[None]]
    _deferred: bool = False
    _released: bool = False
    _task: asyncio.Task[None] | None = field(default=None, init=False)

    def defer_until(self, worker: asyncio.Task[object]) -> None:
        if self._released or self._deferred:
            return
        self._deferred = True
        self._task = asyncio.create_task(self._drain_and_release(worker))

    async def release(self) -> None:
        if self._released or self._deferred:
            return
        self._released = True
        await self.callback()

    async def _drain_and_release(self, worker: asyncio.Task[object]) -> None:
        try:
            await worker
        except (Exception, asyncio.CancelledError):
            logger.debug("Deferred inference worker stopped", exc_info=True)
        try:
            if not self._released:
                self._released = True
                await self.callback()
        except Exception:
            logger.exception("Deferred inference cleanup failed")


async def to_thread_cancel_safe(
    function: Callable[P, T],
    *args: P.args,
    deferred_cleanup: DeferredCleanup | None = None,
    **kwargs: P.kwargs,
) -> T:
    """Do not release shared inference state while its worker thread is running."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if deferred_cleanup is not None:
            deferred_cleanup.defer_until(task)
        else:
            asyncio.create_task(_drain(task))
        raise


async def _drain(task: asyncio.Task[object]) -> None:
    try:
        await task
    except (Exception, asyncio.CancelledError):
        logger.debug("Detached inference worker stopped", exc_info=True)
