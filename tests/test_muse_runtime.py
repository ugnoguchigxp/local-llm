from __future__ import annotations

import asyncio
from typing import cast

import pytest

from agent_runtime.errors import AgentRuntimeError
from agent_runtime.muse.config import MuseConfig
from agent_runtime.base import NativeEvent
from agent_runtime.muse.runtime import MuseRuntime, _validate_initialize_result


class PagingBridge:
    running = True

    def __init__(self, responses):
        self.responses = iter(responses)

    async def request(self, method, params=None, **_kwargs):
        assert method == "events.page"
        assert params is not None
        return next(self.responses)


def event(cursor: str):
    return {
        "type": "provider.event",
        "native_session_id": "native-session",
        "native_turn_id": None,
        "native_cursor": cursor,
        "data": {},
    }


def runtime_with_bridge(monkeypatch, bridge):
    runtime = MuseRuntime(config=cast(MuseConfig, None))

    async def ready_bridge():
        return bridge

    monkeypatch.setattr(runtime, "_ready_bridge", ready_bridge)
    return runtime


def test_event_page_rejects_malformed_rows(monkeypatch):
    runtime = runtime_with_bridge(
        monkeypatch,
        PagingBridge([{"events": ["invalid"], "next_cursor": None}]),
    )

    with pytest.raises(AgentRuntimeError) as raised:
        asyncio.run(runtime.page_events(native_session_id="native-session", cursor="c0"))

    assert raised.value.code == "runtime_protocol_mismatch"


def test_event_page_rejects_foreign_session_events(monkeypatch):
    foreign = event("c1")
    foreign["native_session_id"] = "other-session"
    runtime = runtime_with_bridge(
        monkeypatch,
        PagingBridge([{"events": [foreign], "next_cursor": None}]),
    )

    with pytest.raises(AgentRuntimeError) as raised:
        asyncio.run(runtime.page_events(native_session_id="native-session", cursor="c0"))

    assert raised.value.code == "runtime_protocol_mismatch"


def test_event_page_enforces_total_limit_on_final_page(monkeypatch):
    responses = []
    for page in range(6):
        rows = [event(f"c-{page}-{index}") for index in range(1000)]
        responses.append(
            {
                "events": rows,
                "next_cursor": None if page == 5 else f"page-{page + 1}",
            }
        )
    runtime = runtime_with_bridge(monkeypatch, PagingBridge(responses))

    with pytest.raises(AgentRuntimeError) as raised:
        asyncio.run(runtime.page_events(native_session_id="native-session", cursor="c0"))

    assert raised.value.code == "event_replay_too_large"


def test_event_page_limits_empty_cursor_chains(monkeypatch):
    runtime = runtime_with_bridge(
        monkeypatch,
        PagingBridge(
            [
                {"events": [], "next_cursor": f"page-{index}"}
                for index in range(101)
            ]
        ),
    )

    with pytest.raises(AgentRuntimeError) as raised:
        asyncio.run(runtime.page_events(native_session_id="native-session", cursor="c0"))

    assert raised.value.code == "runtime_protocol_mismatch"


def test_event_protocol_errors_are_classified_as_protocol_mismatch():
    runtime = MuseRuntime(config=cast(MuseConfig, None))

    with pytest.raises(AgentRuntimeError) as raised:
        runtime._native_event(
            {
                "type": "turn.started",
                "native_session_id": "native-session",
                "native_cursor": "c1",
                "native_turn_id": 1,
                "data": {},
            }
        )

    assert raised.value.code == "runtime_protocol_mismatch"


def test_active_turn_tracking_is_scoped_to_session():
    runtime = MuseRuntime(config=cast(MuseConfig, None))
    first = NativeEvent("muse", "turn.started", "session-a", "turn-1", "c1", {})
    second = NativeEvent("muse", "turn.started", "session-b", "turn-1", "c2", {})

    runtime._track_event(first)
    runtime._track_event(second)
    runtime._track_event(
        NativeEvent("muse", "turn.completed", "session-a", "turn-1", "c3", {})
    )

    assert runtime._active_turns == {("session-b", "turn-1")}


def test_turn_start_rejects_queued_or_steered_dispositions():
    from agent_runtime.muse.runtime import _native_turn

    with pytest.raises(AgentRuntimeError) as raised:
        _native_turn(
            {
                "native_turn_id": "turn-1",
                "status": "accepted",
                "disposition": "queued",
            },
            "turn.start",
        )

    assert raised.value.code == "runtime_protocol_mismatch"


def test_initialize_requires_exact_bridge_contract_and_durable_sessions():
    valid = {
        "bridge_protocol": 1,
        "sdk_version": "0.1.1",
        "schema_fingerprint": "sha256:test",
        "session_durability": "durable",
    }
    assert (
        _validate_initialize_result(valid, expected_fingerprint="sha256:test")
        == "sha256:test"
    )

    for field, value in (
        ("bridge_protocol", 2),
        ("sdk_version", "0.2.0"),
        ("session_durability", "ephemeral"),
        ("schema_fingerprint", "sha256:changed"),
    ):
        invalid = {**valid, field: value}
        with pytest.raises(AgentRuntimeError) as raised:
            _validate_initialize_result(invalid, expected_fingerprint="sha256:test")
        assert raised.value.code == "runtime_protocol_mismatch"
