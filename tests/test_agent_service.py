from __future__ import annotations

import asyncio
import uuid

import pytest

from agent_runtime.base import AgentModel, NativeEvent, NativeSession, NativeTurn, RuntimeStatus
from agent_runtime.errors import AgentRuntimeError
from agent_runtime.events import AgentEvent, CursorCodec, EventBroker
from agent_runtime.muse.config import MuseConfig
from agent_runtime.registry import RuntimeRegistry
from agent_runtime.service import (
    AgentService,
    _command_id,
    _load_cursor_secret,
    build_agent_service,
)
from agent_runtime.state import AgentStateStore
from agent_runtime.workspaces import WorkspaceManager


class FakeRuntime:
    id = "muse"

    def __init__(self):
        self.handler = None
        self.sessions = {}
        self.released = set()
        self.page = []
        self.page_error = None
        self.resume_session_id = None
        self.start_model_id = "model-a"
        self.complete_during_cancel = False
        self.resolve_during_approval = False
        self.start_commands = []
        self.turn_start_calls = 0
        self.fail_start_once = False
        self.emit_during_resume = None

    def set_event_handler(self, handler):
        self.handler = handler

    async def status(self):
        return RuntimeStatus(
            id="muse",
            status="ready",
            billing_mode="subscription",
            auth="verified",
            protocol_fingerprint="sha256:test",
        )

    async def preflight(self):
        return await self.status()

    async def list_models(self):
        return [
            AgentModel(
                id="muse/model-a",
                runtime="muse",
                provider_id="provider-a",
                native_model_id="model-a",
                display_name="Model A",
            )
        ]

    async def start_session(self, **kwargs):
        self.start_commands.append(kwargs["command_id"])
        if self.fail_start_once:
            self.fail_start_once = False
            raise AgentRuntimeError(
                code="provider_host_exited",
                message="simulated crash after command reservation",
                status_code=503,
                runtime="muse",
            )
        session = NativeSession(
            "native-session",
            "c0",
            "idle",
            self.start_model_id,
            "provider-a",
        )
        self.sessions[session.session_id] = kwargs
        return session

    async def resume_session(self, **kwargs):
        self.released.discard(kwargs["native_session_id"])
        if self.emit_during_resume is not None:
            assert self.handler is not None
            await self.handler(self.emit_during_resume)
        return NativeSession(
            self.resume_session_id or kwargs["native_session_id"],
            "c9",
            "idle",
            "model-a",
            "provider-a",
        )

    async def release_session(self, *, native_session_id):
        self.released.add(native_session_id)

    async def start_turn(self, **kwargs):
        self.turn_start_calls += 1
        assert self.handler is not None
        await self.handler(
            NativeEvent("muse", "turn.started", kwargs["native_session_id"], "native-turn", "c1", {})
        )
        return NativeTurn("native-turn", "accepted", "started")

    async def cancel_turn(self, **kwargs):
        if self.complete_during_cancel:
            assert self.handler is not None
            await self.handler(
                NativeEvent(
                    "muse",
                    "turn.cancelled",
                    kwargs["native_session_id"],
                    kwargs["native_turn_id"],
                    "cancel-terminal",
                    {"terminal": "cancelled"},
                )
            )
        return NativeTurn(kwargs["native_turn_id"], "accepted", "cancel_requested")

    async def decide_approval(self, **kwargs):
        if self.resolve_during_approval:
            assert self.handler is not None
            await self.handler(
                NativeEvent(
                    "muse",
                    "approval.resolved",
                    kwargs["native_session_id"],
                    "native-turn",
                    "approval-terminal",
                    {"approval_id": kwargs["approval_id"], "decision": "denied"},
                )
            )
        return {"status": "accepted"}

    async def answer_user_input(self, **kwargs):
        return {"status": "accepted"}

    async def page_events(self, **kwargs):
        if self.page_error is not None:
            raise self.page_error
        return list(self.page)

    async def emit(self, event):
        assert self.handler is not None
        await self.handler(event)

    async def close(self):
        return None


