from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from agent_runtime.errors import AgentRuntimeError


@dataclass(frozen=True)
class _StreamOverflow:
    pass


_STREAM_OVERFLOW = _StreamOverflow()


@dataclass(frozen=True)
class _StreamClosed:
    pass


_STREAM_CLOSED = _StreamClosed()


@dataclass(frozen=True)
class AgentEvent:
    cursor: str
    type: str
    session_id: str
    turn_id: str | None
    created_at: int
    data: dict[str, Any]
    native_cursor: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "cursor": self.cursor,
            "type": self.type,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "created_at": self.created_at,
            "data": self.data,
        }


QueueItem = AgentEvent | _StreamOverflow | _StreamClosed


class CursorCodec:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 16:
            raise ValueError("cursor secret must contain at least 16 bytes")
        self._secret = secret

    def encode(self, *, session_id: str, runtime_id: str, native_cursor: str) -> str:
        payload = json.dumps(
            {
                "v": 1,
                "session": session_id,
                "runtime": runtime_id,
                "native": native_cursor,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return f"{_b64(payload)}.{_b64(signature)}"

    def decode(self, token: str, *, session_id: str, runtime_id: str) -> str:
        try:
            payload_text, signature_text = token.split(".", 1)
            payload = _unb64(payload_text)
            signature = _unb64(signature_text)
            expected = hmac.new(self._secret, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature mismatch")
            decoded = json.loads(payload)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AgentRuntimeError(
                code="invalid_event_cursor",
                message="The event cursor is invalid or has been modified.",
                status_code=400,
                retryable=False,
            ) from exc
        if (
            decoded.get("v") != 1
            or decoded.get("session") != session_id
            or decoded.get("runtime") != runtime_id
            or not isinstance(decoded.get("native"), str)
        ):
            raise AgentRuntimeError(
                code="invalid_event_cursor",
                message="The event cursor does not belong to this session.",
                status_code=400,
                retryable=False,
            )
        return decoded["native"]


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class EventBroker:
    def __init__(
        self,
        max_events_per_session: int = 1000,
        queue_size: int = 256,
        max_subscribers_per_session: int = 16,
        max_event_bytes: int = 1024 * 1024,
        max_history_bytes_per_session: int = 16 * 1024 * 1024,
    ) -> None:
        if (
            max_events_per_session < 1
            or queue_size < 1
            or max_subscribers_per_session < 1
            or max_event_bytes < 1
            or max_history_bytes_per_session < 1
        ):
            raise ValueError("event and subscriber limits must be positive")
        self._history: dict[str, deque[AgentEvent]] = defaultdict(deque)
        self._history_sizes: dict[str, deque[int]] = defaultdict(deque)
        self._history_bytes: dict[str, int] = defaultdict(int)
        self._subscribers: dict[str, set[asyncio.Queue[QueueItem]]] = defaultdict(set)
        self._max_events_per_session = max_events_per_session
        self._queue_size = queue_size
        self._max_subscribers_per_session = max_subscribers_per_session
        self._max_event_bytes = max_event_bytes
        self._max_history_bytes_per_session = max_history_bytes_per_session
        self._lock = asyncio.Lock()

    def validate_event(self, event: AgentEvent) -> int:
        try:
            size = len(
                json.dumps(
                    event.public_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise AgentRuntimeError(
                code="runtime_protocol_mismatch",
                message="The provider emitted an invalid event payload.",
                status_code=503,
            ) from exc
        if size > self._max_event_bytes:
            raise AgentRuntimeError(
                code="provider_response_too_large",
                message="A provider event exceeded the safe size limit.",
                status_code=502,
            )
        return size

    async def publish(self, event: AgentEvent) -> None:
        size = self.validate_event(event)
        async with self._lock:
            history = self._history[event.session_id]
            sizes = self._history_sizes[event.session_id]
            history.append(event)
            sizes.append(size)
            self._history_bytes[event.session_id] += size
            while (
                len(history) > self._max_events_per_session
                or self._history_bytes[event.session_id]
                > self._max_history_bytes_per_session
            ):
                history.popleft()
                self._history_bytes[event.session_id] -= sizes.popleft()
            subscribers = list(self._subscribers[event.session_id])
        overflowed: list[asyncio.Queue[QueueItem]] = []
        for queue in subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                queue.put_nowait(_STREAM_OVERFLOW)
                overflowed.append(queue)
            else:
                queue.put_nowait(event)
        for queue in overflowed:
            await self.unsubscribe(event.session_id, queue)

    async def subscribe(
        self,
        session_id: str,
        after_cursor: str | None,
        base_cursor: str | None = None,
    ) -> tuple[list[AgentEvent], asyncio.Queue[QueueItem], bool]:
        queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            if len(self._subscribers.get(session_id, ())) >= self._max_subscribers_per_session:
                raise AgentRuntimeError(
                    code="event_subscriber_limit",
                    message="The session has reached its event subscriber limit.",
                    status_code=429,
                    retryable=True,
                    retry_after=1,
                )
            history = list(self._history[session_id])
            self._subscribers[session_id].add(queue)
        if after_cursor is None:
            return history, queue, True
        if after_cursor == base_cursor:
            return history, queue, True
        for index, event in enumerate(history):
            if event.native_cursor == after_cursor:
                return history[index + 1 :], queue, True
        return [], queue, False

    async def unsubscribe(self, session_id: str, queue: asyncio.Queue[QueueItem]) -> None:
        async with self._lock:
            self._subscribers[session_id].discard(queue)
            if not self._subscribers[session_id]:
                self._subscribers.pop(session_id, None)

    async def iter_live(
        self,
        session_id: str,
        queue: asyncio.Queue[QueueItem],
        heartbeat_seconds: float = 15.0,
    ) -> AsyncIterator[AgentEvent | None]:
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
                    if item is _STREAM_OVERFLOW:
                        raise AgentRuntimeError(
                            code="event_stream_overflow",
                            message="The event subscriber fell behind; reconnect from the last cursor.",
                            status_code=503,
                            retryable=True,
                        )
                    if item is _STREAM_CLOSED:
                        return
                    yield item
                except TimeoutError:
                    yield None
        finally:
            await self.unsubscribe(session_id, queue)

    async def discard_history(self, session_id: str) -> None:
        async with self._lock:
            self._history.pop(session_id, None)
            self._history_sizes.pop(session_id, None)
            self._history_bytes.pop(session_id, None)
            subscribers = list(self._subscribers.pop(session_id, ()))
        for queue in subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(_STREAM_CLOSED)


def now_millis() -> int:
    return int(time.time() * 1000)
