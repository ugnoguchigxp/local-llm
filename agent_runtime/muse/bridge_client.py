from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agent_runtime.muse.error_mapping import map_bridge_error


LOGGER = logging.getLogger(__name__)
MAX_FRAME_BYTES = 10 * 1024 * 1024
MAX_PENDING_REQUESTS = 256
MAX_STDERR_CHARS = 8192
MAX_STDERR_LINE_BYTES = 64 * 1024
_SECRET_PATTERN = re.compile(
    r'''(?ix)
    (
        ["']?(?:authorization|api[_-]?key|(?:access|refresh|session|id)?[_-]?token|client[_-]?secret|password|cookie)["']?
        \s*[:=]\s*["']?(?:bearer\s+)?
    )
    ([^"'\s,;}]+)
    '''
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_SECRET_KEY_PATTERN = re.compile(
    r"(?i)(?:authorization|api[_-]?key|(?:access|refresh|session|id)?[_-]?token|client[_-]?secret|password|cookie)"
)


def _redact(text: str) -> str:
    assigned = _SECRET_PATTERN.sub(r"\1[REDACTED]", text)
    return _BEARER_PATTERN.sub("Bearer [REDACTED]", assigned)[:MAX_STDERR_CHARS]


def _redact_value(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, list):
        return [_redact_value(item, depth + 1) for item in value[:1000]]
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if _SECRET_KEY_PATTERN.search(str(key))
                else _redact_value(member, depth + 1)
            )
            for key, member in list(value.items())[:1000]
        }
    return value


class BridgeProtocolError(RuntimeError):
    pass


EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class MuseBridgeClient:
    def __init__(
        self,
        *,
        node_binary: str,
        entrypoint: Path,
        env: dict[str, str],
        request_timeout_ms: int,
        event_callback: EventCallback,
        debug_log: bool = False,
    ) -> None:
        self._node_binary = node_binary
        self._entrypoint = entrypoint
        self._env = env
        self._request_timeout_ms = request_timeout_ms
        self._request_timeout = request_timeout_ms / 1000
        self._event_callback = event_callback
        self._debug_log = debug_log
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._stderr_tail = ""

    @property
    def running(self) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
            and self._reader_task is not None
            and not self._reader_task.done()
            and not self._closed
        )

    async def start(self) -> None:
        if self.running:
            return
        if self._closed:
            raise map_bridge_error("bridge_not_running", "Muse bridge is closed.")
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._node_binary,
                str(self._entrypoint),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
                limit=MAX_FRAME_BYTES + 1,
            )
        except OSError as exc:
            raise map_bridge_error("bridge_not_running", f"Could not start Muse bridge: {exc}") from exc
        self._reader_task = asyncio.create_task(self._read_stdout(), name="muse-bridge-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="muse-bridge-stderr")

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        await self.start()
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise map_bridge_error("bridge_not_running", "Muse bridge is not running.")
        if len(self._pending) >= MAX_PENDING_REQUESTS:
            raise map_bridge_error(
                "backpressured",
                "Muse bridge has reached its pending request limit.",
            )
        request_id = f"brq_{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        frame = {"v": 1, "id": request_id, "method": method, "params": params or {}}
        encoded = (json.dumps(frame, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        if len(encoded) > MAX_FRAME_BYTES:
            self._pending.pop(request_id, None)
            raise map_bridge_error(
                "inputTooLarge",
                "Muse bridge request is too large.",
            )
        try:
            async with self._write_lock:
                process.stdin.write(encoded)
                await process.stdin.drain()
            timeout = (timeout_ms / 1000) if timeout_ms is not None else self._request_timeout
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise map_bridge_error(
                "provider_host_exited",
                f"Muse bridge request {method} timed out.",
                retryable=False,
            ) from exc
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            raise
        except (OSError, ConnectionError, RuntimeError) as exc:
            self._pending.pop(request_id, None)
            raise map_bridge_error("provider_host_exited", "Muse bridge connection closed.") from exc

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while line := await process.stdout.readline():
                if len(line) > MAX_FRAME_BYTES:
                    raise BridgeProtocolError("inbound Muse bridge frame is too large")
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise BridgeProtocolError("Muse bridge emitted invalid JSON") from exc
                if not isinstance(frame, dict) or frame.get("v") != 1:
                    raise BridgeProtocolError("Muse bridge emitted an invalid frame")
                if frame.get("event") is True:
                    await self._event_callback(frame)
                    continue
                request_id = frame.get("id")
                if not isinstance(request_id, str):
                    raise BridgeProtocolError("Muse bridge response has no request id")
                response_result: dict[str, Any] | None = None
                response_error: Exception | None = None
                if frame.get("ok") is True:
                    if not isinstance(frame.get("result"), dict):
                        raise BridgeProtocolError("Muse bridge response has an invalid result")
                    response_result = frame["result"]
                elif frame.get("ok") is False:
                    error = frame.get("error")
                    if not isinstance(error, dict):
                        raise BridgeProtocolError("Muse bridge response has no error")
                    kind = error.get("kind")
                    message = error.get("message")
                    retryable = error.get("retryable", False)
                    data = error.get("data", {})
                    if (
                        not isinstance(kind, str)
                        or not kind
                        or not isinstance(message, str)
                        or not isinstance(retryable, bool)
                        or not isinstance(data, dict)
                    ):
                        raise BridgeProtocolError("Muse bridge response has an invalid error")
                    response_error = map_bridge_error(
                        kind,
                        _redact(message),
                        retryable=retryable,
                        data=_redact_value(data),
                    )
                else:
                    raise BridgeProtocolError("Muse bridge response has an invalid status")
                future = self._pending.pop(request_id, None)
                if future is None or future.done():
                    continue
                if response_error is not None:
                    future.set_exception(response_error)
                elif response_result is not None:
                    future.set_result(response_result)
                else:
                    raise BridgeProtocolError("Muse bridge response could not be resolved")
        except Exception as exc:
            message = str(exc) if isinstance(exc, BridgeProtocolError) else "Muse bridge output failed validation"
            self._fail_pending(map_bridge_error("protocol_error", message))
        finally:
            self._fail_pending(
                map_bridge_error(
                    "bridge_eof",
                    "Muse bridge exited before the request completed.",
                    data={"stderr": self._stderr_tail} if self._debug_log else None,
                )
            )

    async def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        buffer = b""
        discarding_line = False
        while chunk := await process.stderr.read(4096):
            if discarding_line:
                newline = chunk.find(b"\n")
                if newline < 0:
                    continue
                chunk = chunk[newline + 1 :]
                discarding_line = False
            buffer += chunk
            while (newline := buffer.find(b"\n")) >= 0:
                self._record_stderr_line(buffer[: newline + 1])
                buffer = buffer[newline + 1 :]
            if len(buffer) > MAX_STDERR_LINE_BYTES:
                self._stderr_tail = "Muse bridge diagnostic exceeded the safe line limit."
                buffer = b""
                discarding_line = True
        if buffer and not discarding_line:
            self._record_stderr_line(buffer)

    def _record_stderr_line(self, line: bytes) -> None:
        redacted = _redact(line.decode("utf-8", errors="replace"))
        self._stderr_tail = (self._stderr_tail + redacted)[-MAX_STDERR_CHARS:]
        if self._debug_log:
            LOGGER.warning("Muse bridge diagnostic: %s", redacted.rstrip())

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def close(self) -> None:
        if self._closed:
            return
        process = self._process
        if process is None:
            self._closed = True
            return
        if process.returncode is None and self.running:
            try:
                await self.request(
                    "runtime.shutdown",
                    timeout_ms=min(5000, self._request_timeout_ms),
                )
            except Exception:
                pass
        self._closed = True
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=min(5, self._request_timeout))
        except TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=min(2, max(0.5, self._request_timeout)))
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._reader_task, self._stderr_task) if task is not None),
            return_exceptions=True,
        )
        self._fail_pending(map_bridge_error("bridge_eof", "Muse bridge closed."))