def build_service(tmp_path):
    runtime = FakeRuntime()
    service = AgentService(
        registry=RuntimeRegistry([runtime]),
        state=AgentStateStore(tmp_path / "state.sqlite3"),
        workspaces=WorkspaceManager(tmp_path / "workspaces"),
        cursor_codec=CursorCodec(b"x" * 32),
    )
    return service, runtime


def test_session_and_turn_lifecycle_with_early_event(tmp_path):
    asyncio.run(_session_and_turn_lifecycle_with_early_event(tmp_path))


async def _session_and_turn_lifecycle_with_early_event(tmp_path):
    service, runtime = build_service(tmp_path)
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )
    replayed = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )
    assert replayed["id"] == session["id"]

    turn = await service.start_turn(
        session_id=session["id"],
        text="hello",
        idempotency_key="turn-key",
    )
    assert service.get_session(session["id"])["status"] == "running"
    iterator = await service.prepare_event_stream(session["id"], session["cursor"])
    started = await anext(iterator)
    assert started is not None
    assert started.type == "turn.started"
    assert started.turn_id == turn["id"]

    await runtime.emit(
        NativeEvent("muse", "turn.completed", "native-session", "native-turn", "c2", {"terminal": "completed"})
    )
    completed = await anext(iterator)
    assert completed is not None and completed.type == "turn.completed"
    assert service.get_session(session["id"])["status"] == "idle"
    await iterator.aclose()

    released = await service.release_session(session["id"], "release-key")
    assert released["status"] == "released"
    resumed = await service.resume_session(session["id"], "resume-key")
    assert resumed["status"] == "idle"
    assert resumed["released_at"] is None
    await service.close()


def test_idempotency_conflict_is_rejected(tmp_path):
    asyncio.run(_idempotency_conflict_is_rejected(tmp_path))


async def _idempotency_conflict_is_rejected(tmp_path):
    service, _runtime = build_service(tmp_path)
    await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="same-key",
    )

    with pytest.raises(AgentRuntimeError) as raised:
        await service.create_session(
            runtime_id="muse",
            public_model_id="muse/different",
            approval_policy="strict",
            idempotency_key="same-key",
        )

    assert raised.value.code == "idempotency_conflict"
    await service.close()


def test_event_cursor_is_bound_to_session(tmp_path):
    asyncio.run(_event_cursor_is_bound_to_session(tmp_path))


async def _event_cursor_is_bound_to_session(tmp_path):
    service, _runtime = build_service(tmp_path)
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )

    with pytest.raises(AgentRuntimeError) as raised:
        await service.prepare_event_stream(session["id"], session["cursor"] + "tampered")

    assert raised.value.code == "invalid_event_cursor"
    await service.close()


def test_session_can_resume_after_gateway_process_restart(tmp_path):
    asyncio.run(_session_can_resume_after_gateway_process_restart(tmp_path))


async def _session_can_resume_after_gateway_process_restart(tmp_path):
    service, _runtime = build_service(tmp_path)
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )
    await service.close()

    state = AgentStateStore(tmp_path / "state.sqlite3")
    assert state.mark_sessions_for_recovery("muse") == 1
    runtime = FakeRuntime()
    restarted = AgentService(
        registry=RuntimeRegistry([runtime]),
        state=state,
        workspaces=WorkspaceManager(tmp_path / "workspaces"),
        cursor_codec=CursorCodec(b"x" * 32),
    )

    assert restarted.get_session(session["id"])["status"] == "recovery_required"
    resumed = await restarted.resume_session(session["id"], "resume-after-restart")

    assert resumed["status"] == "idle"
    assert resumed["released_at"] is None
    await restarted.close()


