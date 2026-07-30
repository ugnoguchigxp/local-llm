from __future__ import annotations

import asyncio
import threading

import pytest

from speech.common.async_utils import DeferredCleanup, to_thread_cancel_safe


def test_cancelled_thread_call_defers_resource_release() -> None:
    async def scenario() -> None:
        started = threading.Event()
        unblock = threading.Event()
        cleaned = asyncio.Event()

        def blocking_call() -> None:
            started.set()
            unblock.wait(timeout=5)

        async def cleanup_callback() -> None:
            cleaned.set()

        cleanup = DeferredCleanup(cleanup_callback)
        call = asyncio.create_task(
            to_thread_cancel_safe(
                blocking_call,
                deferred_cleanup=cleanup,
            )
        )
        await asyncio.to_thread(started.wait, 2)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

        await cleanup.release()
        assert not cleaned.is_set()

        unblock.set()
        await asyncio.wait_for(cleaned.wait(), timeout=2)

    asyncio.run(scenario())
