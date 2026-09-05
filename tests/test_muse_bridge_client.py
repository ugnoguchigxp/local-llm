from __future__ import annotations

import os
import asyncio
import sys

import pytest

from agent_runtime.errors import AgentRuntimeError
from agent_runtime.muse.bridge_client import MuseBridgeClient, _redact, _redact_value


def test_bridge_client_correlates_response_and_event(tmp_path):
    asyncio.run(_bridge_client_correlates_response_and_event(tmp_path))


async def _bridge_client_correlates_response_and_event(tmp_path):
    fake = tmp_path / "fake_bridge.py"
    fake.write_text(
        """
import json
import sys
for line in sys.stdin:
    request = json.loads(line)
    if request["method"] == "runtime.shutdown":
        print(json.dumps({"v": 1, "id": request["id"], "ok": True, "result": {"status": "closed"}}), flush=True)
        break
    print(json.dumps({"v": 1, "event": True, "type": "turn.started", "native_session_id": "s1", "native_turn_id": "t1", "native_cursor": "c1", "data": {}}), flush=True)
    print(json.dumps({"v": 1, "id": request["id"], "ok": True, "result": {"ready": True}}), flush=True)
""",
        encoding="utf-8",
    )
    events = []

    async def receive(event):
        events.append(event)

    client = MuseBridgeClient(
        node_binary=sys.executable,
        entrypoint=fake,
        env=dict(os.environ),
        request_timeout_ms=1000,
        event_callback=receive,
    )

    assert await client.request("runtime.health") == {"ready": True}
    assert events[0]["type"] == "turn.started"
    await client.close()
    assert not client.running


def test_bridge_client_classifies_malformed_stdout(tmp_path):
    asyncio.run(_bridge_client_classifies_malformed_stdout(tmp_path))


async def _bridge_client_classifies_malformed_stdout(tmp_path):
    fake = tmp_path / "malformed_bridge.py"
    fake.write_text('print("not-json", flush=True)\n', encoding="utf-8")

    async def receive(_event):
        return None

    client = MuseBridgeClient(
        node_binary=sys.executable,
        entrypoint=fake,
        env=dict(os.environ),
        request_timeout_ms=1000,
        event_callback=receive,
    )

    with pytest.raises(AgentRuntimeError) as raised:
        await client.request("runtime.health")

    assert raised.value.code == "runtime_protocol_mismatch"
    await client.close()


def test_bridge_client_rejects_malformed_success_shape(tmp_path):
    asyncio.run(_bridge_client_rejects_malformed_success_shape(tmp_path))


async def _bridge_client_rejects_malformed_success_shape(tmp_path):
    fake = tmp_path / "malformed_response.py"
    fake.write_text(
        """
import json
import sys
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({"v": 1, "id": request["id"], "ok": True, "result": []}), flush=True)
""",
        encoding="utf-8",
    )

    async def receive(_event):
        return None

    client = MuseBridgeClient(
        node_binary=sys.executable,
        entrypoint=fake,
        env=dict(os.environ),
        request_timeout_ms=1000,
        event_callback=receive,
    )

    with pytest.raises(AgentRuntimeError) as raised:
        await client.request("runtime.health")

    assert raised.value.code == "runtime_protocol_mismatch"
    await client.close()


def test_bridge_client_classifies_eof_and_redacts_stderr(tmp_path):
    asyncio.run(_bridge_client_classifies_eof_and_redacts_stderr(tmp_path))


async def _bridge_client_classifies_eof_and_redacts_stderr(tmp_path):
    fake = tmp_path / "eof_bridge.py"
    fake.write_text(
        'import sys\nprint("api_key=very-secret", file=sys.stderr, flush=True)\n',
        encoding="utf-8",
    )

    async def receive(_event):
        return None

    client = MuseBridgeClient(
        node_binary=sys.executable,
        entrypoint=fake,
        env=dict(os.environ),
        request_timeout_ms=100,
        event_callback=receive,
        debug_log=True,
    )

    with pytest.raises(AgentRuntimeError) as raised:
        await client.request("runtime.health")

    assert raised.value.code == "provider_host_exited"
    assert "very-secret" not in str(raised.value.data)
    assert "[REDACTED]" in str(raised.value.data)
    await client.close()


def test_bridge_client_times_out_without_retrying(tmp_path):
    asyncio.run(_bridge_client_times_out_without_retrying(tmp_path))


async def _bridge_client_times_out_without_retrying(tmp_path):
    fake = tmp_path / "slow_bridge.py"
    fake.write_text(
        "import sys, time\nfor _line in sys.stdin:\n    time.sleep(10)\n",
        encoding="utf-8",
    )

    async def receive(_event):
        return None

    client = MuseBridgeClient(
        node_binary=sys.executable,
        entrypoint=fake,
        env=dict(os.environ),
        request_timeout_ms=50,
        event_callback=receive,
    )

    with pytest.raises(AgentRuntimeError) as raised:
        await client.request("runtime.health")

    assert raised.value.code == "provider_host_exited"
    await client.close()


def test_bridge_redacts_secrets_split_across_stderr_chunks(tmp_path):
    asyncio.run(_bridge_redacts_secrets_split_across_stderr_chunks(tmp_path))


async def _bridge_redacts_secrets_split_across_stderr_chunks(tmp_path):
    fake = tmp_path / "split_stderr.py"
    fake.write_text(
        """
import sys
import time
sys.stderr.write('{"Authorization":"Bearer ')
sys.stderr.flush()
time.sleep(0.05)
sys.stderr.write('split-secret"}\\n')
sys.stderr.flush()
""",
        encoding="utf-8",
    )

    async def receive(_event):
        return None

    client = MuseBridgeClient(
        node_binary=sys.executable,
        entrypoint=fake,
        env=dict(os.environ),
        request_timeout_ms=1000,
        event_callback=receive,
        debug_log=True,
    )

    with pytest.raises(AgentRuntimeError) as raised:
        await client.request("runtime.health")

    assert "split-secret" not in str(raised.value.data)
    assert "[REDACTED]" in str(raised.value.data)
    await client.close()


def test_bridge_diagnostic_redaction_handles_json_and_bearer_tokens():
    redacted = _redact(
        '{"Authorization":"Bearer super-secret","api_key":"json-secret"} Bearer bare-secret'
    )

    assert "super-secret" not in redacted
    assert "json-secret" not in redacted
    assert "bare-secret" not in redacted
    assert redacted.count("[REDACTED]") == 3


def test_bridge_error_data_is_recursively_redacted():
    value = _redact_value(
        {
            "client_secret": "first-secret",
            "nested": {"token": "second-secret", "safe": "ok"},
        }
    )

    assert value == {
        "client_secret": "[REDACTED]",
        "nested": {"token": "[REDACTED]", "safe": "ok"},
    }


def test_cancelled_bridge_request_does_not_leak_pending_future(tmp_path):
    asyncio.run(_cancelled_bridge_request_does_not_leak_pending_future(tmp_path))


async def _cancelled_bridge_request_does_not_leak_pending_future(tmp_path):
    fake = tmp_path / "slow_bridge.py"
    fake.write_text("import sys, time\nfor _line in sys.stdin:\n    time.sleep(10)\n", encoding="utf-8")

    async def receive(_event):
        return None

    client = MuseBridgeClient(
        node_binary=sys.executable,
        entrypoint=fake,
        env=dict(os.environ),
        request_timeout_ms=200,
        event_callback=receive,
    )
    task = asyncio.create_task(client.request("runtime.health"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client._pending == {}
    await client.close()