def test_pending_session_retry_reuses_gateway_and_provider_command_ids(tmp_path):
    asyncio.run(_pending_session_retry_reuses_gateway_and_provider_command_ids(tmp_path))


async def _pending_session_retry_reuses_gateway_and_provider_command_ids(tmp_path):
    service, runtime = build_service(tmp_path)
    runtime.fail_start_once = True

    with pytest.raises(AgentRuntimeError, match="simulated crash"):
        await service.create_session(
            runtime_id="muse",
            public_model_id="muse/model-a",
            approval_policy="strict",
            idempotency_key="reserved-key",
        )

    reservation = service.state.get_idempotency("session.create", "reserved-key")
    assert reservation is not None and reservation["status"] == "pending"
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="reserved-key",
    )

    assert session["id"] == reservation["gateway_resource_id"]
    assert runtime.start_commands == [reservation["native_command_id"]] * 2
    completed = service.state.get_idempotency("session.create", "reserved-key")
    assert completed is not None and completed["status"] == "completed"
    await service.close()


def test_provider_command_ids_are_uuid7():
    assert uuid.UUID(_command_id()).version == 7


def test_cursor_secret_rejects_symlinks_and_oversized_files(tmp_path):
    target = tmp_path / "target.secret"
    target.write_bytes(b"x" * 32)
    target.chmod(0o600)
    link = tmp_path / "link.secret"
    link.symlink_to(target)
    with pytest.raises(RuntimeError, match="symlink"):
        _load_cursor_secret(link)

    oversized = tmp_path / "oversized.secret"
    oversized.write_bytes(b"x" * 4097)
    oversized.chmod(0o600)
    with pytest.raises(RuntimeError, match="too large"):
        _load_cursor_secret(oversized)


def test_state_initialization_failure_degrades_only_agent_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_LLM_MUSE_ENABLED", "true")
    monkeypatch.setenv("LOCAL_LLM_AGENT_STATE_DB", str(tmp_path))
    config = MuseConfig.from_env(repo_root=tmp_path)
    service = build_agent_service(config)

    statuses = asyncio.run(service.list_runtimes())

    assert statuses[0]["status"] == "degraded"
    assert "state initialization failed" in statuses[0]["detail"]
    asyncio.run(service.close())


def test_mutation_reloads_session_after_waiting_for_operation_lock(tmp_path):
    asyncio.run(_mutation_reloads_session_after_waiting_for_operation_lock(tmp_path))


async def _mutation_reloads_session_after_waiting_for_operation_lock(tmp_path):
    service, runtime = build_service(tmp_path)
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )

    await service._operation_lock.acquire()
    task = asyncio.create_task(
        service.start_turn(
            session_id=session["id"],
            text="must not start",
            idempotency_key="turn-key",
        )
    )
    await asyncio.sleep(0)
    service.state.update_session(session["id"], status="recovery_required")
    service._operation_lock.release()

    with pytest.raises(AgentRuntimeError) as raised:
        await task
    assert raised.value.code == "agent_session_conflict"
    assert runtime.turn_start_calls == 0
    await service.close()


def test_interaction_response_does_not_expose_provider_payload(tmp_path):
    asyncio.run(_interaction_response_does_not_expose_provider_payload(tmp_path))


async def _interaction_response_does_not_expose_provider_payload(tmp_path):
    service, runtime = build_service(tmp_path)
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )
    await service.start_turn(
        session_id=session["id"],
        text="hello",
        idempotency_key="turn-key",
    )
    await runtime.emit(
        NativeEvent(
            "muse",
            "approval.requested",
            "native-session",
            "native-turn",
            "approval-requested",
            {"approval_id": "approval-1"},
        )
    )

    response = await service.decide_approval(
        session_id=session["id"],
        approval_id="approval-1",
        decision="deny",
        idempotency_key="approval-key",
    )

    assert response == {"status": "accepted", "id": "approval-1", "replayed": False}
    await service.close()


