from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import tomllib
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agent_runtime.base import (
    AgentModel,
    EventHandler,
    NativeEvent,
    NativeSession,
    NativeTurn,
    RuntimeStatus,
)
from agent_runtime.errors import AgentRuntimeError, billing_unverified, runtime_unavailable
from agent_runtime.grok.acp_client import GrokAcpClient
from agent_runtime.grok.config import AUTH_METHOD, GrokBillingEvidence, GrokConfig
from agent_runtime.grok.error_mapping import grok_error, protocol_error
from agent_runtime.grok.event_mapping import map_session_update, permission_summary
from agent_runtime.grok.json_utils import loads_strict


ClientFactory = Callable[..., GrokAcpClient]
_VERSION_RE = re.compile(r"\bgrok\s+([^\s]+)", re.IGNORECASE)
_ALLOW_ONCE_KINDS = {"allow_once"}
_DENY_KINDS = {"reject_once"}
_TERMINAL_STOP_REASONS = {"end_turn", "max_tokens", "max_turn_requests", "refusal"}
_RECOVERY_ERRORS = {
    "provider_host_exited",
    "provider_timeout",
    "runtime_protocol_mismatch",
    "provider_response_too_large",
}
_HISTORY_LIMIT = 2000
_CONTROL_PROMPT = re.compile(r"^\s*(?:/[A-Za-z][A-Za-z0-9_-]*(?:\s|$)|!)")


@dataclass
class _PendingPermission:
    request_id: int | str
    approval_id: str
    turn_id: str
    options: tuple[dict[str, str], ...]
    allow_once_safe: bool
    timeout_task: asyncio.Task[None] | None = None


@dataclass
class _SessionHost:
    client: GrokAcpClient
    workspace_root: str
    model_id: str
    provider_id: str
    native_session_id: str | None = None
    cursor: int = 0
    history: deque[NativeEvent] = field(default_factory=lambda: deque(maxlen=_HISTORY_LIMIT))
    active_turn_id: str | None = None
    prompt_task: asyncio.Task[None] | None = None
    cancel_task: asyncio.Task[None] | None = None
    cancel_closing: bool = False
    pending_permissions: dict[str, _PendingPermission] = field(default_factory=dict)
    loading: bool = False
    loading_session_id: str | None = None
    protocol_fingerprint: str | None = None


