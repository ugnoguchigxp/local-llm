from __future__ import annotations

import asyncio
from typing import Any

from agent_runtime.base import AgentModel, EventHandler, NativeEvent, NativeSession, NativeTurn, RuntimeStatus
from agent_runtime.errors import AgentRuntimeError, billing_unverified, runtime_unavailable
from agent_runtime.muse.bridge_client import MuseBridgeClient
from agent_runtime.muse.config import EXPECTED_SDK_VERSION, MuseConfig


class MuseRuntime:
    id = "muse"

    def __init__(self, config: MuseConfig) -> None:
        self.config = config
        self._event_handler: EventHandler | None = None
        self._bridge: MuseBridgeClient | None = None
        self._initialize_lock = asyncio.Lock()
        self._status = RuntimeStatus(id=self.id, status="disabled")
        self._active_sessions: set[str] = set()
        self._active_turns: set[tuple[str, str]] = set()

    def set_event_handler(self, handler: EventHandler) -> None:
        self._event_handler = handler

    async def status(self) -> RuntimeStatus:
        if not self.config.enabled:
            return RuntimeStatus(id=self.id, status="disabled", detail="LOCAL_LLM_MUSE_ENABLED is false")
        failure = self._static_preflight_error()
        if failure is not None:
            return failure
        if self._status.status == "ready" and (self._bridge is None or not self._bridge.running):
            return RuntimeStatus(
                id=self.id,
                status="unavailable",
                billing_mode="subscription",
                auth=self._status.auth,
                protocol_fingerprint=self._status.protocol_fingerprint,
                detail="Muse host exited; existing sessions require explicit resume",
            )
        return RuntimeStatus(
            id=self.id,
            status=self._status.status if self._status.status == "ready" else "configured",
            billing_mode="subscription",
            auth=self._status.auth,
            protocol_fingerprint=self._status.protocol_fingerprint,
            active_sessions=len(self._active_sessions),
            active_turns=len(self._active_turns),
            detail=self._status.detail,
        )

    async def preflight(self) -> RuntimeStatus:
        if not self.config.enabled:
            raise runtime_unavailable("Muse Runtime is disabled.")
        failure = self._static_preflight_error()
        if failure is not None:
            if failure.status == "billing_unverified":
                raise billing_unverified(failure.detail or "Muse subscription billing is unverified.")
            raise runtime_unavailable(failure.detail or "Muse Runtime is unavailable.")
        await self._ensure_initialized()
        return await self.status()

    def _static_preflight_error(self) -> RuntimeStatus | None:
        binary = self.config.resolved_binary()
        if binary is None:
            return RuntimeStatus(id=self.id, status="unavailable", detail="Muse binary was not found")
        if self.config.resolved_node_binary() is None:
            return RuntimeStatus(id=self.id, status="unavailable", detail="Node.js binary was not found")
        if not self.config.bridge_entry.is_file():
            return RuntimeStatus(
                id=self.id,
                status="unavailable",
                detail="Muse bridge is not built; run pnpm run build:muse-bridge",
            )
        if self.config.profile_root is None or not self.config.profile_root.is_dir():
            return RuntimeStatus(id=self.id, status="unavailable", detail="Muse profile root is missing")
        _evidence, evidence_error = self.config.validate_billing_evidence()
        if evidence_error:
            return RuntimeStatus(id=self.id, status="billing_unverified", detail=evidence_error)
        return None

    async def _ensure_initialized(self) -> None:
        if self._bridge is not None and self._bridge.running and self._status.status == "ready":
            return
        async with self._initialize_lock:
            if self._bridge is not None and self._bridge.running and self._status.status == "ready":
                return
            node_binary = self.config.resolved_node_binary()
            muse_binary = self.config.resolved_binary()
            if node_binary is None or muse_binary is None:
                raise runtime_unavailable("Muse or Node.js binary is unavailable.")
            if self._bridge is not None:
                await self._bridge.close()
                self._bridge = None
                self._active_sessions.clear()
                self._active_turns.clear()
            bridge = MuseBridgeClient(
                node_binary=node_binary,
                entrypoint=self.config.bridge_entry.resolve(),
                env=self.config.child_env(),
                request_timeout_ms=self.config.request_timeout_ms,
                event_callback=self._handle_bridge_event,
                debug_log=self.config.debug_log,
            )
            try:
                result = await bridge.request(
                    "runtime.initialize",
                    {
                        "muse_binary": muse_binary,
                        "expected_fingerprint": self.config.expected_fingerprint,
                        "shutdown_timeout_ms": self.config.shutdown_timeout_ms,
                        "approval_timeout_ms": self.config.approval_timeout_ms,
                        "sdk_version": EXPECTED_SDK_VERSION,
                    },
                    timeout_ms=self.config.startup_timeout_ms,
                )
                fingerprint = _validate_initialize_result(
                    result,
                    expected_fingerprint=self.config.expected_fingerprint,
                )
            except Exception:
                await bridge.close()
                raise
            self._bridge = bridge
            self._status = RuntimeStatus(
                id=self.id,
                status="ready",
                billing_mode="subscription",
                auth="verified",
                protocol_fingerprint=fingerprint,
            )

    async def list_models(self) -> list[AgentModel]:
        bridge = await self._ready_bridge()
        result = await bridge.request("models.list")
        rows = result.get("models")
        if not isinstance(rows, list):
            raise _protocol_error("Muse model catalog response is invalid.")
        models: list[AgentModel] = []
        for row in rows:
            if not isinstance(row, dict):
                raise _protocol_error("Muse model catalog contains an invalid entry.")
            provider_id = row.get("providerId")
            model_id = row.get("modelId")
            display = row.get("displayLabel")
            if not all(isinstance(value, str) and value for value in (provider_id, model_id, display)):
                raise _protocol_error("Muse model catalog contains invalid model identity.")
            if provider_id not in self.config.allowed_provider_ids or model_id not in self.config.allowed_models:
                continue
            models.append(
                AgentModel(
                    id=f"muse/{model_id}",
                    runtime=self.id,
                    provider_id=provider_id,
                    native_model_id=model_id,
                    display_name=display,
                    context_limit=_optional_int(row.get("contextLimit")),
                    output_limit=_optional_int(row.get("outputLimit")),
                    capabilities={
                        "sessions": True,
                        "streaming": True,
                        "provider_managed_tools": True,
                        "approvals": True,
                        "resume": True,
                    },
                )
            )
        return models

    async def start_session(
        self,
        *,
        workspace_root: str,
        model_id: str,
        provider_id: str,
        approval_mode: str,
        command_id: str,
    ) -> NativeSession:
        bridge = await self._ready_bridge()
        if len(self._active_sessions) >= self.config.max_sessions:
            raise AgentRuntimeError(
                code="runtime_overloaded",
                message="Muse Runtime has reached its loaded-session limit.",
                status_code=503,
                runtime="muse",
                retryable=True,
            )
        result = await bridge.request(
            "session.start",
            {
                "workspace_root": workspace_root,
                "model_id": model_id,
                "provider_id": provider_id,
                "approval_mode": self._map_approval_mode(approval_mode),
                "command_id": command_id,
            },
        )
        session = _native_session(result, "session.start")
        self._active_sessions.add(session.session_id)
        return session

    def _map_approval_mode(self, public_policy: str) -> str:
        if public_policy != "strict" or not self.config.native_approval_mode:
            raise AgentRuntimeError(
                code="unsupported_capability",
                message="Muse strict approval mode has not been verified.",
                status_code=400,
                runtime="muse",
            )
        return self.config.native_approval_mode

    async def resume_session(
        self,
        *,
        native_session_id: str,
        cursor: str | None,
        command_id: str,
    ) -> NativeSession:
        bridge = await self._ready_bridge()
        if (
            native_session_id not in self._active_sessions
            and len(self._active_sessions) >= self.config.max_sessions
        ):
            raise AgentRuntimeError(
                code="runtime_overloaded",
                message="Muse Runtime has reached its loaded-session limit.",
                status_code=503,
                runtime="muse",
                retryable=True,
            )
        result = await bridge.request(
            "session.resume",
            {
                "native_session_id": native_session_id,
                "cursor": cursor,
                "command_id": command_id,
            },
        )
        session = _native_session(result, "session.resume")
        if session.session_id != native_session_id:
            raise _protocol_error("Muse resumed a different native session.")
        self._active_sessions.add(session.session_id)
        return session

    async def release_session(self, *, native_session_id: str) -> None:
        bridge = await self._ready_bridge()
        self._require_loaded_session(native_session_id)
        await bridge.request("session.release", {"native_session_id": native_session_id})
        self._active_sessions.discard(native_session_id)

    async def start_turn(
        self,
        *,
        native_session_id: str,
        text: str,
        command_id: str,
    ) -> NativeTurn:
        bridge = await self._ready_bridge()
        self._require_loaded_session(native_session_id)
        result = await bridge.request(
            "turn.start",
            {
                "native_session_id": native_session_id,
                "text": text,
                "command_id": command_id,
            },
        )
        turn = _native_turn(result, "turn.start")
        self._active_turns.add((native_session_id, turn.turn_id))
        return turn

    async def cancel_turn(
        self,
        *,
        native_session_id: str,
        native_turn_id: str,
        command_id: str,
    ) -> NativeTurn:
        bridge = await self._ready_bridge()
        self._require_loaded_session(native_session_id)
        result = await bridge.request(
            "turn.cancel",
            {
                "native_session_id": native_session_id,
                "native_turn_id": native_turn_id,
                "command_id": command_id,
            },
        )
        turn = _native_turn(result, "turn.cancel", disposition="cancel_requested")
        if turn.turn_id != native_turn_id:
            raise _protocol_error("Muse cancelled a different native turn.")
        return turn

    async def decide_approval(
        self,
        *,
        native_session_id: str,
        approval_id: str,
        decision: str,
        command_id: str,
    ) -> dict[str, Any]:
        bridge = await self._ready_bridge()
        self._require_loaded_session(native_session_id)
        return await bridge.request(
            "approval.decide",
            {
                "native_session_id": native_session_id,
                "approval_id": approval_id,
                "decision": decision,
                "command_id": command_id,
            },
        )

    async def answer_user_input(
        self,
        *,
        native_session_id: str,
        user_input_id: str,
        answers: list[dict[str, Any]],
        command_id: str,
    ) -> dict[str, Any]:
        bridge = await self._ready_bridge()
        self._require_loaded_session(native_session_id)
        return await bridge.request(
            "user_input.answer",
            {
                "native_session_id": native_session_id,
                "user_input_id": user_input_id,
                "answers": answers,
                "command_id": command_id,
            },
        )

    async def page_events(
        self,
        *,
        native_session_id: str,
        cursor: str | None,
        limit: int = 200,
    ) -> list[NativeEvent]:
        bridge = await self._ready_bridge()
        events: list[NativeEvent] = []
        next_cursor = cursor
        seen_cursors: set[str] = set()
        pages = 0
        while True:
            pages += 1
            if pages > 100:
                raise _protocol_error("Muse event paging exceeded the page limit.")
            result = await bridge.request(
                "events.page",
                {"native_session_id": native_session_id, "cursor": next_cursor, "limit": limit},
            )
            rows = result.get("events")
            if not isinstance(rows, list):
                raise _protocol_error("Muse event page response is invalid.")
            if not all(isinstance(row, dict) for row in rows):
                raise _protocol_error("Muse event page contains an invalid event.")
            for row in rows:
                event = self._native_event(row)
                if event.native_session_id != native_session_id:
                    raise _protocol_error("Muse event page contained a foreign session event.")
                self._track_event(event)
                events.append(event)
            if len(events) > 5000:
                raise AgentRuntimeError(
                    code="event_replay_too_large",
                    message="Muse event replay exceeded the gateway limit.",
                    status_code=413,
                    runtime="muse",
                )
            raw_next = result.get("next_cursor")
            if raw_next is None:
                return events
            if not isinstance(raw_next, str) or raw_next in seen_cursors or raw_next == next_cursor:
                raise _protocol_error("Muse event paging cursor did not advance.")
            seen_cursors.add(raw_next)
            next_cursor = raw_next

    async def _ready_bridge(self) -> MuseBridgeClient:
        await self.preflight()
        if self._bridge is None:
            raise runtime_unavailable("Muse bridge did not initialize.")
        return self._bridge

    def _require_loaded_session(self, native_session_id: str) -> None:
        if native_session_id not in self._active_sessions:
            raise AgentRuntimeError(
                code="agent_session_not_loaded",
                message="The Muse session is not loaded on this host; resume it explicitly.",
                status_code=409,
                runtime="muse",
            )

    async def _handle_bridge_event(self, frame: dict[str, Any]) -> None:
        event = self._native_event(frame)
        self._track_event(event)
        if self._event_handler is not None:
            await self._event_handler(event)

    def _track_event(self, event: NativeEvent) -> None:
        if event.native_turn_id is None:
            return
        if event.event_type == "turn.started":
            self._active_turns.add((event.native_session_id, event.native_turn_id))
        elif event.event_type in {
            "turn.completed",
            "turn.cancelled",
            "turn.failed",
            "turn.unqueued",
        }:
            self._active_turns.discard((event.native_session_id, event.native_turn_id))

    def _native_event(self, frame: dict[str, Any]) -> NativeEvent:
        event_type = _required_string(frame, "type", "Muse event")
        session_id = _required_string(frame, "native_session_id", "Muse event")
        native_cursor = _required_string(frame, "native_cursor", "Muse event")
        turn_id = frame.get("native_turn_id")
        data = frame.get("data")
        if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
            raise _protocol_error("Muse event turn id is invalid.")
        if not isinstance(data, dict):
            raise _protocol_error("Muse event data is invalid.")
        return NativeEvent(
            runtime_id=self.id,
            event_type=event_type,
            native_session_id=session_id,
            native_turn_id=turn_id,
            native_cursor=native_cursor,
            data=data,
        )

    async def close(self) -> None:
        bridge, self._bridge = self._bridge, None
        if bridge is not None:
            await bridge.close()
        self._active_sessions.clear()
        self._active_turns.clear()
        self._status = RuntimeStatus(id=self.id, status="configured")