def test_slow_event_subscriber_is_disconnected_instead_of_losing_events_silently():
    asyncio.run(_slow_event_subscriber_is_disconnected_instead_of_losing_events_silently())


async def _slow_event_subscriber_is_disconnected_instead_of_losing_events_silently():
    broker = EventBroker(queue_size=1)
    _history, queue, _found = await broker.subscribe("ags_abc", None)
    for index in range(2):
        await broker.publish(
            AgentEvent(
                cursor=f"public-{index}",
                type="message.delta",
                session_id="ags_abc",
                turn_id="agt_abc",
                created_at=index,
                data={"text": str(index)},
                native_cursor=f"native-{index}",
            )
        )

    iterator = broker.iter_live("ags_abc", queue)
    with pytest.raises(AgentRuntimeError) as raised:
        await anext(iterator)
    assert raised.value.code == "event_stream_overflow"


def test_event_broker_enforces_event_and_history_byte_limits():
    asyncio.run(_event_broker_enforces_event_and_history_byte_limits())


async def _event_broker_enforces_event_and_history_byte_limits():
    broker = EventBroker(
        max_events_per_session=10,
        max_event_bytes=300,
        max_history_bytes_per_session=350,
    )
    for index in range(2):
        await broker.publish(
            AgentEvent(
                cursor=f"public-{index}",
                type="message.delta",
                session_id="ags_abc",
                turn_id="agt_abc",
                created_at=index,
                data={"text": "x" * 80},
                native_cursor=f"native-{index}",
            )
        )
    history, queue, _found = await broker.subscribe("ags_abc", None)
    assert len(history) == 1
    await broker.unsubscribe("ags_abc", queue)

    with pytest.raises(AgentRuntimeError) as raised:
        await broker.publish(
            AgentEvent(
                cursor="public-large",
                type="message.delta",
                session_id="ags_abc",
                turn_id="agt_abc",
                created_at=3,
                data={"text": "x" * 500},
                native_cursor="native-large",
            )
        )
    assert raised.value.code == "provider_response_too_large"


def test_resume_rejects_changed_native_session_identity(tmp_path):
    asyncio.run(_resume_rejects_changed_native_session_identity(tmp_path))


async def _resume_rejects_changed_native_session_identity(tmp_path):
    service, runtime = build_service(tmp_path)
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )
    await service.release_session(session["id"], "release-key")
    runtime.resume_session_id = "different-native-session"

    with pytest.raises(AgentRuntimeError) as raised:
        await service.resume_session(session["id"], "resume-key")

    assert raised.value.code == "agent_session_invariant_mismatch"
    await service.close()


def test_failed_event_replay_leaves_resumed_session_in_recovery(tmp_path):
    asyncio.run(_failed_event_replay_leaves_resumed_session_in_recovery(tmp_path))


async def _failed_event_replay_leaves_resumed_session_in_recovery(tmp_path):
    service, runtime = build_service(tmp_path)
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )
    await service.release_session(session["id"], "release-key")
    runtime.page_error = AgentRuntimeError(
        code="provider_host_exited",
        message="paging failed",
        status_code=503,
        runtime="muse",
    )

    with pytest.raises(AgentRuntimeError, match="paging failed"):
        await service.resume_session(session["id"], "resume-key")

    assert service.get_session(session["id"])["status"] == "recovery_required"
    await service.close()


def test_unmatched_provider_events_are_globally_bounded(tmp_path):
    asyncio.run(_unmatched_provider_events_are_globally_bounded(tmp_path))


async def _unmatched_provider_events_are_globally_bounded(tmp_path):
    service, _runtime = build_service(tmp_path)
    for index in range(100):
        await service._on_native_event(
            NativeEvent(
                "muse",
                "turn.started",
                f"unknown-{index}",
                f"turn-{index}",
                f"cursor-{index}",
                {},
            )
        )

    assert len(service._pending_native_events) <= 64
    assert service._pending_native_event_count <= 512
    await service.close()


