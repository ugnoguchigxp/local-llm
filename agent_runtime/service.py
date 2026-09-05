from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from agent_runtime.base import NativeEvent
from agent_runtime.catalog import AgentCatalog
from agent_runtime.errors import AgentRuntimeError
from agent_runtime.events import AgentEvent, CursorCodec, EventBroker, now_millis
from agent_runtime.muse.config import MuseConfig
from agent_runtime.muse.runtime import MuseRuntime
from agent_runtime.registry import RuntimeRegistry
from agent_runtime.state import AgentStateStore, SessionRecord, TurnRecord
from agent_runtime.workspaces import WorkspaceManager


TERMINAL_EVENTS = {"turn.completed", "turn.cancelled", "turn.failed", "turn.unqueued"}
ACTIVE_SESSION_STATES = {"running", "waiting_for_approval", "waiting_for_input"}
MAX_PENDING_NATIVE_SESSIONS = 64
MAX_PENDING_NATIVE_EVENTS = 512
MAX_PENDING_NATIVE_EVENTS_PER_SESSION = 32
MAX_PENDING_NATIVE_EVENT_BYTES = 512 * 1024


class _CursorWindow:
    def __init__(self, limit: int = 8192) -> None:
        self._limit = limit
        self._order: deque[str] = deque()
        self._values: set[str] = set()

    def add(self, cursor: str) -> bool:
        if cursor in self._values:
            return False
        if len(self._order) >= self._limit:
            self._values.discard(self._order.popleft())
        self._order.append(cursor)
        self._values.add(cursor)
        return True