class GrokRuntime:
    id = "grok"

    def __init__(
        self,
        config: GrokConfig,
        *,
        client_factory: ClientFactory = GrokAcpClient,
    ) -> None:
        self.config = config
        self._client_factory = client_factory
        self._event_handler: EventHandler | None = None
        self._hosts: dict[str, _SessionHost] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._catalog: list[AgentModel] | None = None
        self._protocol_fingerprint: str | None = None
        self._host_version: str | None = None
        self._auth = "unverified"
        self._detail: str | None = None
        self._preflight_state: str | None = None

    def set_event_handler(self, handler: EventHandler) -> None:
        self._event_handler = handler

    async def status(self) -> RuntimeStatus:
        if not self.config.enabled:
            return RuntimeStatus(
                id=self.id,
                status="disabled",
                protocol_name="acp",
                protocol_version=str(self.config.acp_protocol_version),
                detail="LOCAL_LLM_GROK_ENABLED is false",
            )
        static_error = self.config.validate_static()
        evidence, evidence_error = self.config.validate_billing_evidence()
        if static_error:
            state = "unavailable"
            detail = static_error
        elif evidence_error:
            state = "billing_unverified"
            detail = evidence_error
        elif self._auth == "required":
            state = "auth_required"
            detail = self._detail
        elif self._preflight_state is not None:
            state = self._preflight_state
            detail = self._detail
        elif self._protocol_fingerprint:
            if any(not host.client.running for host in self._hosts.values()):
                state = "degraded"
                detail = "A Grok ACP process exited; its session requires explicit resume"
            else:
                state = "ready"
                detail = self._detail
        else:
            state = "configured"
            detail = self._detail
        assurance = evidence.billing_assurance if evidence is not None else "unverified"
        return RuntimeStatus(
            id=self.id,
            status=state,
            billing_mode="subscription",
            billing_assurance=assurance,
            auth=self._auth,
            protocol_fingerprint=self._protocol_fingerprint,
            protocol_name="acp",
            protocol_version=str(self.config.acp_protocol_version),
            host_version=self._host_version or self.config.expected_version or None,
            active_sessions=len(self._hosts),
            active_turns=sum(host.active_turn_id is not None for host in self._hosts.values()),
            detail=detail,
        )

    async def preflight(self) -> RuntimeStatus:
        if not self.config.enabled:
            raise runtime_unavailable("Grok Runtime is disabled.", runtime="grok")
        try:
            return await self._run_preflight()
        except AgentRuntimeError as exc:
            self._detail = exc.message
            if exc.code == "runtime_auth_required":
                self._auth = "required"
                self._preflight_state = None
            else:
                if self._auth == "required":
                    self._auth = "unverified"
                if exc.code in {
                    "runtime_unavailable",
                    "runtime_billing_unverified",
                    "runtime_configuration_unsafe",
                }:
                    self._preflight_state = "unavailable"
                else:
                    self._preflight_state = "degraded"
            raise

    async def _run_preflight(self) -> RuntimeStatus:
        static_error = self.config.validate_static()
        if static_error:
            raise runtime_unavailable(static_error, runtime="grok")
        evidence, evidence_error = self.config.validate_billing_evidence()
        if evidence_error or evidence is None:
            raise billing_unverified(
                evidence_error or "Grok subscription billing is unverified.", runtime="grok"
            )
        version = await self._read_version()
        if version != self.config.expected_version:
            raise protocol_error(
                f"Grok CLI version {version!r} does not match the pinned version."
            )
        await self._inspect_profile(self.config.profile_root or Path.cwd())
        client = self._new_client(
            workspace_root=str((self.config.profile_root or Path.cwd()).resolve()),
            model_id=self.config.allowed_models[0],
        )
        try:
            await client.start()
            initialized = await client.initialize(
                self.config.acp_protocol_version, self.config.startup_timeout_ms
            )
            fingerprint, models = self._validate_initialize(
                initialized,
                evidence,
                expected_model_id=self.config.allowed_models[0],
            )
            _require_result_object(
                await client.request(
                    "authenticate",
                    {"methodId": AUTH_METHOD},
                    timeout_ms=self.config.startup_timeout_ms,
                ),
                "authenticate",
            )
        finally:
            await client.close()
        self._catalog = models
        self._protocol_fingerprint = fingerprint
        self._host_version = version
        self._auth = "verified"
        self._preflight_state = None
        self._detail = None
        return await self.status()

    async def list_models(self) -> list[AgentModel]:
        await self.preflight()
        return list(self._catalog or [])

    async def start_session(
        self,
        *,
        workspace_root: str,
        model_id: str,
        provider_id: str,
        approval_mode: str,
        command_id: str,
    ) -> NativeSession:
        del command_id
        if approval_mode != "strict":
            raise grok_error(
                "Grok Runtime only supports the Gateway strict approval policy.",
                code="unsupported_capability",
                status_code=400,
            )
        if provider_id != AUTH_METHOD or model_id not in self.config.allowed_models:
            raise protocol_error("The requested Grok model identity is not allowed.")
        await self.preflight()
        async with self._lifecycle_lock:
            if len(self._hosts) >= self.config.max_sessions:
                raise grok_error(
                    "Grok Runtime has reached its loaded-session limit.",
                    code="runtime_overloaded",
                    retryable=True,
                )
            host = await self._open_host(workspace_root, model_id, provider_id)
            try:
                host.loading = True
                result = await host.client.request(
                    "session/new",
                    {"cwd": host.workspace_root, "mcpServers": []},
                    timeout_ms=self.config.startup_timeout_ms,
                )
                native_session_id = _required_result_string(result, "sessionId", "session/new")
                if (
                    host.loading_session_id is not None
                    and host.loading_session_id != native_session_id
                ):
                    raise protocol_error(
                        "Grok session/new notifications used another session id."
                    )
                host.native_session_id = native_session_id
                if native_session_id in self._hosts:
                    raise protocol_error("Grok reused an already-loaded session id.")
                self._hosts[native_session_id] = host
            except BaseException:
                await host.client.close()
                raise
            finally:
                host.loading = False
        return NativeSession(
            session_id=native_session_id,
            view_cursor="0",
            status="idle",
            model_id=model_id,
            provider_id=provider_id,
        )

    async def resume_session(
        self,
        *,
        native_session_id: str,
        cursor: str | None,
        command_id: str,
        workspace_root: str | None = None,
        model_id: str | None = None,
        provider_id: str | None = None,
    ) -> NativeSession:
        del command_id
        if not workspace_root or not model_id or not provider_id:
            raise protocol_error("Grok resume requires persisted workspace and model identity.")
        if provider_id != AUTH_METHOD or model_id not in self.config.allowed_models:
            raise protocol_error("The resumed Grok model identity is not allowed.")
        await self.preflight()
        initial_cursor = _parse_cursor(cursor)
        async with self._lifecycle_lock:
            existing = self._hosts.get(native_session_id)
            if existing is not None:
                if existing.active_turn_id is not None and existing.client.running:
                    raise grok_error(
                        "An active Grok turn cannot be resumed.",
                        code="agent_session_conflict",
                        status_code=409,
                    )
                self._hosts.pop(native_session_id, None)
                if existing.prompt_task is not None and not existing.prompt_task.done():
                    existing.prompt_task.cancel()
                await existing.client.close()
            if len(self._hosts) >= self.config.max_sessions:
                raise grok_error(
                    "Grok Runtime has reached its loaded-session limit.",
                    code="runtime_overloaded",
                    retryable=True,
                )
            host = await self._open_host(workspace_root, model_id, provider_id)
            host.native_session_id = native_session_id
            host.cursor = initial_cursor
            try:
                host.loading = True
                _require_result_object(
                    await host.client.request(
                        "session/resume",
                        {
                            "sessionId": native_session_id,
                            "cwd": host.workspace_root,
                            "mcpServers": [],
                        },
                        timeout_ms=self.config.startup_timeout_ms,
                    ),
                    "session/resume",
                )
                self._hosts[native_session_id] = host
            except BaseException:
                await host.client.close()
                raise
            finally:
                host.loading = False
        return NativeSession(
            native_session_id,
            str(host.cursor),
            "idle",
            model_id,
            provider_id,
        )

    async def release_session(self, *, native_session_id: str) -> None:
        async with self._lifecycle_lock:
            host = self._hosts.get(native_session_id)
            if host is None:
                raise _not_loaded()
            if host.active_turn_id is not None:
                raise grok_error(
                    "An active Grok turn cannot be released.",
                    code="agent_session_conflict",
                    status_code=409,
                )
            try:
                _require_result_object(
                    await host.client.request(
                        "session/close",
                        {"sessionId": native_session_id},
                        timeout_ms=self.config.shutdown_timeout_ms,
                    ),
                    "session/close",
                )
            finally:
                self._hosts.pop(native_session_id, None)
                await host.client.close()

    async def start_turn(
        self,
        *,
        native_session_id: str,
        text: str,
        command_id: str,
    ) -> NativeTurn:
        host = self._require_host(native_session_id)
        if _CONTROL_PROMPT.match(text):
            raise grok_error(
                "Grok slash commands and shell prompts are unavailable through the Gateway.",
                code="unsupported_prompt_control",
                status_code=400,
            )
        await self.preflight()
        await self._inspect_profile(Path(host.workspace_root))
        if host.protocol_fingerprint != self._protocol_fingerprint:
            raise protocol_error("Grok ACP compatibility changed for the loaded session.")
        if host.active_turn_id is not None:
            raise grok_error(
                "The Grok session already has an active turn.",
                code="agent_session_conflict",
                status_code=409,
            )
        turn_id = f"grok-turn-{command_id}"
        host.active_turn_id = turn_id
        try:
            await self._emit(host, "turn.started", turn_id, {})
        except BaseException:
            host.active_turn_id = None
            raise
        host.prompt_task = asyncio.create_task(
            self._run_prompt(host, turn_id, text), name=f"grok-prompt-{native_session_id}"
        )
        return NativeTurn(turn_id=turn_id, status="accepted", disposition="started")

    async def cancel_turn(
        self,
        *,
        native_session_id: str,
        native_turn_id: str,
        command_id: str,
    ) -> NativeTurn:
        del command_id
        host = self._require_host(native_session_id)
        if host.active_turn_id != native_turn_id:
            raise grok_error(
                "The Grok turn is not active.", code="agent_turn_conflict", status_code=409
            )
        await host.client.notify("session/cancel", {"sessionId": native_session_id})
        for pending in tuple(host.pending_permissions.values()):
            await self._settle_permission(host, pending, option_id=None, reason="cancelled")
        if host.cancel_task is None or host.cancel_task.done():
            host.cancel_task = asyncio.create_task(
                self._cancel_watchdog(host, native_turn_id),
                name=f"grok-cancel-{native_session_id}",
            )
        return NativeTurn(native_turn_id, "accepted", "cancel_requested")

    async def decide_approval(
        self,
        *,
        native_session_id: str,
        approval_id: str,
        decision: str,
        command_id: str,
    ) -> dict[str, Any]:
        del command_id
        host = self._require_host(native_session_id)
        pending = host.pending_permissions.get(approval_id)
        if pending is None:
            raise grok_error(
                "The Grok approval request is no longer pending.",
                code="approval_not_found",
                status_code=404,
            )
        if decision not in {"allow_once", "deny"}:
            raise grok_error(
                "The Grok approval decision is invalid.",
                code="invalid_approval_decision",
                status_code=400,
            )
        if decision == "allow_once" and not pending.allow_once_safe:
            raise grok_error(
                "Grok did not provide enough tool input to approve this operation safely.",
                code="approval_context_unavailable",
                status_code=409,
            )
        if decision == "allow_once":
            await self.preflight()
            await self._inspect_profile(Path(host.workspace_root))
            if host.protocol_fingerprint != self._protocol_fingerprint:
                raise protocol_error("Grok ACP compatibility changed for the loaded session.")
            if host.pending_permissions.get(approval_id) is not pending:
                raise grok_error(
                    "The Grok approval request is no longer pending.",
                    code="approval_not_found",
                    status_code=404,
                )
        allowed_kinds = _ALLOW_ONCE_KINDS if decision == "allow_once" else _DENY_KINDS
        option = next((item for item in pending.options if item["kind"] in allowed_kinds), None)
        if option is None:
            raise protocol_error(
                f"Grok did not provide a safe option for the {decision!r} decision."
            )
        await self._settle_permission(
            host,
            pending,
            option_id=option["option_id"],
            reason="approved" if decision == "allow_once" else "denied",
        )
        return {"status": "accepted"}

    async def answer_user_input(
        self,
        *,
        native_session_id: str,
        user_input_id: str,
        answers: list[dict[str, Any]],
        command_id: str,
    ) -> dict[str, Any]:
        del native_session_id, user_input_id, answers, command_id
        raise grok_error(
            "Grok ACP user-input requests are not enabled in this release.",
            code="unsupported_capability",
            status_code=400,
        )

    async def page_events(
        self,
        *,
        native_session_id: str,
        cursor: str | None,
        limit: int = 200,
    ) -> list[NativeEvent]:
        host = self._require_host(native_session_id)
        after = _parse_cursor(cursor)
        history = list(host.history)
        if not history:
            if after == host.cursor:
                return []
            raise _cursor_expired()
        oldest = int(history[0].native_cursor)
        if after < oldest - 1 or after > host.cursor:
            raise _cursor_expired()
        return [event for event in history if int(event.native_cursor) > after][:limit]

    async def close(self) -> None:
        async with self._lifecycle_lock:
            hosts = list(self._hosts.values())
            self._hosts.clear()
        background_tasks = [
            task
            for host in hosts
            for task in (host.prompt_task, host.cancel_task)
            if task is not None
        ]
        for host in hosts:
            if host.active_turn_id is not None and host.client.running:
                try:
                    await host.client.notify(
                        "session/cancel", {"sessionId": host.native_session_id}
                    )
                except AgentRuntimeError:
                    pass
            for pending in tuple(host.pending_permissions.values()):
                try:
                    await self._settle_permission(
                        host, pending, option_id=None, reason="cancelled"
                    )
                except AgentRuntimeError:
                    pass
            if host.prompt_task is not None and not host.prompt_task.done():
                host.prompt_task.cancel()
            if host.cancel_task is not None and not host.cancel_task.done():
                host.cancel_task.cancel()
            await host.client.close()
        await asyncio.gather(*background_tasks, return_exceptions=True)

    async def _open_host(
        self, workspace_root: str, model_id: str, provider_id: str
    ) -> _SessionHost:
        workspace = str(Path(workspace_root).resolve())
        await self._inspect_profile(Path(workspace))
        host: _SessionHost

        async def notification(method: str, params: dict[str, Any]) -> None:
            await self._handle_notification(host, method, params)

        async def request(
            request_id: int | str, method: str, params: dict[str, Any]
        ) -> None:
            await self._handle_request(host, request_id, method, params)

        client = self._client_factory(
            command=self.config.command(workspace_root=workspace, model_id=model_id),
            env=self.config.child_env(),
            request_timeout_ms=self.config.request_timeout_ms,
            shutdown_timeout_ms=self.config.shutdown_timeout_ms,
            max_frame_bytes=self.config.max_frame_bytes,
            notification_callback=notification,
            request_callback=request,
            debug_log=self.config.debug_log,
        )
        host = _SessionHost(client, workspace, model_id, provider_id)
        try:
            await client.start()
            initialized = await client.initialize(
                self.config.acp_protocol_version, self.config.startup_timeout_ms
            )
            evidence, evidence_error = self.config.validate_billing_evidence()
            if evidence_error or evidence is None:
                raise billing_unverified(
                    evidence_error or "Grok billing evidence became invalid.", runtime="grok"
                )
            fingerprint, models = self._validate_initialize(
                initialized, evidence, expected_model_id=model_id
            )
            if fingerprint != self._protocol_fingerprint:
                raise protocol_error("Grok ACP compatibility changed after preflight.")
            if model_id not in {model.native_model_id for model in models}:
                raise protocol_error("The selected Grok model disappeared after preflight.")
            host.protocol_fingerprint = fingerprint
            _require_result_object(
                await client.request(
                    "authenticate",
                    {"methodId": AUTH_METHOD},
                    timeout_ms=self.config.startup_timeout_ms,
                ),
                "authenticate",
            )
            self._auth = "verified"
        except BaseException:
            await client.close()
            raise
        return host

    def _new_client(self, *, workspace_root: str, model_id: str) -> GrokAcpClient:
        async def ignore_notification(method: str, params: dict[str, Any]) -> None:
            del method, params

        async def reject_request(
            request_id: int | str, method: str, params: dict[str, Any]
        ) -> None:
            del request_id, params
            raise protocol_error(f"Grok requested {method!r} during compatibility probing.")

        return self._client_factory(
            command=self.config.command(workspace_root=workspace_root, model_id=model_id),
            env=self.config.child_env(),
            request_timeout_ms=self.config.request_timeout_ms,
            shutdown_timeout_ms=self.config.shutdown_timeout_ms,
            max_frame_bytes=self.config.max_frame_bytes,
            notification_callback=ignore_notification,
            request_callback=reject_request,
            debug_log=self.config.debug_log,
        )

    async def _run_prompt(self, host: _SessionHost, turn_id: str, text: str) -> None:
        try:
            result = await host.client.request(
                "session/prompt",
                {
                    "sessionId": host.native_session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
                timeout_ms=self.config.turn_timeout_ms,
            )
            if not isinstance(result, dict):
                raise protocol_error("Grok prompt response must be an object.")
            reason = result.get("stopReason")
            if not isinstance(reason, str):
                raise protocol_error("Grok prompt response has no stopReason.")
            if reason == "cancelled":
                await self._finish_turn(host, turn_id, "turn.cancelled", {"terminal": reason})
            elif reason in _TERMINAL_STOP_REASONS:
                await self._finish_turn(host, turn_id, "turn.completed", {"terminal": reason})
            else:
                raise protocol_error("Grok prompt response has an unknown stopReason.")
        except asyncio.CancelledError:
            return
        except AgentRuntimeError as exc:
            requires_recovery = exc.code in _RECOVERY_ERRORS
            if requires_recovery:
                if exc.code == "provider_timeout":
                    try:
                        await host.client.notify(
                            "session/cancel", {"sessionId": host.native_session_id}
                        )
                    except AgentRuntimeError:
                        pass
                try:
                    await host.client.close()
                except AgentRuntimeError:
                    pass
            finished = await self._finish_turn(
                host,
                turn_id,
                "turn.failed",
                {"terminal": "failed", "error": {"code": exc.code, "message": exc.message}},
            )
            if requires_recovery and finished:
                await self._emit(
                    host,
                    "session.recovery_required",
                    None,
                    {"reason": exc.code},
                )
        except Exception as exc:
            await self._finish_turn(
                host,
                turn_id,
                "turn.failed",
                {
                    "terminal": "failed",
                    "error": {
                        "code": "provider_request_failed",
                        "message": type(exc).__name__,
                    },
                },
            )

    async def _cancel_watchdog(self, host: _SessionHost, turn_id: str) -> None:
        try:
            await asyncio.sleep(self.config.shutdown_timeout_ms / 1000)
            if host.active_turn_id != turn_id:
                return
            host.cancel_closing = True
            try:
                await host.client.close()
            finally:
                host.cancel_closing = False
            finished = await self._finish_turn(
                host,
                turn_id,
                "turn.cancelled",
                {"terminal": "forced_cancel", "provider_confirmed": False},
            )
            if finished:
                await self._emit(
                    host,
                    "session.recovery_required",
                    None,
                    {"reason": "provider_cancel_timeout"},
                )
        except asyncio.CancelledError:
            return
        finally:
            if host.cancel_task is asyncio.current_task():
                host.cancel_task = None

    async def _finish_turn(
        self,
        host: _SessionHost,
        turn_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> bool:
        if host.active_turn_id != turn_id:
            return False
        for pending in tuple(host.pending_permissions.values()):
            try:
                await self._settle_permission(
                    host, pending, option_id=None, reason="cancelled"
                )
            except AgentRuntimeError:
                pass
        host.active_turn_id = None
        host.prompt_task = None
        current = asyncio.current_task()
        if host.cancel_task is current:
            host.cancel_task = None
        elif host.cancel_task is not None and not host.cancel_closing:
            host.cancel_task.cancel()
            host.cancel_task = None
        await self._emit(host, event_type, turn_id, data)
        return True

    async def _handle_notification(
        self, host: _SessionHost, method: str, params: dict[str, Any]
    ) -> None:
        if method != "session/update":
            return
        session_id = params.get("sessionId")
        if host.loading:
            if not isinstance(session_id, str) or not session_id:
                raise protocol_error("Grok emitted a loading update without a session id.")
            if host.native_session_id is None:
                if host.loading_session_id is None:
                    host.loading_session_id = session_id
                elif host.loading_session_id != session_id:
                    raise protocol_error(
                        "Grok emitted loading updates for multiple sessions."
                    )
            elif session_id != host.native_session_id:
                raise protocol_error("Grok emitted an update for a foreign session.")
            update = params.get("update")
            map_session_update(update)
            if (
                isinstance(update, dict)
                and update.get("sessionUpdate") == "current_model_update"
                and update.get("modelId") != host.model_id
            ):
                raise protocol_error(
                    "Grok selected another model while loading the session."
                )
            return
        if host.native_session_id is None or session_id != host.native_session_id:
            raise protocol_error("Grok emitted an update for a foreign session.")
        if host.active_turn_id is None:
            raise protocol_error("Grok emitted a session update without an active turn.")
        mapped = map_session_update(params.get("update"))
        if mapped is not None:
            if mapped.event_type == "session.invariant_changed":
                turn_id = host.active_turn_id
                await host.client.notify(
                    "session/cancel", {"sessionId": host.native_session_id}
                )
                for pending in tuple(host.pending_permissions.values()):
                    await self._settle_permission(
                        host, pending, option_id=None, reason="cancelled"
                    )
                await host.client.close()
                await self._finish_turn(
                    host,
                    turn_id,
                    "turn.failed",
                    {
                        "terminal": "failed",
                        "error": {
                            "code": "session_invariant_changed",
                            "message": "Grok changed a protected session invariant.",
                        },
                    },
                )
                await self._emit(host, mapped.event_type, turn_id, mapped.data)
                return
            await self._emit(host, mapped.event_type, host.active_turn_id, mapped.data)

    async def _handle_request(
        self,
        host: _SessionHost,
        request_id: int | str,
        method: str,
        params: dict[str, Any],
    ) -> None:
        if method != "session/request_permission":
            await host.client.respond(
                request_id,
                error={"code": -32601, "message": "Method not supported by local-llm"},
            )
            return
        session_id, options, subject, allow_once_safe = permission_summary(params)
        if host.native_session_id is None or session_id != host.native_session_id:
            await host.client.respond(
                request_id,
                result={"outcome": {"outcome": "cancelled"}},
            )
            raise protocol_error("Grok requested permission for a foreign session.")
        if host.active_turn_id is None:
            await host.client.respond(
                request_id,
                result={"outcome": {"outcome": "cancelled"}},
            )
            raise protocol_error("Grok requested permission without an active turn.")
        if any(
            pending.request_id == request_id for pending in host.pending_permissions.values()
        ):
            await host.client.respond(
                request_id,
                result={"outcome": {"outcome": "cancelled"}},
            )
            raise protocol_error("Grok reused a pending permission request id.")
        safe_kinds = {item["kind"] for item in options}
        if not safe_kinds.intersection(_DENY_KINDS):
            await host.client.respond(
                request_id,
                result={"outcome": {"outcome": "cancelled"}},
            )
            raise protocol_error("Grok permission request has no one-time deny option.")
        approval_id = f"apr_{uuid.uuid4().hex}"
        pending = _PendingPermission(
            request_id=request_id,
            approval_id=approval_id,
            turn_id=host.active_turn_id,
            options=tuple(options),
            allow_once_safe=allow_once_safe,
        )
        host.pending_permissions[approval_id] = pending
        pending.timeout_task = asyncio.create_task(
            self._permission_timeout(host, pending), name=f"grok-approval-{approval_id}"
        )
        await self._emit(
            host,
            "approval.requested",
            pending.turn_id,
            {
                "approval_id": approval_id,
                "subject": subject,
                "available_choices": [
                    choice
                    for choice, kinds in (
                        ("allow_once", _ALLOW_ONCE_KINDS),
                        ("deny", _DENY_KINDS),
                    )
                    if safe_kinds.intersection(kinds)
                    and (choice != "allow_once" or allow_once_safe)
                ],
            },
        )

    async def _permission_timeout(
        self, host: _SessionHost, pending: _PendingPermission
    ) -> None:
        try:
            await asyncio.sleep(self.config.approval_timeout_ms / 1000)
            if pending.approval_id in host.pending_permissions:
                await self._settle_permission(
                    host, pending, option_id=None, reason="timeout"
                )
        except asyncio.CancelledError:
            return
        except AgentRuntimeError:
            return

    async def _settle_permission(
        self,
        host: _SessionHost,
        pending: _PendingPermission,
        *,
        option_id: str | None,
        reason: str,
    ) -> None:
        if host.pending_permissions.pop(pending.approval_id, None) is None:
            return
        current = asyncio.current_task()
        if pending.timeout_task is not None and pending.timeout_task is not current:
            pending.timeout_task.cancel()
        outcome = (
            {"outcome": "selected", "optionId": option_id}
            if option_id is not None
            else {"outcome": "cancelled"}
        )
        await host.client.respond(pending.request_id, result={"outcome": outcome})
        await self._emit(
            host,
            "approval.resolved",
            pending.turn_id,
            {
                "approval_id": pending.approval_id,
                "decision": "approved" if reason == "approved" else "denied",
                "reason": reason,
            },
        )

    async def _emit(
        self,
        host: _SessionHost,
        event_type: str,
        turn_id: str | None,
        data: dict[str, Any],
    ) -> None:
        if host.native_session_id is None:
            raise protocol_error("Grok event arrived before session initialization.")
        host.cursor += 1
        event = NativeEvent(
            runtime_id=self.id,
            event_type=event_type,
            native_session_id=host.native_session_id,
            native_turn_id=turn_id,
            native_cursor=str(host.cursor),
            data=data,
        )
        host.history.append(event)
        if self._event_handler is not None:
            await self._event_handler(event)

    async def _read_version(self) -> str:
        binary = self.config.resolved_binary()
        if binary is None:
            raise runtime_unavailable("Grok binary was not found.", runtime="grok")
        try:
            returncode, stdout, _stderr = await self._run_bounded_command(
                [binary, "--version"], stdout_limit=4096
            )
        except (OSError, TimeoutError) as exc:
            raise runtime_unavailable(
                "Unable to read the Grok CLI version.", runtime="grok"
            ) from exc
        match = _VERSION_RE.search(stdout.decode("utf-8", errors="replace"))
        if returncode != 0 or match is None:
            raise protocol_error("Grok CLI returned an invalid version response.")
        return match.group(1)

    async def _inspect_profile(self, cwd: Path) -> None:
        binary = self.config.resolved_binary()
        if binary is None:
            raise runtime_unavailable("Grok binary was not found.", runtime="grok")
        try:
            returncode, stdout, _stderr = await self._run_bounded_command(
                [
                    binary,
                    "--cwd",
                    str(cwd.resolve()),
                    "--no-auto-update",
                    "inspect",
                    "--json",
                ],
                stdout_limit=self.config.max_frame_bytes,
            )
        except (OSError, TimeoutError) as exc:
            raise runtime_unavailable(
                "Grok configuration inspection failed.", runtime="grok"
            ) from exc
        if returncode != 0:
            raise runtime_unavailable("Grok configuration inspection failed.", runtime="grok")
        try:
            inspection = loads_strict(stdout)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise protocol_error("Grok inspect returned invalid JSON.") from exc
        self._validate_inspection(inspection)

    async def _run_bounded_command(
        self,
        command: list[str],
        *,
        stdout_limit: int,
    ) -> tuple[int, bytes, bytes]:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.config.child_env(),
        )
        assert process.stdout is not None and process.stderr is not None

        async def read_limited(
            stream: asyncio.StreamReader, limit: int, stream_name: str
        ) -> bytes:
            output = bytearray()
            while True:
                chunk = await stream.read(min(65_536, limit + 1 - len(output)))
                if not chunk:
                    return bytes(output)
                output.extend(chunk)
                if len(output) > limit:
                    raise grok_error(
                        f"Grok {stream_name} exceeded the safe output limit.",
                        code="provider_response_too_large",
                        status_code=502,
                    )

        tasks = [
            asyncio.create_task(process.wait()),
            asyncio.create_task(read_limited(process.stdout, stdout_limit, "stdout")),
            asyncio.create_task(read_limited(process.stderr, 64 * 1024, "stderr")),
        ]
        try:
            returncode, stdout, stderr = await asyncio.wait_for(
                asyncio.gather(*tasks), timeout=self.config.startup_timeout_ms / 1000
            )
            return returncode, stdout, stderr
        except BaseException:
            for task in tasks:
                task.cancel()
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=1)
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    def _validate_inspection(self, inspection: Any) -> None:
        if not isinstance(inspection, dict):
            raise protocol_error("Grok inspect response must be an object.")
        if inspection.get("grokVersion") != self.config.expected_version:
            raise protocol_error("Grok inspect reported an unexpected CLI version.")
        for field in (
            "projectInstructions",
            "hooks",
            "skills",
            "plugins",
            "marketplaces",
            "mcpServers",
            "lspServers",
        ):
            if inspection.get(field) != []:
                raise grok_error(
                    f"Grok configuration inspection found unsupported {field}.",
                    code="runtime_configuration_unsafe",
                )
        agents = inspection.get("agents")
        if not isinstance(agents, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("source"), dict)
            or item["source"].get("type") != "builtin"
            for item in agents
        ):
            raise grok_error(
                "Grok configuration inspection found a custom agent.",
                code="runtime_configuration_unsafe",
            )
        login = inspection.get("loginPolicy")
        if not isinstance(login, dict) or login.get("apiKeyAuthDisabled") is not True:
            raise billing_unverified(
                "Grok API-key authentication is not disabled by requirements.toml.",
                runtime="grok",
            )
        permissions = inspection.get("permissions")
        if not isinstance(permissions, dict) or any(
            (
                permissions.get("loaded") != 0,
                permissions.get("skipped") != [],
                permissions.get("sources") != [],
                permissions.get("mcpServerAllowlist") != [],
                permissions.get("marketplaceAllowlist") != [],
                permissions.get("managedSettingsActive") is not False,
            )
        ):
            raise grok_error(
                "Grok configuration inspection found inherited permission rules.",
                code="runtime_configuration_unsafe",
            )
        config_sources = inspection.get("configSources")
        layers = config_sources.get("layers") if isinstance(config_sources, dict) else None
        if not isinstance(layers, list):
            raise protocol_error("Grok inspect omitted configuration sources.")
        profile = (self.config.profile_root or Path("/")).resolve()
        allowed_names = {"config.toml", "requirements.toml", "managed_config.toml"}
        system_requirements = Path("/etc/grok/requirements.toml")
        system_requirements_resolved = system_requirements.resolve()
        for layer in layers:
            path_value = layer.get("path") if isinstance(layer, dict) else None
            if not isinstance(path_value, str):
                raise protocol_error("Grok inspect returned an invalid configuration layer.")
            path = Path(path_value).resolve()
            if path == system_requirements_resolved:
                self._validate_system_requirements(system_requirements)
            elif path.parent != profile / ".grok" or path.name not in allowed_names:
                raise grok_error(
                    "Grok discovered a configuration layer outside its dedicated profile.",
                    code="runtime_configuration_unsafe",
                )
        external = inspection.get("externalCompat")
        if (
            not isinstance(external, dict)
            or external.get("remoteSettingsLoaded") is not False
        ):
            raise grok_error(
                "Grok loaded external compatibility settings.",
                code="runtime_configuration_unsafe",
            )
        self._validate_config_files()

    def _validate_config_files(self) -> None:
        if self.config.profile_root is None:
            raise protocol_error("Grok profile root is unavailable.")
        grok_home = self.config.profile_root.resolve() / ".grok"
        try:
            invalid_home = grok_home.is_symlink() or not grok_home.is_dir()
            grok_home_metadata = grok_home.stat()
        except OSError as exc:
            raise grok_error(
                "Grok home is unavailable.",
                code="runtime_configuration_unsafe",
            ) from exc
        if invalid_home:
            raise grok_error(
                "Grok home must be a non-symlink directory in the dedicated profile.",
                code="runtime_configuration_unsafe",
            )
        if grok_home_metadata.st_uid != os.getuid():
            raise grok_error(
                "Grok home must be owned by the current user.",
                code="runtime_configuration_unsafe",
            )
        requirements = grok_home / "requirements.toml"
        config = grok_home / "config.toml"
        for path in (requirements, config, grok_home / "managed_config.toml"):
            try:
                is_symlink = path.is_symlink()
                exists = path.exists()
                invalid_file = exists and (
                    not path.is_file() or path.stat().st_size > 64 * 1024
                )
            except OSError as exc:
                raise grok_error(
                    "Grok configuration file is unavailable.",
                    code="runtime_configuration_unsafe",
                ) from exc
            if is_symlink:
                raise grok_error(
                    "Grok configuration files must not be symlinks.",
                    code="runtime_configuration_unsafe",
                )
            if invalid_file:
                raise grok_error(
                    "Grok configuration file is invalid or too large.",
                    code="runtime_configuration_unsafe",
                )
        try:
            requirement_data = tomllib.loads(requirements.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise grok_error(
                "Grok requirements.toml is missing or invalid.",
                code="runtime_configuration_unsafe",
            ) from exc
        expected_requirements = {
            "grok_com_config": {"disable_api_key_auth": True},
            "sandbox": {"profile": "workspace"},
        }
        if requirement_data != expected_requirements:
            raise grok_error(
                "Grok requirements.toml differs from the verified policy.",
                code="runtime_configuration_unsafe",
            )
        if config.exists():
            try:
                config_data = tomllib.loads(config.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
                raise grok_error(
                    "Grok config.toml is invalid.", code="runtime_configuration_unsafe"
                ) from exc
            allowed_config = {
                "cli": {"installer": "internal"},
                "marketplace": {"default_skills_installs_purged": True},
            }
            if config_data != allowed_config:
                raise grok_error(
                    "Grok config.toml contains settings outside the verified allowlist.",
                    code="runtime_configuration_unsafe",
                )
        managed = grok_home / "managed_config.toml"
        try:
            managed_data = managed.read_text(encoding="utf-8") if managed.exists() else ""
        except (OSError, UnicodeDecodeError) as exc:
            raise grok_error(
                "Grok managed_config.toml is unreadable.",
                code="runtime_configuration_unsafe",
            ) from exc
        if managed_data.strip():
            raise grok_error(
                "Grok managed_config.toml must be empty in the dedicated profile.",
                code="runtime_configuration_unsafe",
            )

    def _validate_system_requirements(self, path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise grok_error(
                "Grok system requirements are unreadable.",
                code="runtime_configuration_unsafe",
            ) from exc
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_mode & 0o022
            ):
                raise grok_error(
                    "Grok system requirements are not root-owned and write-protected.",
                    code="runtime_configuration_unsafe",
                )
            encoded = handle.read(64 * 1024 + 1)
        if len(encoded) > 64 * 1024:
            raise grok_error(
                "Grok system requirements are too large.",
                code="runtime_configuration_unsafe",
            )
        try:
            data = tomllib.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise grok_error(
                "Grok system requirements are invalid.",
                code="runtime_configuration_unsafe",
            ) from exc
        grok_com_config = data.get("grok_com_config")
        sandbox = data.get("sandbox")
        ui = data.get("ui")
        if (
            not isinstance(grok_com_config, dict)
            or grok_com_config.get("disable_api_key_auth") is not True
            or not isinstance(sandbox, dict)
            or sandbox.get("profile") != "workspace"
            or not isinstance(ui, dict)
            or ui.get("disable_bypass_permissions_mode") is not True
        ):
            raise grok_error(
                "Grok system requirements do not enforce the Gateway policy.",
                code="runtime_configuration_unsafe",
            )

    def _validate_initialize(
        self,
        result: Any,
        evidence: GrokBillingEvidence,
        *,
        expected_model_id: str,
    ) -> tuple[str, list[AgentModel]]:
        if not isinstance(result, dict):
            raise protocol_error("Grok initialize response must be an object.")
        version = result.get("protocolVersion")
        if version != self.config.acp_protocol_version:
            raise protocol_error("Grok negotiated an unsupported ACP version.")
        methods = result.get("authMethods")
        if not isinstance(methods, list) or not any(
            isinstance(item, dict) and item.get("id") == AUTH_METHOD for item in methods
        ):
            raise billing_unverified(
                "Grok ACP did not advertise browser subscription login.", runtime="grok"
            )
        capabilities = result.get("agentCapabilities")
        if not isinstance(capabilities, dict) or capabilities.get("loadSession") is not True:
            raise protocol_error("Grok ACP does not support session loading.")
        session_caps = capabilities.get("sessionCapabilities")
        if not isinstance(session_caps, dict) or not all(
            isinstance(session_caps.get(name), dict) for name in ("resume", "close")
        ):
            raise protocol_error("Grok ACP lacks resume or close capability.")
        metadata = result.get("_meta")
        if not isinstance(metadata, dict):
            raise protocol_error("Grok initialize response has no metadata.")
        if metadata.get("agentVersion") != self.config.expected_version:
            raise protocol_error("Grok ACP agent version does not match the pinned CLI version.")
        if metadata.get("mcpServers") not in (None, []):
            raise grok_error(
                "Grok ACP initialized with an unexpected MCP server.",
                code="runtime_configuration_unsafe",
            )
        model_state = metadata.get("modelState")
        available = model_state.get("availableModels") if isinstance(model_state, dict) else None
        if not isinstance(available, list):
            raise protocol_error("Grok ACP model catalog is unavailable.")
        if model_state.get("currentModelId") != expected_model_id:
            raise protocol_error("Grok ACP did not select the requested model.")
        if not available or len(available) > 256:
            raise protocol_error("Grok ACP model catalog has an unsafe size.")
        models: list[AgentModel] = []
        seen: set[str] = set()
        for row in available:
            if not isinstance(row, dict):
                raise protocol_error("Grok ACP model catalog contains an invalid entry.")
            model_id = row.get("modelId")
            name = row.get("name") or model_id
            if (
                not isinstance(model_id, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model_id)
                is None
                or not isinstance(name, str)
                or not name
                or len(name.encode("utf-8")) > 4096
            ):
                raise protocol_error("Grok ACP model identity is invalid.")
            if model_id in seen:
                raise protocol_error("Grok ACP model catalog contains duplicate ids.")
            seen.add(model_id)
            if model_id not in self.config.allowed_models:
                continue
            row_meta = row.get("_meta")
            context = row.get("contextWindow")
            if not isinstance(context, int) and isinstance(row_meta, dict):
                context = row_meta.get("totalContextTokens")
            models.append(
                AgentModel(
                    id=f"grok/{model_id}",
                    runtime=self.id,
                    provider_id=AUTH_METHOD,
                    native_model_id=model_id,
                    display_name=name,
                    context_limit=(
                        context
                        if isinstance(context, int)
                        and not isinstance(context, bool)
                        and 0 < context <= 2**31 - 1
                        else None
                    ),
                    capabilities={
                        "sessions": True,
                        "streaming": True,
                        "provider_managed_tools": True,
                        "approvals": True,
                        "resume": True,
                    },
                )
            )
        if {model.native_model_id for model in models} != set(self.config.allowed_models):
            raise protocol_error("A configured Grok model is absent from ACP discovery.")
        if not set(self.config.allowed_models).issubset(evidence.model_ids):
            raise billing_unverified(
                "The discovered Grok models are not covered by billing evidence.", runtime="grok"
            )
        snapshot = {
            "protocolVersion": version,
            "agentVersion": metadata.get("agentVersion"),
            "authMethod": AUTH_METHOD,
            "models": sorted(self.config.allowed_models),
            "capabilities": {
                "loadSession": capabilities.get("loadSession"),
                "sessionCapabilities": sorted(session_caps),
            },
        }
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}", models

    def _require_host(self, native_session_id: str) -> _SessionHost:
        host = self._hosts.get(native_session_id)
        if host is None or not host.client.running:
            raise _not_loaded()
        return host


def _required_result_string(result: Any, name: str, operation: str) -> str:
    value = result.get(name) if isinstance(result, dict) else None
    if not isinstance(value, str) or not value:
        raise protocol_error(f"Grok {operation} response has no {name}.")
    return value


def _require_result_object(result: Any, operation: str) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise protocol_error(f"Grok {operation} response must be an object.")
    return result


def _parse_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        value = int(cursor)
    except (TypeError, ValueError) as exc:
        raise _cursor_expired() from exc
    if value < 0 or str(value) != cursor:
        raise _cursor_expired()
    return value


def _not_loaded() -> AgentRuntimeError:
    return grok_error(
        "The Grok session is not loaded.",
        code="agent_session_not_loaded",
        status_code=409,
    )


def _cursor_expired() -> AgentRuntimeError:
    return grok_error(
        "The Grok event cursor is outside the retained history.",
        code="event_cursor_expired",
        status_code=410,
    )