def test_oversized_unmatched_provider_event_is_rejected(tmp_path):
    asyncio.run(_oversized_unmatched_provider_event_is_rejected(tmp_path))


async def _oversized_unmatched_provider_event_is_rejected(tmp_path):
    service, _runtime = build_service(tmp_path)

    with pytest.raises(AgentRuntimeError) as raised:
        await service._on_native_event(
            NativeEvent(
                "muse",
                "message.delta",
                "unknown-session",
                "unknown-turn",
                "cursor",
                {"text": "x" * (513 * 1024)},
            )
        )

    assert raised.value.code == "provider_response_too_large"
    assert service._pending_native_event_count == 0
    await service.close()


def test_foreign_turn_event_for_loaded_session_forces_recovery(tmp_path):
    asyncio.run(_foreign_turn_event_for_loaded_session_forces_recovery(tmp_path))


async def _foreign_turn_event_for_loaded_session_forces_recovery(tmp_path):
    service, runtime = build_service(tmp_path)
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )

    with pytest.raises(AgentRuntimeError) as raised:
        await runtime.emit(
            NativeEvent(
                "muse",
                "turn.started",
                "native-session",
                "foreign-turn",
                "c1",
                {},
            )
        )

    assert raised.value.code == "runtime_protocol_mismatch"
    assert service.get_session(session["id"])["status"] == "recovery_required"
    await service.close()


def test_create_rejects_changed_provider_model_identity(tmp_path):
    asyncio.run(_create_rejects_changed_provider_model_identity(tmp_path))


async def _create_rejects_changed_provider_model_identity(tmp_path):
    service, runtime = build_service(tmp_path)
    runtime.start_model_id = "different-model"

    with pytest.raises(AgentRuntimeError) as raised:
        await service.create_session(
            runtime_id="muse",
            public_model_id="muse/model-a",
            approval_policy="strict",
            idempotency_key="session-key",
        )

    assert raised.value.code == "agent_session_invariant_mismatch"
    await service.close()


def test_replayed_provider_events_update_persisted_session_state(tmp_path):
    asyncio.run(_replayed_provider_events_update_persisted_session_state(tmp_path))


async def _replayed_provider_events_update_persisted_session_state(tmp_path):
    service, runtime = build_service(tmp_path)
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )
    turn = await service.start_turn(
        session_id=session["id"],
        text="hello",
        idempotency_key="turn-key",
    )
    runtime.page = [
        NativeEvent(
            "muse",
            "turn.completed",
            "native-session",
            "native-turn",
            "c2",
            {"terminal": "completed"},
        )
    ]

    iterator = await service.prepare_event_stream(session["id"], session["cursor"])
    completed = await anext(iterator)

    assert completed is not None and completed.type == "turn.completed"
    assert completed.turn_id == turn["id"]
    assert service.get_session(session["id"])["status"] == "idle"
    await iterator.aclose()
    await service.close()


def test_interaction_events_have_explicit_session_states(tmp_path):
    asyncio.run(_interaction_events_have_explicit_session_states(tmp_path))


async def _interaction_events_have_explicit_session_states(tmp_path):
    service, runtime = build_service(tmp_path)
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )
    await service.start_turn(
        session_id=session["id"],
        text="hello",
        idempotency_key="turn-key",
    )
    await runtime.emit(
        NativeEvent(
            "muse",
            "approval.requested",
            "native-session",
            "native-turn",
            "c2",
            {"approval_id": "approval-1"},
        )
    )
    assert service.get_session(session["id"])["status"] == "waiting_for_approval"

    with pytest.raises(AgentRuntimeError) as raised:
        await service.release_session(session["id"], "release-key")
    assert raised.value.code == "agent_session_conflict"

    await runtime.emit(
        NativeEvent(
            "muse",
            "approval.resolved",
            "native-session",
            "native-turn",
            "c3",
            {"approval_id": "approval-1", "decision": "denied"},
        )
    )
    assert service.get_session(session["id"])["status"] == "running"
    await service.close()