def _required_string(value: dict[str, Any], key: str, where: str) -> str:
    member = value.get(key)
    if not isinstance(member, str) or not member:
        raise _protocol_error(f"{where} has an invalid {key}.")
    return member


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _protocol_error(message: str) -> AgentRuntimeError:
    return AgentRuntimeError(
        code="runtime_protocol_mismatch",
        message=message,
        status_code=503,
        runtime="muse",
    )


def _validate_initialize_result(
    result: dict[str, Any],
    *,
    expected_fingerprint: str,
) -> str:
    protocol = result.get("bridge_protocol")
    if not isinstance(protocol, int) or isinstance(protocol, bool) or protocol != 1:
        raise _protocol_error("Muse bridge protocol version is unsupported.")
    if _required_string(result, "sdk_version", "runtime.initialize") != EXPECTED_SDK_VERSION:
        raise _protocol_error("Muse bridge SDK version is unsupported.")
    if _required_string(result, "session_durability", "runtime.initialize") != "durable":
        raise _protocol_error("Muse host does not provide durable sessions.")
    fingerprint = _required_string(result, "schema_fingerprint", "runtime.initialize")
    if fingerprint != expected_fingerprint:
        raise _protocol_error(
            "Muse schema fingerprint does not match the verified configuration."
        )
    return fingerprint


def _native_session(result: dict[str, Any], where: str) -> NativeSession:
    status = _required_string(result, "status", where)
    if status not in {"idle", "running"}:
        raise _protocol_error(f"{where} returned an unsupported session status.")
    return NativeSession(
        session_id=_required_string(result, "native_session_id", where),
        view_cursor=_required_string(result, "view_cursor", where),
        status=status,
        model_id=result.get("model_id") if isinstance(result.get("model_id"), str) else None,
        provider_id=result.get("provider_id") if isinstance(result.get("provider_id"), str) else None,
    )


def _native_turn(
    result: dict[str, Any],
    where: str,
    disposition: str | None = None,
) -> NativeTurn:
    status = _required_string(result, "status", where)
    if status != "accepted":
        raise _protocol_error(f"{where} returned an unsupported command status.")
    native_disposition = disposition or _required_string(result, "disposition", where)
    if disposition is None and native_disposition != "started":
        raise _protocol_error(f"{where} did not start a fresh turn.")
    return NativeTurn(
        turn_id=_required_string(result, "native_turn_id", where),
        status=status,
        disposition=native_disposition,
    )
