from __future__ import annotations

import sqlite3

import pytest

from agent_runtime.state import AgentStateStore, SessionRecord, TurnRecord


def test_state_store_round_trips_session_turn_and_idempotency(tmp_path):
    store = AgentStateStore(tmp_path / "state.sqlite3")
    session = SessionRecord(
        gateway_session_id="ags_abc123",
        runtime_id="muse",
        native_session_id="native-session",
        public_model_id="muse/model-a",
        native_model_id="model-a",
        provider_id="provider-a",
        workspace_mode="isolated",
        workspace_path=str(tmp_path / "workspace"),
        approval_policy="strict",
        initial_native_cursor="c0",
        last_native_cursor="c0",
        status="idle",
        protocol_fingerprint="sha256:test",
        created_at=1,
        updated_at=1,
        released_at=None,
    )
    store.create_session(session)
    turn = TurnRecord("agt_abc123", "ags_abc123", "native-turn", "accepted", 2, 2)
    store.create_turn(turn)
    store.save_idempotency(
        scope="turn.start:ags_abc123",
        key="key-1",
        operation="turn.start",
        request_hash="hash",
        resource_id="agt_abc123",
        command_id="command",
    )

    assert store.get_session("ags_abc123") == session
    assert store.get_session_by_native("muse", "native-session") == session
    assert store.get_turn_by_native("ags_abc123", "native-turn") == turn
    assert store.get_idempotency("turn.start:ags_abc123", "key-1")["request_hash"] == "hash"

    store.update_session("ags_abc123", status="released", released_at=3)
    assert store.get_session("ags_abc123").released_at == 3
    store.update_session("ags_abc123", status="idle", clear_released_at=True)
    assert store.get_session("ags_abc123").released_at is None
    assert store.mark_sessions_for_recovery("muse") == 1
    assert store.get_session("ags_abc123").status == "recovery_required"
    store.mark_session_resumed(
        "ags_abc123",
        status="idle",
        expected_last_cursor="c0",
        resumed_cursor="c9",
    )
    resumed = store.get_session("ags_abc123")
    assert resumed.status == "idle"
    assert resumed.last_native_cursor == "c9"
    store.close()


def test_expired_completed_idempotency_key_can_be_reused(tmp_path):
    store = AgentStateStore(tmp_path / "state.sqlite3")
    store.save_idempotency(
        scope="session.create",
        key="expired",
        operation="session.start",
        request_hash="old",
        resource_id="ags_old",
        command_id="command-old",
        status="completed",
        ttl_seconds=-1,
    )

    assert store.get_idempotency("session.create", "expired") is None
    store.save_idempotency(
        scope="session.create",
        key="expired",
        operation="session.start",
        request_hash="new",
        resource_id="ags_new",
        command_id="command-new",
        status="pending",
    )
    assert store.get_idempotency("session.create", "expired")["request_hash"] == "new"
    store.close()


def test_expired_pending_idempotency_is_retained_for_safe_retry(tmp_path):
    store = AgentStateStore(tmp_path / "state.sqlite3")
    store.save_idempotency(
        scope="turn.start:ags_abc",
        key="pending",
        operation="turn.start",
        request_hash="hash",
        resource_id="agt_abc",
        command_id="command",
        status="pending",
        ttl_seconds=-1,
    )

    assert store.get_idempotency("turn.start:ags_abc", "pending")["status"] == "pending"
    store.close()


def test_completed_idempotency_cannot_be_completed_twice(tmp_path):
    store = AgentStateStore(tmp_path / "state.sqlite3")
    store.save_idempotency(
        scope="session.release:ags_abc",
        key="key",
        operation="session.release",
        request_hash="hash",
        resource_id="ags_abc",
        command_id="command",
        status="pending",
    )
    store.complete_idempotency("session.release:ags_abc", "key")

    with pytest.raises(sqlite3.IntegrityError, match="not pending"):
        store.complete_idempotency("session.release:ags_abc", "key")
    store.close()


def test_state_database_is_private_and_rejects_unknown_schema(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = AgentStateStore(path)
    for sidecar in (tmp_path / "state.sqlite3-wal", tmp_path / "state.sqlite3-shm"):
        assert sidecar.stat().st_mode & 0o777 == 0o600
    store.close()
    assert path.stat().st_mode & 0o777 == 0o600

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 999")
    connection.close()
    with pytest.raises(sqlite3.DatabaseError, match="schema version"):
        AgentStateStore(path)