def test_event_broker_limits_subscribers_per_session():
    asyncio.run(_event_broker_limits_subscribers_per_session())


async def _event_broker_limits_subscribers_per_session():
    broker = EventBroker(max_subscribers_per_session=1)
    _history, queue, _found = await broker.subscribe("ags_abc", None)

    with pytest.raises(AgentRuntimeError) as raised:
        await broker.subscribe("ags_abc", None)

    assert raised.value.code == "event_subscriber_limit"
    await broker.unsubscribe("ags_abc", queue)


def test_release_closes_existing_event_subscribers(tmp_path):
    asyncio.run(_release_closes_existing_event_subscribers(tmp_path))


async def _release_closes_existing_event_subscribers(tmp_path):
    service, _runtime = build_service(tmp_path)
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )
    iterator = await service.prepare_event_stream(session["id"], session["cursor"])

    await service.release_session(session["id"], "release-key")

    with pytest.raises(StopAsyncIteration):
        await anext(iterator)
    await service.close()


def test_event_stream_preflight_marks_unloaded_session_for_recovery(tmp_path):
    asyncio.run(_event_stream_preflight_marks_unloaded_session_for_recovery(tmp_path))


async def _event_stream_preflight_marks_unloaded_session_for_recovery(tmp_path):
    service, runtime = build_service(tmp_path)
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )
    runtime.page_error = AgentRuntimeError(
        code="agent_session_not_loaded",
        message="resume required",
        status_code=409,
        runtime="muse",
    )

    with pytest.raises(AgentRuntimeError) as raised:
        await service.prepare_event_stream(session["id"], session["cursor"])

    assert raised.value.code == "agent_session_not_loaded"
    assert service.get_session(session["id"])["status"] == "recovery_required"
    assert service.broker._subscribers == {}
    await service.close()


def test_cancel_response_cannot_reopen_a_terminal_turn(tmp_path):
    asyncio.run(_cancel_response_cannot_reopen_a_terminal_turn(tmp_path))


async def _cancel_response_cannot_reopen_a_terminal_turn(tmp_path):
    service, runtime = build_service(tmp_path)
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )
    turn = await service.start_turn(
        session_id=session["id"],
        text="hello",
        idempotency_key="turn-key",
    )
    runtime.complete_during_cancel = True

    cancelled = await service.cancel_turn(
        session_id=session["id"],
        turn_id=turn["id"],
        idempotency_key="cancel-key",
    )

    assert cancelled["status"] == "cancelled"
    assert service.get_session(session["id"])["status"] == "idle"
    await service.close()


def test_late_terminal_event_cannot_reopen_released_session(tmp_path):
    asyncio.run(_late_terminal_event_cannot_reopen_released_session(tmp_path))


async def _late_terminal_event_cannot_reopen_released_session(tmp_path):
    service, runtime = build_service(tmp_path)
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )
    turn = await service.start_turn(
        session_id=session["id"],
        text="hello",
        idempotency_key="turn-key",
    )
    await runtime.emit(
        NativeEvent(
            "muse",
            "turn.completed",
            "native-session",
            "native-turn",
            "c2",
            {"terminal": "completed"},
        )
    )
    await service.release_session(session["id"], "release-key")

    await runtime.emit(
        NativeEvent(
            "muse",
            "turn.completed",
            "native-session",
            "native-turn",
            "c3",
            {"terminal": "completed"},
        )
    )
    await runtime.emit(
        NativeEvent(
            "muse",
            "session.recovery_required",
            "native-session",
            None,
            "c4",
            {"reason": "late-gap"},
        )
    )

    assert turn["id"]
    record = service.state.get_session(session["id"])
    assert record is not None and record.status == "released"
    assert record.last_native_cursor == "c2"
    await service.close()