class AgentService:
    def __init__(
        self,
        *,
        registry: RuntimeRegistry,
        state: AgentStateStore | None,
        workspaces: WorkspaceManager,
        cursor_codec: CursorCodec,
        broker: EventBroker | None = None,
        initialization_error: str | None = None,
    ) -> None:
        self.registry = registry
        self.catalog = AgentCatalog(registry)
        self.state = state
        self.workspaces = workspaces
        self.cursor_codec = cursor_codec
        self.broker = broker or EventBroker()
        self.initialization_error = initialization_error
        self._operation_lock = asyncio.Lock()
        self._pending_native_events: OrderedDict[
            tuple[str, str], deque[NativeEvent]
        ] = OrderedDict()
        self._pending_native_event_count = 0
        self._resuming_native_sessions: set[tuple[str, str]] = set()
        self._starting_turn_sessions: set[tuple[str, str]] = set()
        for runtime in registry.all():
            runtime.set_event_handler(self._on_native_event)

    async def list_runtimes(self) -> list[dict[str, Any]]:
        statuses = [_runtime_status_dict(await runtime.status()) for runtime in self.registry.all()]
        if self.initialization_error:
            for status in statuses:
                status["status"] = "degraded"
                status["detail"] = self.initialization_error
        return statuses

    async def preflight(self, runtime_id: str) -> dict[str, Any]:
        if self.initialization_error:
            self._require_state()
        return _runtime_status_dict(await self.registry.get(runtime_id).preflight())

    async def list_models(self, runtime_id: str) -> list[dict[str, Any]]:
        models = await self.catalog.list_models(runtime_id)
        return [
            {
                "id": model.id,
                "object": "agent.model",
                "runtime": model.runtime,
                "provider_id": model.provider_id,
                "native_model_id": model.native_model_id,
                "display_name": model.display_name,
                "context_limit": model.context_limit,
                "output_limit": model.output_limit,
                "capabilities": model.capabilities,
            }
            for model in models
        ]

    async def create_session(
        self,
        *,
        runtime_id: str,
        public_model_id: str,
        approval_policy: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        state = self._require_state()
        request_hash = _hash_request(
            {
                "runtime": runtime_id,
                "model": public_model_id,
                "approval_policy": approval_policy,
                "workspace": {"mode": "isolated"},
            }
        )
        scope = "session.create"
        async with self._operation_lock:
            previous = state.get_idempotency(scope, idempotency_key)
            if previous is not None:
                self._check_idempotency(previous, request_hash)
                gateway_session_id = str(previous["gateway_resource_id"])
                if previous["status"] == "completed":
                    record = state.get_session(gateway_session_id)
                    if record is None:
                        raise AgentRuntimeError(
                            code="idempotency_state_invalid",
                            message="The idempotent session record is missing.",
                            status_code=500,
                        )
                    return self._session_dict(record)
                if previous["status"] != "pending":
                    raise AgentRuntimeError(
                        code="idempotency_state_invalid",
                        message="The idempotent session reservation has an invalid status.",
                        status_code=500,
                    )
                command_id = str(previous["native_command_id"])
            else:
                gateway_session_id = _public_id("ags")
                command_id = _command_id()

            runtime = self.registry.get(runtime_id)
            model = await self.catalog.resolve(runtime_id, public_model_id)
            if previous is None:
                state.save_idempotency(
                    scope=scope,
                    key=idempotency_key,
                    operation="session.start",
                    request_hash=request_hash,
                    resource_id=gateway_session_id,
                    command_id=command_id,
                    status="pending",
                )

            workspace = self.workspaces.create_isolated(gateway_session_id)
            native = await runtime.start_session(
                workspace_root=str(workspace),
                model_id=model.native_model_id,
                provider_id=model.provider_id,
                approval_mode=approval_policy,
                command_id=command_id,
            )
            if native.model_id != model.native_model_id or native.provider_id != model.provider_id:
                raise AgentRuntimeError(
                    code="agent_session_invariant_mismatch",
                    message="The provider session did not preserve the requested model identity.",
                    status_code=409,
                    runtime=runtime_id,
                )
            status = await runtime.status()
            if not status.protocol_fingerprint:
                raise AgentRuntimeError(
                    code="runtime_protocol_mismatch",
                    message="The runtime did not report a protocol fingerprint.",
                    status_code=503,
                    runtime=runtime_id,
                )
            now = now_millis()
            record = SessionRecord(
                gateway_session_id=gateway_session_id,
                runtime_id=runtime_id,
                native_session_id=native.session_id,
                public_model_id=public_model_id,
                native_model_id=model.native_model_id,
                provider_id=model.provider_id,
                workspace_mode="isolated",
                workspace_path=str(workspace),
                approval_policy=approval_policy,
                initial_native_cursor=native.view_cursor,
                last_native_cursor=native.view_cursor,
                status="running" if native.status == "running" else "idle",
                protocol_fingerprint=status.protocol_fingerprint,
                created_at=now,
                updated_at=now,
                released_at=None,
            )
            state.create_session_and_complete_idempotency(
                record,
                scope=scope,
                key=idempotency_key,
            )
            await self._drain_pending_events(runtime_id, native.session_id)
            return self._session_dict(state.get_session(gateway_session_id) or record)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._session_dict(self._get_session_record(session_id))

    async def resume_session(self, session_id: str, idempotency_key: str) -> dict[str, Any]:
        state = self._require_state()
        request_hash = _hash_request({"session_id": session_id, "operation": "resume"})
        scope = f"session.resume:{session_id}"
        async with self._operation_lock:
            record = self._get_session_record(session_id)
            previous = state.get_idempotency(scope, idempotency_key)
            if previous is not None:
                self._check_idempotency(previous, request_hash)
                if previous["status"] == "completed":
                    return self._session_dict(self._get_session_record(session_id))
                if previous["status"] != "pending":
                    raise AgentRuntimeError(
                        code="idempotency_state_invalid",
                        message="The idempotent resume reservation has an invalid status.",
                        status_code=500,
                    )
                command_id = str(previous["native_command_id"])
            else:
                command_id = _command_id()
            if previous is None and record.status not in {"released", "recovery_required"}:
                raise AgentRuntimeError(
                    code="agent_session_conflict",
                    message="Only a released or recovery-required session can be resumed.",
                    status_code=409,
                    runtime=record.runtime_id,
                )
            self._validate_session_invariants(record)
            runtime = self.registry.get(record.runtime_id)
            model = await self.catalog.resolve(record.runtime_id, record.public_model_id)
            if model.native_model_id != record.native_model_id or model.provider_id != record.provider_id:
                raise AgentRuntimeError(
                    code="agent_session_invariant_mismatch",
                    message="The session model no longer matches the verified runtime catalog.",
                    status_code=409,
                    runtime=record.runtime_id,
                )
            runtime_status = await runtime.preflight()
            if runtime_status.protocol_fingerprint != record.protocol_fingerprint:
                raise AgentRuntimeError(
                    code="runtime_protocol_mismatch",
                    message="The session was created with a different runtime protocol fingerprint.",
                    status_code=409,
                    runtime=record.runtime_id,
                )
            if previous is None:
                state.save_idempotency(
                    scope=scope,
                    key=idempotency_key,
                    operation="session.resume",
                    request_hash=request_hash,
                    resource_id=session_id,
                    command_id=command_id,
                    status="pending",
                )
            native_key = (record.runtime_id, record.native_session_id)
            self._resuming_native_sessions.add(native_key)
            try:
                native = await runtime.resume_session(
                    native_session_id=record.native_session_id,
                    cursor=record.last_native_cursor,
                    command_id=command_id,
                )
                session_changed = native.session_id != record.native_session_id
                model_changed = native.model_id != record.native_model_id
                provider_changed = native.provider_id != record.provider_id
                if session_changed or model_changed or provider_changed:
                    raise AgentRuntimeError(
                        code="agent_session_invariant_mismatch",
                        message="The resumed provider session changed model identity.",
                        status_code=409,
                        runtime=record.runtime_id,
                    )
                state.mark_session_resumed(
                    session_id,
                    status="running" if native.status == "running" else "idle",
                    expected_last_cursor=record.last_native_cursor,
                    resumed_cursor=native.view_cursor,
                )
                self._resuming_native_sessions.discard(native_key)
                for missed in await runtime.page_events(
                    native_session_id=record.native_session_id,
                    cursor=record.last_native_cursor,
                ):
                    await self._on_native_event(missed)
                await self._drain_pending_events(record.runtime_id, record.native_session_id)
                state.complete_idempotency(scope, idempotency_key)
            except BaseException:
                state.update_session(session_id, status="recovery_required")
                raise
            finally:
                self._resuming_native_sessions.discard(native_key)
            return self._session_dict(self._get_session_record(session_id))

    async def release_session(self, session_id: str, idempotency_key: str) -> dict[str, Any]:
        state = self._require_state()
        request_hash = _hash_request({"session_id": session_id, "operation": "release"})
        scope = f"session.release:{session_id}"
        async with self._operation_lock:
            record = self._get_session_record(session_id)
            previous = state.get_idempotency(scope, idempotency_key)
            if previous is not None:
                self._check_idempotency(previous, request_hash)
                if previous["status"] == "completed":
                    return self._session_dict(self._get_session_record(session_id))
                if previous["status"] != "pending":
                    raise AgentRuntimeError(
                        code="idempotency_state_invalid",
                        message="The idempotent release reservation has an invalid status.",
                        status_code=500,
                    )
                command_id = str(previous["native_command_id"])
            else:
                command_id = _command_id()
            if record.status in ACTIVE_SESSION_STATES:
                raise AgentRuntimeError(
                    code="agent_session_conflict",
                    message="An active session must be settled before release.",
                    status_code=409,
                    runtime=record.runtime_id,
                )
            if previous is None:
                state.save_idempotency(
                    scope=scope,
                    key=idempotency_key,
                    operation="session.release",
                    request_hash=request_hash,
                    resource_id=session_id,
                    command_id=command_id,
                    status="pending",
                )
            if record.status not in {"released", "recovery_required"}:
                try:
                    await self.registry.get(record.runtime_id).release_session(
                        native_session_id=record.native_session_id
                    )
                except AgentRuntimeError as exc:
                    if exc.code not in {"provider_host_exited", "agent_session_not_loaded"}:
                        raise
            state.update_session(session_id, status="released", released_at=now_millis())
            state.complete_idempotency(scope, idempotency_key)
            await self.broker.discard_history(session_id)
            return self._session_dict(self._get_session_record(session_id))

    async def start_turn(
        self,
        *,
        session_id: str,
        text: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        state = self._require_state()
        request_hash = _hash_request({"session_id": session_id, "text": text})
        scope = f"turn.start:{session_id}"
        async with self._operation_lock:
            record = self._get_session_record(session_id)
            previous = state.get_idempotency(scope, idempotency_key)
            if previous is not None:
                self._check_idempotency(previous, request_hash)
                gateway_turn_id = str(previous["gateway_resource_id"])
                if previous["status"] == "completed":
                    turn = state.get_turn(session_id, gateway_turn_id)
                    if turn is None:
                        raise AgentRuntimeError(
                            code="idempotency_state_invalid",
                            message="The idempotent turn record is missing.",
                            status_code=500,
                        )
                    return self._turn_dict(turn)
                if previous["status"] != "pending":
                    raise AgentRuntimeError(
                        code="idempotency_state_invalid",
                        message="The idempotent turn reservation has an invalid status.",
                        status_code=500,
                    )
                command_id = str(previous["native_command_id"])
            else:
                gateway_turn_id = _public_id("agt")
                command_id = _command_id()
            if record.status != "idle":
                raise AgentRuntimeError(
                    code="agent_session_conflict",
                    message="The session is not idle.",
                    status_code=409,
                    runtime=record.runtime_id,
                )
            if previous is None:
                state.save_idempotency(
                    scope=scope,
                    key=idempotency_key,
                    operation="turn.start",
                    request_hash=request_hash,
                    resource_id=gateway_turn_id,
                    command_id=command_id,
                    status="pending",
                )
            native_key = (record.runtime_id, record.native_session_id)
            self._starting_turn_sessions.add(native_key)
            try:
                try:
                    native = await self.registry.get(record.runtime_id).start_turn(
                        native_session_id=record.native_session_id,
                        text=text,
                        command_id=command_id,
                    )
                except AgentRuntimeError as exc:
                    self._mark_recovery_on_error(record, exc)
                    raise
                now = now_millis()
                turn = TurnRecord(
                    gateway_turn_id=gateway_turn_id,
                    gateway_session_id=session_id,
                    native_turn_id=native.turn_id,
                    status="accepted",
                    created_at=now,
                    updated_at=now,
                )
                state.create_turn_and_complete_idempotency(
                    turn,
                    scope=scope,
                    key=idempotency_key,
                )
                await self._drain_pending_events(record.runtime_id, record.native_session_id)
                return self._turn_dict(state.get_turn(session_id, gateway_turn_id) or turn)
            finally:
                self._starting_turn_sessions.discard(native_key)

    async def cancel_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        state = self._require_state()
        request_hash = _hash_request({"session_id": session_id, "turn_id": turn_id})
        scope = f"turn.cancel:{turn_id}"
        async with self._operation_lock:
            session = self._get_session_record(session_id)
            turn = state.get_turn(session_id, turn_id)
            if turn is None:
                raise AgentRuntimeError(
                    code="agent_turn_not_found",
                    message="Agent turn was not found.",
                    status_code=404,
                    runtime=session.runtime_id,
                )
            previous = state.get_idempotency(scope, idempotency_key)
            if previous is not None:
                self._check_idempotency(previous, request_hash)
                if previous["status"] == "completed":
                    return self._turn_dict(state.get_turn(session_id, turn_id) or turn)
                if previous["status"] != "pending":
                    raise AgentRuntimeError(
                        code="idempotency_state_invalid",
                        message="The idempotent cancel reservation has an invalid status.",
                        status_code=500,
                    )
                command_id = str(previous["native_command_id"])
            else:
                command_id = _command_id()
            if turn.status in {"completed", "cancelled", "failed", "unqueued", "cancel_requested"}:
                if previous is not None:
                    state.ensure_idempotency_completed(scope, idempotency_key)
                    return self._turn_dict(state.get_turn(session_id, turn_id) or turn)
                raise AgentRuntimeError(
                    code="agent_turn_conflict",
                    message="The turn is already terminal or cancellation was already requested.",
                    status_code=409,
                    runtime=session.runtime_id,
                )
            if previous is None:
                state.save_idempotency(
                    scope=scope,
                    key=idempotency_key,
                    operation="turn.cancel",
                    request_hash=request_hash,
                    resource_id=turn_id,
                    command_id=command_id,
                    status="pending",
                )
            try:
                await self.registry.get(session.runtime_id).cancel_turn(
                    native_session_id=session.native_session_id,
                    native_turn_id=turn.native_turn_id,
                    command_id=command_id,
                )
            except AgentRuntimeError as exc:
                self._mark_recovery_on_error(session, exc)
                raise
            state.update_turn_if_nonterminal(turn_id, "cancel_requested")
            state.ensure_idempotency_completed(scope, idempotency_key)
            return self._turn_dict(state.get_turn(session_id, turn_id) or turn)

    async def decide_approval(
        self,
        *,
        session_id: str,
        approval_id: str,
        decision: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self._interaction_command(
            session_id=session_id,
            scope=f"approval.decide:{session_id}:{approval_id}",
            idempotency_key=idempotency_key,
            request_body={"approval_id": approval_id, "decision": decision},
            resource_id=approval_id,
            operation="approval.decide",
            expected_session_status="waiting_for_approval",
            invoke=lambda session, command_id: self.registry.get(
                session.runtime_id
            ).decide_approval(
                native_session_id=session.native_session_id,
                approval_id=approval_id,
                decision=decision,
                command_id=command_id,
            ),
        )

    async def answer_user_input(
        self,
        *,
        session_id: str,
        user_input_id: str,
        answers: list[dict[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self._interaction_command(
            session_id=session_id,
            scope=f"user_input.answer:{session_id}:{user_input_id}",
            idempotency_key=idempotency_key,
            request_body={"user_input_id": user_input_id, "answers": answers},
            resource_id=user_input_id,
            operation="user_input.answer",
            expected_session_status="waiting_for_input",
            invoke=lambda session, command_id: self.registry.get(
                session.runtime_id
            ).answer_user_input(
                native_session_id=session.native_session_id,
                user_input_id=user_input_id,
                answers=answers,
                command_id=command_id,
            ),
        )

    async def _interaction_command(
        self,
        *,
        session_id: str,
        scope: str,
        idempotency_key: str,
        request_body: dict[str, Any],
        resource_id: str,
        operation: str,
        expected_session_status: str,
        invoke: Callable[[SessionRecord, str], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        state = self._require_state()
        request_hash = _hash_request(request_body)
        async with self._operation_lock:
            session = self._get_session_record(session_id)
            previous = state.get_idempotency(scope, idempotency_key)
            if previous is not None:
                self._check_idempotency(previous, request_hash)
                if previous["status"] == "completed":
                    return {"status": "accepted", "id": resource_id, "replayed": True}
                if previous["status"] != "pending":
                    raise AgentRuntimeError(
                        code="idempotency_state_invalid",
                        message="The interaction reservation has an invalid status.",
                        status_code=500,
                    )
                command_id = str(previous["native_command_id"])
            else:
                if session.status != expected_session_status:
                    raise AgentRuntimeError(
                        code="agent_session_conflict",
                        message="The session is not waiting for this interaction.",
                        status_code=409,
                        runtime=session.runtime_id,
                    )
                command_id = _command_id()
                state.save_idempotency(
                    scope=scope,
                    key=idempotency_key,
                    operation=operation,
                    request_hash=request_hash,
                    resource_id=resource_id,
                    command_id=command_id,
                    status="pending",
                )
            if session.status in {"released", "recovery_required"}:
                raise AgentRuntimeError(
                    code="agent_session_conflict",
                    message="Resume the session before resolving interactions.",
                    status_code=409,
                    runtime=session.runtime_id,
                )
            try:
                await invoke(session, command_id)
            except AgentRuntimeError as exc:
                self._mark_recovery_on_error(session, exc)
                raise
            state.ensure_idempotency_completed(scope, idempotency_key)
            return {"status": "accepted", "id": resource_id, "replayed": False}

    async def prepare_event_stream(
        self,
        session_id: str,
        public_cursor: str | None,
    ) -> AsyncIterator[AgentEvent | None]:
        session = self._get_session_record(session_id)
        if session.status in {"released", "recovery_required"}:
            raise AgentRuntimeError(
                code="agent_session_conflict",
                message="Resume the session before opening its event stream.",
                status_code=409,
                runtime=session.runtime_id,
            )
        native_after = None
        if public_cursor:
            native_after = self.cursor_codec.decode(
                public_cursor,
                session_id=session_id,
                runtime_id=session.runtime_id,
            )
        history, queue, found = await self.broker.subscribe(
            session_id,
            native_after,
            session.initial_native_cursor,
        )
        runtime = self.registry.get(session.runtime_id)
        if native_after is None or native_after == session.initial_native_cursor:
            page_from = session.initial_native_cursor
        elif not found:
            page_from = native_after
        else:
            page_from = None
        replay: list[NativeEvent] = []
        if page_from is not None:
            try:
                replay = await runtime.page_events(
                    native_session_id=session.native_session_id,
                    cursor=page_from,
                )
            except BaseException as exc:
                await self.broker.unsubscribe(session_id, queue)
                if isinstance(exc, AgentRuntimeError):
                    self._mark_recovery_on_error(session, exc)
                raise

        async def iterator() -> AsyncIterator[AgentEvent | None]:
            delivered = _CursorWindow()
            try:
                for native in replay:
                    event = await self._apply_native_event(native, publish=False)
                    if event is not None and (
                        event.type == "session.recovery_required"
                        or delivered.add(event.native_cursor)
                    ):
                        yield event
                for event in history:
                    if (
                        event.type == "session.recovery_required"
                        or delivered.add(event.native_cursor)
                    ):
                        yield event
                async for event in self.broker.iter_live(session_id, queue):
                    if event is None:
                        yield None
                    elif (
                        event.type == "session.recovery_required"
                        or delivered.add(event.native_cursor)
                    ):
                        yield event
            finally:
                await self.broker.unsubscribe(session_id, queue)

        return iterator()

    async def _on_native_event(self, native: NativeEvent) -> None:
        await self._apply_native_event(native, publish=True)

    async def _apply_native_event(
        self,
        native: NativeEvent,
        *,
        publish: bool,
    ) -> AgentEvent | None:
        state = self.state
        if state is None:
            return None
        session = state.get_session_by_native(native.runtime_id, native.native_session_id)
        if session is None:
            self._buffer_pending_event(native)
            return None
        native_key = (native.runtime_id, native.native_session_id)
        if session.status in {"released", "recovery_required"}:
            if native_key in self._resuming_native_sessions:
                self._buffer_pending_event(native)
            return None
        event = self._to_agent_event(session, native)
        if event is None:
            if (
                native_key in self._starting_turn_sessions
                or native_key in self._resuming_native_sessions
            ):
                self._buffer_pending_event(native)
                return None
            state.update_session(
                session.gateway_session_id,
                status="recovery_required",
            )
            raise AgentRuntimeError(
                code="runtime_protocol_mismatch",
                message="The provider emitted an event for an unknown turn.",
                status_code=503,
                runtime=session.runtime_id,
            )
        try:
            self.broker.validate_event(event)
        except AgentRuntimeError as exc:
            if exc.runtime is None:
                exc.runtime = session.runtime_id
            state.update_session(
                session.gateway_session_id,
                status="recovery_required",
            )
            raise
        if event.type in {"session.recovery_required", "session.invariant_changed"}:
            state.update_session(
                session.gateway_session_id,
                last_native_cursor=native.native_cursor,
                status="recovery_required",
            )
            if publish:
                await self.broker.publish(event)
            return event
        state.update_session(session.gateway_session_id, last_native_cursor=native.native_cursor)
        if event.type == "approval.resolved":
            approval_id = event.data.get("approval_id")
            native_decision = event.data.get("decision")
            decision = (
                "allow_once"
                if native_decision == "approved"
                else "deny" if native_decision == "denied" else None
            )
            if isinstance(approval_id, str) and decision is not None:
                state.complete_pending_scope_with_hash(
                    f"approval.decide:{session.gateway_session_id}:{approval_id}",
                    _hash_request({"approval_id": approval_id, "decision": decision}),
                )
        if event.type == "turn.started" and event.turn_id:
            if state.update_turn_if_nonterminal(event.turn_id, "running"):
                state.transition_session_from_event(session.gateway_session_id, "running")
        elif event.type in {"approval.requested", "user_input.requested"} and event.turn_id:
            waiting_status = (
                "waiting_for_approval"
                if event.type == "approval.requested"
                else "waiting_for_input"
            )
            if state.update_turn_if_nonterminal(event.turn_id, waiting_status):
                state.transition_session_from_event(
                    session.gateway_session_id,
                    waiting_status,
                )
        elif event.type in {"approval.resolved", "user_input.resolved"} and event.turn_id:
            turn = state.get_turn(session.gateway_session_id, event.turn_id)
            if turn is not None and turn.status not in {
                "completed",
                "cancelled",
                "failed",
                "unqueued",
            }:
                if state.update_turn_if_nonterminal(event.turn_id, "running"):
                    state.transition_session_from_event(
                        session.gateway_session_id,
                        "running",
                    )
        elif event.type in TERMINAL_EVENTS and event.turn_id:
            if state.update_turn_if_nonterminal(
                event.turn_id,
                event.type.removeprefix("turn."),
            ):
                state.transition_session_from_event(session.gateway_session_id, "idle")
        if publish:
            await self.broker.publish(event)
        return event

    def _to_agent_event(self, session: SessionRecord, native: NativeEvent) -> AgentEvent | None:
        gateway_turn_id: str | None = None
        if native.native_turn_id:
            state = self._require_state()
            turn = state.get_turn_by_native(session.gateway_session_id, native.native_turn_id)
            if turn is None:
                return None
            gateway_turn_id = turn.gateway_turn_id
        return AgentEvent(
            cursor=self.cursor_codec.encode(
                session_id=session.gateway_session_id,
                runtime_id=session.runtime_id,
                native_cursor=native.native_cursor,
            ),
            type=native.event_type,
            session_id=session.gateway_session_id,
            turn_id=gateway_turn_id,
            created_at=now_millis(),
            data=native.data,
            native_cursor=native.native_cursor,
        )

    async def _drain_pending_events(self, runtime_id: str, native_session_id: str) -> None:
        key = (runtime_id, native_session_id)
        pending = self._pending_native_events.pop(key, deque())
        self._pending_native_event_count -= len(pending)
        while pending:
            event = pending.popleft()
            state = self._require_state()
            session = state.get_session_by_native(event.runtime_id, event.native_session_id)
            if session is None:
                self._buffer_pending_event(event)
                continue
            if self._to_agent_event(session, event) is None:
                state.update_session(
                    session.gateway_session_id,
                    status="recovery_required",
                )
                raise AgentRuntimeError(
                    code="runtime_protocol_mismatch",
                    message="The provider emitted an event for an unknown turn.",
                    status_code=503,
                    runtime=session.runtime_id,
                )
            await self._on_native_event(event)

    def _buffer_pending_event(self, event: NativeEvent) -> None:
        try:
            encoded_size = len(
                json.dumps(
                    {
                        "type": event.event_type,
                        "session": event.native_session_id,
                        "turn": event.native_turn_id,
                        "cursor": event.native_cursor,
                        "data": event.data,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise AgentRuntimeError(
                code="runtime_protocol_mismatch",
                message="The provider emitted an invalid event payload.",
                status_code=503,
                runtime=event.runtime_id,
            ) from exc
        if encoded_size > MAX_PENDING_NATIVE_EVENT_BYTES:
            raise AgentRuntimeError(
                code="provider_response_too_large",
                message="An unmatched provider event exceeded the safe buffer limit.",
                status_code=502,
                runtime=event.runtime_id,
            )
        key = (event.runtime_id, event.native_session_id)
        while self._pending_native_event_count >= MAX_PENDING_NATIVE_EVENTS:
            oldest_key = next(iter(self._pending_native_events))
            oldest = self._pending_native_events[oldest_key]
            oldest.popleft()
            self._pending_native_event_count -= 1
            if not oldest:
                self._pending_native_events.pop(oldest_key)
        pending = self._pending_native_events.get(key)
        if pending is None:
            if len(self._pending_native_events) >= MAX_PENDING_NATIVE_SESSIONS:
                _discarded_key, discarded = self._pending_native_events.popitem(last=False)
                self._pending_native_event_count -= len(discarded)
            pending = deque()
            self._pending_native_events[key] = pending
        else:
            self._pending_native_events.move_to_end(key)
        if len(pending) >= MAX_PENDING_NATIVE_EVENTS_PER_SESSION:
            pending.popleft()
            self._pending_native_event_count -= 1
        pending.append(event)
        self._pending_native_event_count += 1

    def _mark_recovery_on_error(self, session: SessionRecord, exc: AgentRuntimeError) -> None:
        if exc.code in {
            "provider_host_exited",
            "agent_session_not_loaded",
            "runtime_protocol_mismatch",
            "provider_response_too_large",
        }:
            self._require_state().update_session(
                session.gateway_session_id,
                status="recovery_required",
            )

    def _session_dict(self, record: SessionRecord) -> dict[str, Any]:
        return {
            "id": record.gateway_session_id,
            "runtime": record.runtime_id,
            "model": record.public_model_id,
            "status": record.status,
            "approval_policy": record.approval_policy,
            "workspace": {"mode": record.workspace_mode},
            "events_url": f"/v1/agents/sessions/{record.gateway_session_id}/events",
            "cursor": self.cursor_codec.encode(
                session_id=record.gateway_session_id,
                runtime_id=record.runtime_id,
                native_cursor=record.last_native_cursor,
            ),
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "released_at": record.released_at,
        }

    def _turn_dict(self, record: TurnRecord) -> dict[str, Any]:
        return {
            "id": record.gateway_turn_id,
            "session_id": record.gateway_session_id,
            "status": record.status,
            "events_url": f"/v1/agents/sessions/{record.gateway_session_id}/events",
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    def _get_session_record(self, session_id: str) -> SessionRecord:
        state = self._require_state()
        record = state.get_session(session_id)
        if record is None:
            raise AgentRuntimeError(
                code="agent_session_not_found",
                message="Agent session was not found.",
                status_code=404,
            )
        return record

    def _require_state(self) -> AgentStateStore:
        if self.state is None:
            raise AgentRuntimeError(
                code="runtime_unavailable",
                message="Agent Runtime state is unavailable while Muse is disabled.",
                status_code=503,
                runtime="muse",
            )
        return self.state

    @staticmethod
    def _check_idempotency(previous: Any, request_hash: str) -> None:
        if previous["request_hash"] != request_hash:
            raise AgentRuntimeError(
                code="idempotency_conflict",
                message="The idempotency key was already used with a different request.",
                status_code=409,
            )

    def _validate_session_invariants(self, record: SessionRecord) -> None:
        try:
            self.workspaces.validate_isolated(
                record.gateway_session_id,
                record.workspace_path,
            )
        except AgentRuntimeError as exc:
            if exc.runtime is None:
                exc.runtime = record.runtime_id
            raise

    async def close(self) -> None:
        state, self.state = self.state, None
        try:
            await self.registry.close()
        finally:
            if state is not None:
                state.close()


def build_agent_service(config: MuseConfig | None = None) -> AgentService:
    muse_config = config or MuseConfig.from_env()
    runtime = MuseRuntime(muse_config)
    registry = RuntimeRegistry([runtime])
    state: AgentStateStore | None = None
    initialization_error: str | None = None
    secret = secrets.token_bytes(32)
    if muse_config.enabled:
        try:
            state = AgentStateStore(muse_config.state_db)
            state.mark_sessions_for_recovery("muse")
            secret = _load_cursor_secret(muse_config.cursor_secret_file)
        except (OSError, RuntimeError, sqlite3.DatabaseError) as exc:
            if state is not None:
                state.close()
                state = None
            initialization_error = f"Agent Runtime state initialization failed ({type(exc).__name__})."
    return AgentService(
        registry=registry,
        state=state,
        workspaces=WorkspaceManager(muse_config.workspace_root),
        cursor_codec=CursorCodec(secret),
        initialization_error=initialization_error,
    )


_SERVICE: AgentService | None = None


def get_agent_service() -> AgentService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = build_agent_service()
    return _SERVICE


async def shutdown_agent_service() -> None:
    global _SERVICE
    service, _SERVICE = _SERVICE, None
    if service is not None:
        await service.close()


def _load_cursor_secret(path: Path) -> bytes:
    configured = path.expanduser()
    if configured.is_symlink():
        raise RuntimeError("agent cursor secret must not be a symlink")
    resolved = configured.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if resolved.exists():
        return _read_cursor_secret(resolved)
    value = secrets.token_bytes(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(resolved, flags, 0o600)
    except FileExistsError:
        if configured.is_symlink():
            raise RuntimeError("agent cursor secret is not a private regular file")
        return _read_cursor_secret(resolved)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value)
    return value


def _read_cursor_secret(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("agent cursor secret must be a private regular file") from exc
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise RuntimeError("agent cursor secret must be a private regular file")
        value = handle.read(4097)
    if len(value) < 32:
        raise RuntimeError("agent cursor secret is too short")
    if len(value) > 4096:
        raise RuntimeError("agent cursor secret is too large")
    return value


def _public_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _command_id() -> str:
    generator = getattr(uuid, "uuid7", None)
    if generator is not None:
        return str(generator())
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    value = timestamp_ms << 80
    value |= 0x7 << 76
    value |= secrets.randbits(12) << 64
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    return str(uuid.UUID(int=value))


def _hash_request(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _runtime_status_dict(status: Any) -> dict[str, Any]:
    return {
        "id": status.id,
        "status": status.status,
        "billing_mode": status.billing_mode,
        "auth": status.auth,
        "protocol_fingerprint": status.protocol_fingerprint,
        "active_sessions": status.active_sessions,
        "active_turns": status.active_turns,
        "detail": status.detail,
    }
