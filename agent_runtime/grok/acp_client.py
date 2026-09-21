from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agent_runtime.errors import AgentRuntimeError
from agent_runtime.grok.error_mapping import (
    host_exited,
    map_rpc_error,
    protocol_error,
    redact_provider_text,
)
from agent_runtime.grok.json_utils import loads_strict


JsonObject = dict[str, Any]
NotificationCallback = Callable[[str, JsonObject], Awaitable[None]]
RequestCallback = Callable[[int | str, str, JsonObject], Awaitable[None]]
MAX_PENDING_REQUESTS = 128
MAX_PROVIDER_REQUEST_ID_BYTES = 1024
MAX_METHOD_BYTES = 256
MAX_STDERR_LINE_BYTES = 64 * 1024


class GrokAcpClient:
    """Small, strict JSON-RPC 2.0 client for Grok's line-delimited ACP transport."""

    def __init__(
        self,
        *,
        command: list[str],
        env: dict[str, str],
        request_timeout_ms: int,
        shutdown_timeout_ms: int,
        max_frame_bytes: int,
        notification_callback: NotificationCallback,
        request_callback: RequestCallback,
        debug_log: bool = False,
    ) -> None:
        if not command or not Path(command[0]).is_absolute():
            raise ValueError("Grok ACP command must use an absolute executable path")
        self._command = list(command)
        self._env = dict(env)
        self._request_timeout_ms = request_timeout_ms
        self._shutdown_timeout_ms = shutdown_timeout_ms
        self._max_frame_bytes = max_frame_bytes
        self._notification_callback = notification_callback
        self._request_callback = request_callback
        self._debug_log = debug_log
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closing = False
        self._stderr_tail: deque[str] = deque(maxlen=20)

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None and not self._closing

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        return tuple(self._stderr_tail)

    async def start(self) -> None:
        if self.running:
            return
        if self._process is not None:
            raise host_exited("The Grok ACP client cannot be restarted after it exits.")
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
                limit=self._max_frame_bytes + 1,
            )
        except OSError as exc:
            raise host_exited(f"The Grok ACP process could not start: {exc}") from exc
        self._reader_task = asyncio.create_task(self._read_loop(), name="grok-acp-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="grok-acp-stderr")

    async def initialize(self, protocol_version: int, timeout_ms: int) -> JsonObject:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": protocol_version,
                "clientCapabilities": {},
                "clientInfo": {
                    "name": "local-llm",
                    "title": "local-llm Agent Gateway",
                    "version": "1",
                },
            },
            timeout_ms=timeout_ms,
        )
        if not isinstance(result, dict):
            raise protocol_error("Grok initialize response must be an object.")
        return result

    async def request(
        self,
        method: str,
        params: JsonObject | None = None,
        *,
        timeout_ms: int | None = None,
    ) -> Any:
        if not self.running:
            raise host_exited()
        if len(self._pending) >= MAX_PENDING_REQUESTS:
            raise AgentRuntimeError(
                code="runtime_overloaded",
                message="The Grok ACP client has too many pending requests.",
                status_code=503,
                runtime="grok",
                retryable=True,
            )
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    **({"params": params} if params is not None else {}),
                }
            )
            timeout = (timeout_ms or self._request_timeout_ms) / 1000
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except TimeoutError as exc:
                raise AgentRuntimeError(
                    code="provider_timeout",
                    message=f"Grok ACP request timed out: {method}",
                    status_code=504,
                    runtime="grok",
                    retryable=True,
                ) from exc
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: JsonObject | None = None) -> None:
        if not self.running:
            raise host_exited()
        await self._write(
            {
                "jsonrpc": "2.0",
                "method": method,
                **({"params": params} if params is not None else {}),
            }
        )

    async def respond(
        self,
        request_id: int | str,
        *,
        result: Any | None = None,
        error: JsonObject | None = None,
    ) -> None:
        if error is not None and result is not None:
            raise ValueError("a JSON-RPC response cannot contain both result and error")
        payload: JsonObject = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        await self._write(payload)

    async def _write(self, message: JsonObject) -> None:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            raise host_exited()
        try:
            encoded = json.dumps(
                message,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise protocol_error("An invalid ACP request was constructed.") from exc
        if len(encoded) > self._max_frame_bytes:
            raise AgentRuntimeError(
                code="provider_request_too_large",
                message="The Grok ACP request exceeded the safe frame size.",
                status_code=413,
                runtime="grok",
            )
        async with self._write_lock:
            try:
                process.stdin.write(encoded)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionError, RuntimeError) as exc:
                raise host_exited() from exc

    async def _read_loop(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        failure: AgentRuntimeError | None = None
        try:
            while True:
                try:
                    line = await process.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError) as exc:
                    failure = AgentRuntimeError(
                        code="provider_response_too_large",
                        message="A Grok ACP frame exceeded the safe size limit.",
                        status_code=502,
                        runtime="grok",
                    )
                    raise failure from exc
                if not line:
                    break
                if len(line) > self._max_frame_bytes:
                    failure = AgentRuntimeError(
                        code="provider_response_too_large",
                        message="A Grok ACP frame exceeded the safe size limit.",
                        status_code=502,
                        runtime="grok",
                    )
                    raise failure
                try:
                    message = loads_strict(line)
                except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                    failure = protocol_error("Grok emitted malformed JSON-RPC.")
                    raise failure from exc
                await self._dispatch(message)
        except asyncio.CancelledError:
            return
        except AgentRuntimeError as exc:
            failure = exc
        except Exception as exc:
            failure = protocol_error(f"Grok ACP reader failed: {type(exc).__name__}.")
        finally:
            if not self._closing:
                await self._stop_process(process)
                self._fail_pending(failure or host_exited())

    async def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise protocol_error("Grok emitted an invalid JSON-RPC envelope.")
        request_id = message.get("id")
        id_present = "id" in message
        has_id = isinstance(request_id, (int, str)) and not isinstance(request_id, bool)
        if id_present and not has_id:
            raise protocol_error("Grok emitted an invalid JSON-RPC id.")
        if isinstance(request_id, int) and not -(2**63) <= request_id <= 2**63 - 1:
            raise protocol_error("Grok emitted a JSON-RPC id outside the int64 range.")
        if (
            isinstance(request_id, str)
            and len(request_id.encode("utf-8")) > MAX_PROVIDER_REQUEST_ID_BYTES
        ):
            raise protocol_error("Grok emitted an oversized JSON-RPC id.")
        method = message.get("method")
        if "method" in message and (not isinstance(method, str) or not method):
            raise protocol_error("Grok emitted an invalid JSON-RPC method.")
        if isinstance(method, str) and len(method.encode("utf-8")) > MAX_METHOD_BYTES:
            raise protocol_error("Grok emitted an oversized JSON-RPC method.")
        if isinstance(method, str) and method:
            if "result" in message or "error" in message:
                raise protocol_error("Grok mixed a JSON-RPC request with a response.")
            params = message.get("params", {})
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise protocol_error("Grok emitted JSON-RPC params that are not an object.")
            if has_id:
                await self._request_callback(request_id, method, params)
            else:
                await self._notification_callback(method, params)
            return
        if not has_id:
            raise protocol_error("Grok emitted JSON-RPC without a method or valid id.")
        if "params" in message:
            raise protocol_error("Grok mixed JSON-RPC response and request parameters.")
        if not isinstance(request_id, int):
            raise protocol_error("Grok responded with an unknown JSON-RPC id type.")
        future = self._pending.get(request_id)
        if future is None or future.done():
            raise protocol_error("Grok emitted a response for an unknown request id.")
        if "error" in message and "result" in message:
            raise protocol_error("Grok JSON-RPC response contains result and error.")
        if "error" in message:
            future.set_exception(map_rpc_error(message["error"]))
        elif "result" in message:
            future.set_result(message["result"])
        else:
            raise protocol_error("Grok JSON-RPC response has neither result nor error.")

    async def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        buffer = b""
        discarding_line = False
        try:
            while True:
                chunk = await process.stderr.read(8192)
                if not chunk:
                    break
                if discarding_line:
                    newline = chunk.find(b"\n")
                    if newline < 0:
                        continue
                    chunk = chunk[newline + 1 :]
                    discarding_line = False
                buffer += chunk
                while (newline := buffer.find(b"\n")) >= 0:
                    line = buffer[:newline]
                    if len(line) > MAX_STDERR_LINE_BYTES:
                        self._stderr_tail.append(
                            "Grok diagnostic exceeded the safe line limit."
                        )
                    else:
                        self._record_stderr_line(line)
                    buffer = buffer[newline + 1 :]
                if len(buffer) > MAX_STDERR_LINE_BYTES:
                    self._stderr_tail.append(
                        "Grok diagnostic exceeded the safe line limit."
                    )
                    buffer = b""
                    discarding_line = True
            if buffer and not discarding_line:
                self._record_stderr_line(buffer)
        except asyncio.CancelledError:
            return

    def _record_stderr_line(self, line: bytes) -> None:
        text = line.decode("utf-8", errors="replace")
        if text:
            self._stderr_tail.append(redact_provider_text(text, limit=2000))

    def _fail_pending(self, exc: AgentRuntimeError) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(exc)

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(
                process.wait(), timeout=self._shutdown_timeout_ms / 1000
            )
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    async def close(self) -> None:
        async with self._close_lock:
            if self._closing:
                return
            self._closing = True
            process = self._process
            self._fail_pending(host_exited("The Grok ACP process was closed."))
            if process is not None and process.stdin is not None:
                process.stdin.close()
            if process is not None:
                await self._stop_process(process)
            current = asyncio.current_task()
            for task in (self._reader_task, self._stderr_task):
                if task is not None and task is not current and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(
                    task
                    for task in (self._reader_task, self._stderr_task)
                    if task is not None and task is not current
                ),
                return_exceptions=True,
            )