def test_cancel_rejects_an_already_terminal_turn(tmp_path):
    asyncio.run(_cancel_rejects_an_already_terminal_turn(tmp_path))


async def _cancel_rejects_an_already_terminal_turn(tmp_path):
    service, runtime = build_service(tmp_path)
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )
    turn = await service.start_turn(
        session_id=session["id"],
        text="hello",
        idempotency_key="turn-key",
    )
    await runtime.emit(
        NativeEvent(
            "muse",
            "turn.completed",
            "native-session",
            "native-turn",
            "c2",
            {"terminal": "completed"},
        )
    )

    with pytest.raises(AgentRuntimeError) as raised:
        await service.cancel_turn(
            session_id=session["id"],
            turn_id=turn["id"],
            idempotency_key="cancel-key",
        )

    assert raised.value.code == "agent_turn_conflict"
    await service.close()


def test_interaction_terminal_event_can_complete_idempotency_first(tmp_path):
    asyncio.run(_interaction_terminal_event_can_complete_idempotency_first(tmp_path))


async def _interaction_terminal_event_can_complete_idempotency_first(tmp_path):
    service, runtime = build_service(tmp_path)
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )
    await service.start_turn(
        session_id=session["id"],
        text="hello",
        idempotency_key="turn-key",
    )
    await runtime.emit(
        NativeEvent(
            "muse",
            "approval.requested",
            "native-session",
            "native-turn",
            "approval-requested",
            {"approval_id": "approval-1"},
        )
    )
    runtime.resolve_during_approval = True

    response = await service.decide_approval(
        session_id=session["id"],
        approval_id="approval-1",
        decision="deny",
        idempotency_key="approval-key",
    )

    assert response["status"] == "accepted"
    reservation = service.state.get_idempotency(
        f"approval.decide:{session['id']}:approval-1",
        "approval-key",
    )
    assert reservation is not None and reservation["status"] == "completed"
    await service.close()


def test_provider_gap_forces_recovery_and_preserves_safe_cursor(tmp_path):
    asyncio.run(_provider_gap_forces_recovery_and_preserves_safe_cursor(tmp_path))


async def _provider_gap_forces_recovery_and_preserves_safe_cursor(tmp_path):
    service, runtime = build_service(tmp_path)
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )
    await runtime.emit(
        NativeEvent(
            "muse",
            "session.recovery_required",
            "native-session",
            None,
            "safe-cursor",
            {"reason": "event_gap", "next_cursor": "next-cursor"},
        )
    )
    await runtime.emit(
        NativeEvent(
            "muse",
            "provider.event",
            "native-session",
            None,
            "later-cursor",
            {"method": "session/tokenUsage"},
        )
    )

    record = service.state.get_session(session["id"])
    assert record is not None and record.status == "recovery_required"
    assert record.last_native_cursor == "safe-cursor"
    await service.close()


def test_resume_buffers_reissued_interactions_until_state_is_restored(tmp_path):
    asyncio.run(_resume_buffers_reissued_interactions_until_state_is_restored(tmp_path))


async def _resume_buffers_reissued_interactions_until_state_is_restored(tmp_path):
    service, runtime = build_service(tmp_path)
    session = await service.create_session(
        runtime_id="muse",
        public_model_id="muse/model-a",
        approval_policy="strict",
        idempotency_key="session-key",
    )
    await service.start_turn(
        session_id=session["id"],
        text="hello",
        idempotency_key="turn-key",
    )
    service.state.update_session(session["id"], status="recovery_required")
    runtime.emit_during_resume = NativeEvent(
        "muse",
        "user_input.requested",
        "native-session",
        "native-turn",
        "resume-input",
        {"user_input_id": "input-1"},
    )

    resumed = await service.resume_session(session["id"], "resume-key")

    assert resumed["status"] == "waiting_for_input"
    assert service._pending_native_events == {}
    await service.close()
