from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from agent_runtime.errors import AgentRuntimeError
from agent_runtime.grok.acp_client import MAX_PENDING_REQUESTS, GrokAcpClient


FIXTURE = Path(__file__).parent / "fixtures" / "grok_acp" / "fake_agent.py"


async def _client(notifications, requests):
    async def notification(method, params):
        notifications.append((method, params))

    client = None

    async def request(request_id, method, params):
        requests.append((request_id, method, params))
        assert client is not None
        await client.respond(
            request_id,
            result={"outcome": {"outcome": "cancelled"}},
        )

    client = GrokAcpClient(
        command=[str(Path(sys.executable).resolve()), str(FIXTURE)],
        env={"PATH": "/usr/bin:/bin"},
        request_timeout_ms=2000,
        shutdown_timeout_ms=1000,
        max_frame_bytes=64 * 1024,
        notification_callback=notification,
        request_callback=request,
    )
    await client.start()
    return client


def test_request_notification_and_agent_initiated_request():
    asyncio.run(_request_notification_and_agent_initiated_request())


async def _request_notification_and_agent_initiated_request():
    notifications = []
    requests = []
    client = await _client(notifications, requests)
    try:
        initialized = await client.initialize(1, 2000)
        echoed = await client.request("echo", {"safe": True})
        permission = await client.request("permission/test")
        await client.request("notification/test")
        await asyncio.sleep(0)
        assert initialized == {"protocolVersion": 1}
        assert echoed == {"safe": True}
        assert permission == {"outcome": {"outcome": "cancelled"}}
        assert requests[0][1] == "session/request_permission"
        assert notifications[0][0] == "session/update"
    finally:
        await client.close()


@pytest.mark.parametrize("method", ["malformed", "duplicate", "exit", "oversized"])
def test_malformed_frame_and_host_exit_fail_pending_request(method):
    async def run():
        client = await _client([], [])
        try:
            with pytest.raises(AgentRuntimeError) as raised:
                await client.request(method)
            assert raised.value.code in {
                "runtime_protocol_mismatch",
                "provider_host_exited",
                "provider_response_too_large",
            }
            assert client.running is False
        finally:
            await client.close()

    asyncio.run(run())


def test_request_timeout_does_not_leave_a_pending_future():
    async def run():
        client = await _client([], [])
        try:
            with pytest.raises(AgentRuntimeError) as raised:
                await client.request("hang", timeout_ms=10)
            assert raised.value.code == "provider_timeout"
            assert client._pending == {}
        finally:
            await client.close()

    asyncio.run(run())


def test_pending_request_limit_fails_closed():
    async def run():
        client = await _client([], [])
        loop = asyncio.get_running_loop()
        try:
            for request_id in range(MAX_PENDING_REQUESTS):
                client._pending[request_id] = loop.create_future()
            with pytest.raises(AgentRuntimeError) as raised:
                await client.request("echo")
            assert raised.value.code == "runtime_overloaded"
        finally:
            for future in client._pending.values():
                future.cancel()
            client._pending.clear()
            await client.close()

    asyncio.run(run())


def test_outbound_frame_limit_is_enforced():
    async def run():
        notifications = []
        requests = []
        client = await _client(notifications, requests)
        client._max_frame_bytes = 64
        try:
            with pytest.raises(AgentRuntimeError) as raised:
                await client.request("echo", {"value": "x" * 1000})
            assert raised.value.code == "provider_request_too_large"
        finally:
            await client.close()

    asyncio.run(run())


def test_stderr_tail_is_bounded_and_redacted():
    async def run():
        client = await _client([], [])
        try:
            await client.request("stderr")
            await asyncio.sleep(0)
            assert client.stderr_tail == ("diagnostic Bearer [redacted]",)
        finally:
            await client.close()

    asyncio.run(run())


def test_oversized_stderr_line_is_discarded():
    async def run():
        client = await _client([], [])
        try:
            await client.request("stderr_long")
            for _attempt in range(20):
                if client.stderr_tail:
                    break
                await asyncio.sleep(0.01)
            assert client.stderr_tail == (
                "Grok diagnostic exceeded the safe line limit.",
            )
        finally:
            await client.close()

    asyncio.run(run())


def test_concurrent_close_waits_for_the_same_shutdown():
    async def run():
        client = await _client([], [])
        await asyncio.gather(client.close(), client.close())
        assert client.running is False
        assert client._process is not None and client._process.returncode is not None

    asyncio.run(run())


def test_invalid_json_rpc_envelopes_are_rejected():
    async def run():
        client = await _client([], [])
        try:
            with pytest.raises(AgentRuntimeError, match="invalid JSON-RPC id"):
                await client._dispatch(
                    {"jsonrpc": "2.0", "id": True, "method": "session/update"}
                )
            with pytest.raises(AgentRuntimeError, match="mixed"):
                await client._dispatch(
                    {
                        "jsonrpc": "2.0",
                        "id": "provider-1",
                        "method": "session/update",
                        "result": {},
                    }
                )
            with pytest.raises(AgentRuntimeError, match="invalid JSON-RPC method"):
                await client._dispatch({"jsonrpc": "2.0", "method": 7})
            with pytest.raises(AgentRuntimeError, match="int64"):
                await client._dispatch(
                    {"jsonrpc": "2.0", "id": 2**63, "method": "session/update"}
                )
            with pytest.raises(AgentRuntimeError, match="oversized JSON-RPC id"):
                await client._dispatch(
                    {"jsonrpc": "2.0", "id": "x" * 1025, "method": "session/update"}
                )
            with pytest.raises(AgentRuntimeError, match="oversized JSON-RPC method"):
                await client._dispatch({"jsonrpc": "2.0", "method": "x" * 257})

            future = asyncio.get_running_loop().create_future()
            client._pending[99] = future
            with pytest.raises(AgentRuntimeError, match="result and error"):
                await client._dispatch(
                    {"jsonrpc": "2.0", "id": 99, "result": {}, "error": {}}
                )
            with pytest.raises(AgentRuntimeError, match="request parameters"):
                await client._dispatch(
                    {"jsonrpc": "2.0", "id": 99, "result": {}, "params": {}}
                )
            client._pending.pop(99)
            future.cancel()
        finally:
            await client.close()

    asyncio.run(run())
