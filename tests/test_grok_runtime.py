from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path

import pytest

from agent_runtime.base import NativeEvent
from agent_runtime.errors import AgentRuntimeError
from agent_runtime.grok.config import AUTH_METHOD, GrokConfig
from agent_runtime.grok.runtime import GrokRuntime
from agent_runtime.muse.config import MuseConfig
from agent_runtime.service import build_agent_service


def _initialize_result():
    return {
        "protocolVersion": 1,
        "agentCapabilities": {
            "loadSession": True,
            "sessionCapabilities": {"list": {}, "resume": {}, "close": {}},
        },
        "authMethods": [{"id": AUTH_METHOD, "name": "Grok"}],
        "_meta": {
            "agentVersion": "1.0.13",
            "modelState": {
                "currentModelId": "grok-4.6",
                "availableModels": [
                    {
                        "modelId": "grok-4.6",
                        "name": "Grok 4.6",
                        "_meta": {"totalContextTokens": 500000},
                    }
                ],
            },
        },
    }


class FakeClient:
    def __init__(self, **kwargs):
        self.notification_callback = kwargs["notification_callback"]
        self.request_callback = kwargs["request_callback"]
        self.running = False
        self.calls = []
        self.notifications = []
        self.responses = []
        self.prompt = None

    async def start(self):
        self.running = True

    async def initialize(self, protocol_version, timeout_ms):
        assert protocol_version == 1 and timeout_ms > 0
        return _initialize_result()

    async def request(self, method, params=None, **kwargs):
        self.calls.append((method, params, kwargs))
        if method == "authenticate":
            return {}
        if method == "session/new":
            return {"sessionId": "native-grok-session"}
        if method == "session/resume":
            return {}
        if method == "session/close":
            return {}
        if method == "session/prompt":
            self.prompt = asyncio.get_running_loop().create_future()
            return await self.prompt
        raise AssertionError(method)

    async def notify(self, method, params=None):
        self.notifications.append((method, params))

    async def respond(self, request_id, *, result=None, error=None):
        self.responses.append((request_id, result, error))

    async def close(self):
        self.running = False
        if self.prompt is not None and not self.prompt.done():
            self.prompt.cancel()


def _config(tmp_path: Path) -> GrokConfig:
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    evidence = tmp_path / "evidence.json"
    verified = datetime.now(timezone.utc).replace(microsecond=0)
    expires = verified + timedelta(days=7)
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime": "grok",
                "billing_mode": "subscription",
                "billing_assurance": "operator_attested",
                "profile_root": str(profile),
                "auth_method": AUTH_METHOD,
                "account_fingerprint": "unavailable",
                "plan": "operator-confirmed",
                "extra_usage_credits": "zero",
                "auto_top_up": "disabled",
                "grok_version": "1.0.13",
                "acp_protocol_version": 1,
                "model_ids": ["grok-4.6"],
                "sandbox_profile": "workspace",
                "verified_at": verified.isoformat(),
                "expires_at": expires.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    evidence.chmod(0o600)
    return GrokConfig(
        enabled=True,
        binary="/bin/echo",
        profile_root=profile,
        workspace_root=tmp_path / "workspaces",
        state_db=tmp_path / "state.sqlite3",
        cursor_secret_file=tmp_path / "cursor.secret",
        billing_evidence_file=evidence,
        allowed_models=("grok-4.6",),
        expected_version="1.0.13",
        acp_protocol_version=1,
        sandbox_profile="workspace",
        allow_web_search=False,
        allow_project_extensions=False,
        startup_timeout_ms=1000,
        request_timeout_ms=1000,
        turn_timeout_ms=1000,
        shutdown_timeout_ms=1000,
        approval_timeout_ms=1000,
        max_sessions=2,
        max_frame_bytes=64 * 1024,
        debug_log=False,
    )


async def _ready_runtime(tmp_path, client_class=FakeClient):
    clients = []

    def factory(**kwargs):
        client = client_class(**kwargs)
        clients.append(client)
        return client

    runtime = GrokRuntime(_config(tmp_path), client_factory=factory)

    async def version():
        return "1.0.13"

    async def inspect(_cwd):
        return None

    runtime._read_version = version
    runtime._inspect_profile = inspect
    events: list[NativeEvent] = []

    async def handle(event):
        events.append(event)

    runtime.set_event_handler(handle)
    return runtime, clients, events


def test_full_session_turn_permission_cancel_and_resume_lifecycle(tmp_path):
    asyncio.run(_full_session_turn_permission_cancel_and_resume_lifecycle(tmp_path))


def test_session_new_accepts_and_correlates_early_notifications(tmp_path):
    class EarlyNotificationClient(FakeClient):
        async def request(self, method, params=None, **kwargs):
            if method == "session/new":
                self.calls.append((method, params, kwargs))
                await self.notification_callback(
                    "session/update",
                    {
                        "sessionId": "native-grok-session",
                        "update": {"sessionUpdate": "usage_update"},
                    },
                )
                return {"sessionId": "native-grok-session"}
            return await super().request(method, params, **kwargs)

    async def run():
        runtime, _clients, events = await _ready_runtime(
            tmp_path, client_class=EarlyNotificationClient
        )
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        session = await runtime.start_session(
            workspace_root=str(workspace),
            model_id="grok-4.6",
            provider_id=AUTH_METHOD,
            approval_mode="strict",
            command_id="session-command",
        )

        assert session.session_id == "native-grok-session"
        assert events == []
        await runtime.close()

    asyncio.run(run())


def test_session_new_rejects_early_model_identity_change(tmp_path):
    class ChangedModelClient(FakeClient):
        async def request(self, method, params=None, **kwargs):
            if method == "session/new":
                self.calls.append((method, params, kwargs))
                await self.notification_callback(
                    "session/update",
                    {
                        "sessionId": "native-grok-session",
                        "update": {
                            "sessionUpdate": "current_model_update",
                            "modelId": "grok-4.5",
                        },
                    },
                )
                return {"sessionId": "native-grok-session"}
            return await super().request(method, params, **kwargs)

    async def run():
        runtime, clients, _events = await _ready_runtime(
            tmp_path, client_class=ChangedModelClient
        )
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        with pytest.raises(AgentRuntimeError, match="another model"):
            await runtime.start_session(
                workspace_root=str(workspace),
                model_id="grok-4.6",
                provider_id=AUTH_METHOD,
                approval_mode="strict",
                command_id="session-command",
            )

        assert clients[-1].running is False
        await runtime.close()

    asyncio.run(run())


async def _full_session_turn_permission_cancel_and_resume_lifecycle(tmp_path):
    runtime, clients, events = await _ready_runtime(tmp_path)
    status = await runtime.preflight()
    assert status.status == "ready"
    assert status.protocol_name == "acp"
    assert status.billing_assurance == "operator_attested"
    assert (await runtime.list_models())[0].context_limit == 500000

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = await runtime.start_session(
        workspace_root=str(workspace),
        model_id="grok-4.6",
        provider_id=AUTH_METHOD,
        approval_mode="strict",
        command_id="session-command",
    )
    host_client = clients[-1]
    turn = await runtime.start_turn(
        native_session_id=session.session_id,
        text="Edit a file",
        command_id="turn-command",
    )
    await asyncio.sleep(0)
    await host_client.notification_callback(
        "session/update",
        {
            "sessionId": session.session_id,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "Working"},
            },
        },
    )
    await host_client.request_callback(
        "provider-request",
        "session/request_permission",
        {
            "sessionId": session.session_id,
            "options": [
                {"optionId": "once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "deny", "name": "Reject", "kind": "reject_once"},
            ],
            "toolCall": {
                "toolCallId": "tool-1",
                "title": "Edit",
                "kind": "edit",
                "rawInput": {"path": "safe.txt"},
            },
        },
    )
    approval = next(event for event in events if event.event_type == "approval.requested")
    await runtime.decide_approval(
        native_session_id=session.session_id,
        approval_id=approval.data["approval_id"],
        decision="allow_once",
        command_id="approval-command",
    )
    assert host_client.responses[-1][1] == {
        "outcome": {"outcome": "selected", "optionId": "once"}
    }

    host_client.prompt.set_result({"stopReason": "end_turn"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert [event.event_type for event in events][-1] == "turn.completed"
    assert [event.event_type for event in await runtime.page_events(
        native_session_id=session.session_id, cursor="0", limit=20
    )] == [
        "turn.started",
        "message.delta",
        "approval.requested",
        "approval.resolved",
        "turn.completed",
    ]

    second_turn = await runtime.start_turn(
        native_session_id=session.session_id,
        text="Long task",
        command_id="turn-command-2",
    )
    await asyncio.sleep(0)
    cancelled = await runtime.cancel_turn(
        native_session_id=session.session_id,
        native_turn_id=second_turn.turn_id,
        command_id="cancel-command",
    )
    assert cancelled.disposition == "cancel_requested"
    assert host_client.notifications[-1] == (
        "session/cancel",
        {"sessionId": session.session_id},
    )
    host_client.prompt.set_result({"stopReason": "cancelled"})
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    final_cursor = str(events[-1].native_cursor)
    await runtime.release_session(native_session_id=session.session_id)
    resumed = await runtime.resume_session(
        native_session_id=session.session_id,
        cursor=final_cursor,
        command_id="resume-command",
        workspace_root=str(workspace),
        model_id="grok-4.6",
        provider_id=AUTH_METHOD,
    )
    assert resumed.session_id == session.session_id
    assert resumed.view_cursor == final_cursor
    assert clients[-1].calls[-1][0] == "session/resume"
    await runtime.close()


def test_unknown_or_always_only_permission_fails_closed(tmp_path):
    async def run():
        runtime, clients, _events = await _ready_runtime(tmp_path)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        session = await runtime.start_session(
            workspace_root=str(workspace),
            model_id="grok-4.6",
            provider_id=AUTH_METHOD,
            approval_mode="strict",
            command_id="session-command",
        )
        client = clients[-1]
        await runtime.start_turn(
            native_session_id=session.session_id,
            text="unsafe",
            command_id="turn-command",
        )
        with pytest.raises(AgentRuntimeError) as raised:
            await client.request_callback(
                "p1",
                "session/request_permission",
                {
                    "sessionId": session.session_id,
                    "options": [
                        {"optionId": "forever", "name": "Always", "kind": "allow_always"}
                    ],
                    "toolCall": {"toolCallId": "tool-1", "rawInput": {}},
                },
            )
        assert raised.value.code == "runtime_protocol_mismatch"
        assert client.responses[-1][1] == {"outcome": {"outcome": "cancelled"}}
        await runtime.close()

    asyncio.run(run())


def test_resume_replaces_an_existing_idle_host(tmp_path):
    async def run():
        runtime, clients, _events = await _ready_runtime(tmp_path)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        session = await runtime.start_session(
            workspace_root=str(workspace),
            model_id="grok-4.6",
            provider_id=AUTH_METHOD,
            approval_mode="strict",
            command_id="session-command",
        )
        original = clients[-1]

        resumed = await runtime.resume_session(
            native_session_id=session.session_id,
            cursor="0",
            command_id="resume-command",
            workspace_root=str(workspace),
            model_id="grok-4.6",
            provider_id=AUTH_METHOD,
        )

        assert resumed.status == "idle"
        assert original.running is False
        assert clients[-1] is not original
        assert clients[-1].calls[-1][0] == "session/resume"
        await runtime.close()

    asyncio.run(run())


def test_permission_without_tool_input_exposes_deny_only(tmp_path):
    async def run():
        runtime, clients, events = await _ready_runtime(tmp_path)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        session = await runtime.start_session(
            workspace_root=str(workspace),
            model_id="grok-4.6",
            provider_id=AUTH_METHOD,
            approval_mode="strict",
            command_id="session-command",
        )
        client = clients[-1]
        await runtime.start_turn(
            native_session_id=session.session_id,
            text="Perform an operation",
            command_id="turn-command",
        )
        await asyncio.sleep(0)
        await client.request_callback(
            "p1",
            "session/request_permission",
            {
                "sessionId": session.session_id,
                "options": [
                    {"optionId": "once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "deny", "name": "Reject", "kind": "reject_once"},
                ],
                "toolCall": {"toolCallId": "tool-1", "title": "Unknown operation"},
            },
        )
        approval = next(event for event in events if event.event_type == "approval.requested")
        assert approval.data["available_choices"] == ["deny"]
        assert "provider_options" not in approval.data

        with pytest.raises(AgentRuntimeError) as raised:
            await runtime.decide_approval(
                native_session_id=session.session_id,
                approval_id=approval.data["approval_id"],
                decision="allow_once",
                command_id="allow-command",
            )
        assert raised.value.code == "approval_context_unavailable"

        await runtime.decide_approval(
            native_session_id=session.session_id,
            approval_id=approval.data["approval_id"],
            decision="deny",
            command_id="deny-command",
        )
        assert client.responses[-1][1] == {
            "outcome": {"outcome": "selected", "optionId": "deny"}
        }
        await runtime.close()

    asyncio.run(run())


def test_runtime_close_cancels_pending_permission_requests(tmp_path):
    async def run():
        runtime, clients, events = await _ready_runtime(tmp_path)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        session = await runtime.start_session(
            workspace_root=str(workspace),
            model_id="grok-4.6",
            provider_id=AUTH_METHOD,
            approval_mode="strict",
            command_id="session-command",
        )
        client = clients[-1]
        await runtime.start_turn(
            native_session_id=session.session_id,
            text="Perform an operation",
            command_id="turn-command",
        )
        await asyncio.sleep(0)
        await client.request_callback(
            "p1",
            "session/request_permission",
            {
                "sessionId": session.session_id,
                "options": [
                    {"optionId": "once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "deny", "name": "Reject", "kind": "reject_once"},
                ],
                "toolCall": {"toolCallId": "tool-1", "rawInput": {"path": "safe.txt"}},
            },
        )
        approval = next(event for event in events if event.event_type == "approval.requested")
        pending = runtime._hosts[session.session_id].pending_permissions[
            approval.data["approval_id"]
        ]

        await runtime.close()
        await asyncio.sleep(0)

        assert client.responses[-1][1] == {"outcome": {"outcome": "cancelled"}}
        assert pending.timeout_task is not None and pending.timeout_task.done()

    asyncio.run(run())


def test_turn_rechecks_workspace_configuration(tmp_path):
    async def run():
        runtime, _clients, _events = await _ready_runtime(tmp_path)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        session = await runtime.start_session(
            workspace_root=str(workspace),
            model_id="grok-4.6",
            provider_id=AUTH_METHOD,
            approval_mode="strict",
            command_id="session-command",
        )

        async def reject_workspace(cwd):
            if cwd == workspace.resolve():
                raise AgentRuntimeError(
                    code="runtime_configuration_unsafe",
                    message="Workspace configuration changed",
                    status_code=503,
                    runtime="grok",
                )

        runtime._inspect_profile = reject_workspace
        with pytest.raises(AgentRuntimeError) as raised:
            await runtime.start_turn(
                native_session_id=session.session_id,
                text="Continue",
                command_id="turn-command",
            )
        assert raised.value.code == "runtime_configuration_unsafe"
        await runtime.close()

    asyncio.run(run())


def test_initialize_requires_requested_model(tmp_path):
    config = _config(tmp_path)
    runtime = GrokRuntime(config, client_factory=FakeClient)
    evidence, error = config.validate_billing_evidence()
    assert error is None and evidence is not None
    result = _initialize_result()
    result["_meta"]["modelState"]["currentModelId"] = "grok-4.5"

    with pytest.raises(AgentRuntimeError, match="requested model"):
        runtime._validate_initialize(
            result, evidence, expected_model_id="grok-4.6"
        )


def test_prompt_controls_are_rejected_and_invariant_change_cancels(tmp_path):
    async def run():
        runtime, clients, events = await _ready_runtime(tmp_path)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        session = await runtime.start_session(
            workspace_root=str(workspace),
            model_id="grok-4.6",
            provider_id=AUTH_METHOD,
            approval_mode="strict",
            command_id="session-command",
        )
        client = clients[-1]

        for text in ("/model grok-4.5", "  ! pwd"):
            with pytest.raises(AgentRuntimeError) as raised:
                await runtime.start_turn(
                    native_session_id=session.session_id,
                    text=text,
                    command_id="blocked-command",
                )
            assert raised.value.code == "unsupported_prompt_control"

        await runtime.start_turn(
            native_session_id=session.session_id,
            text="Continue normally",
            command_id="normal-command",
        )
        await asyncio.sleep(0)
        await client.notification_callback(
            "session/update",
            {
                "sessionId": session.session_id,
                "update": {
                    "sessionUpdate": "current_mode_update",
                    "currentModeId": "unrestricted",
                },
            },
        )

        assert client.notifications[-1] == (
            "session/cancel",
            {"sessionId": session.session_id},
        )
        assert client.running is False
        await asyncio.sleep(0)
        assert [event.event_type for event in events][-2:] == [
            "turn.failed",
            "session.invariant_changed",
        ]
        await runtime.close()

    asyncio.run(run())


def test_prompt_timeout_terminates_host_and_requires_recovery(tmp_path):
    class TimeoutClient(FakeClient):
        async def request(self, method, params=None, **kwargs):
            if method == "session/prompt":
                self.calls.append((method, params, kwargs))
                raise AgentRuntimeError(
                    code="provider_timeout",
                    message="Prompt timed out",
                    status_code=504,
                    runtime="grok",
                    retryable=True,
                )
            return await super().request(method, params, **kwargs)

    async def run():
        runtime, clients, events = await _ready_runtime(tmp_path, client_class=TimeoutClient)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        session = await runtime.start_session(
            workspace_root=str(workspace),
            model_id="grok-4.6",
            provider_id=AUTH_METHOD,
            approval_mode="strict",
            command_id="session-command",
        )
        client = clients[-1]

        await runtime.start_turn(
            native_session_id=session.session_id,
            text="A long task",
            command_id="turn-command",
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert client.notifications[-1] == (
            "session/cancel",
            {"sessionId": session.session_id},
        )
        assert client.running is False
        assert [event.event_type for event in events][-2:] == [
            "turn.failed",
            "session.recovery_required",
        ]
        await runtime.close()

    asyncio.run(run())


def test_unknown_prompt_stop_reason_fails_closed(tmp_path):
    async def run():
        runtime, clients, events = await _ready_runtime(tmp_path)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        session = await runtime.start_session(
            workspace_root=str(workspace),
            model_id="grok-4.6",
            provider_id=AUTH_METHOD,
            approval_mode="strict",
            command_id="session-command",
        )
        client = clients[-1]
        await runtime.start_turn(
            native_session_id=session.session_id,
            text="Run",
            command_id="turn-command",
        )
        await asyncio.sleep(0)
        client.prompt.set_result({"stopReason": "future_reason"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert client.running is False
        assert [event.event_type for event in events][-2:] == [
            "turn.failed",
            "session.recovery_required",
        ]
        await runtime.close()

    asyncio.run(run())


def test_cancel_watchdog_forces_terminal_and_recovery(tmp_path):
    async def run():
        runtime, clients, events = await _ready_runtime(tmp_path)
        runtime.config = replace(runtime.config, shutdown_timeout_ms=10)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        session = await runtime.start_session(
            workspace_root=str(workspace),
            model_id="grok-4.6",
            provider_id=AUTH_METHOD,
            approval_mode="strict",
            command_id="session-command",
        )
        client = clients[-1]
        turn = await runtime.start_turn(
            native_session_id=session.session_id,
            text="Run until cancelled",
            command_id="turn-command",
        )
        await asyncio.sleep(0)
        await runtime.cancel_turn(
            native_session_id=session.session_id,
            native_turn_id=turn.turn_id,
            command_id="cancel-command",
        )
        await asyncio.sleep(0.02)

        assert client.running is False
        assert [event.event_type for event in events][-2:] == [
            "turn.cancelled",
            "session.recovery_required",
        ]
        assert events[-2].data["provider_confirmed"] is False
        await runtime.close()

    asyncio.run(run())


def test_inspection_rejects_api_key_auth_and_extensions(tmp_path):
    runtime = GrokRuntime(_config(tmp_path), client_factory=FakeClient)
    base = {
        "grokVersion": "1.0.13",
        "projectInstructions": [],
        "hooks": [],
        "skills": [],
        "plugins": [],
        "marketplaces": [],
        "mcpServers": [],
        "lspServers": [],
        "agents": [{"source": {"type": "builtin"}}],
        "permissions": {"loaded": 0},
        "loginPolicy": {"apiKeyAuthDisabled": False},
        "configSources": {"layers": []},
    }
    with pytest.raises(AgentRuntimeError) as raised:
        runtime._validate_inspection(base)
    assert raised.value.code == "runtime_billing_unverified"

    base["loginPolicy"]["apiKeyAuthDisabled"] = True
    base["hooks"] = [{"event": "pre_tool_use"}]
    with pytest.raises(AgentRuntimeError) as raised:
        runtime._validate_inspection(base)
    assert raised.value.code == "runtime_configuration_unsafe"


def test_profile_toml_allowlist_rejects_custom_models(tmp_path):
    config = _config(tmp_path)
    grok_home = config.profile_root / ".grok"
    grok_home.mkdir()
    (grok_home / "requirements.toml").write_text(
        '[grok_com_config]\ndisable_api_key_auth = true\n[sandbox]\nprofile = "workspace"\n',
        encoding="utf-8",
    )
    user_config = grok_home / "config.toml"
    user_config.write_text(
        '[cli]\ninstaller = "internal"\n[marketplace]\ndefault_skills_installs_purged = true\n',
        encoding="utf-8",
    )
    runtime = GrokRuntime(config, client_factory=FakeClient)
    runtime._validate_config_files()

    user_config.write_text(
        user_config.read_text(encoding="utf-8")
        + '[model.grok-4.6]\nbase_url = "https://untrusted.example/v1"\n',
        encoding="utf-8",
    )
    with pytest.raises(AgentRuntimeError) as raised:
        runtime._validate_config_files()
    assert raised.value.code == "runtime_configuration_unsafe"


def test_profile_toml_read_failure_is_mapped_to_safe_runtime_error(monkeypatch, tmp_path):
    config = _config(tmp_path)
    grok_home = config.profile_root / ".grok"
    grok_home.mkdir()
    (grok_home / "requirements.toml").write_text(
        '[grok_com_config]\ndisable_api_key_auth = true\n[sandbox]\nprofile = "workspace"\n',
        encoding="utf-8",
    )
    managed = grok_home / "managed_config.toml"
    managed.touch()
    original_read_text = Path.read_text

    def fail_managed_read(path, *args, **kwargs):
        if path == managed:
            raise OSError("simulated concurrent replacement")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_managed_read)
    runtime = GrokRuntime(config, client_factory=FakeClient)

    with pytest.raises(AgentRuntimeError) as raised:
        runtime._validate_config_files()

    assert raised.value.code == "runtime_configuration_unsafe"


def test_preflight_reports_auth_required(monkeypatch, tmp_path):
    class AuthFailureClient(FakeClient):
        async def request(self, method, params=None, **kwargs):
            if method == "authenticate":
                raise AgentRuntimeError(
                    code="runtime_auth_required",
                    message="Sign in with Grok",
                    status_code=401,
                    runtime="grok",
                )
            return await super().request(method, params, **kwargs)

    runtime = GrokRuntime(_config(tmp_path), client_factory=AuthFailureClient)

    async def version():
        return "1.0.13"

    async def inspect(_cwd):
        return None

    monkeypatch.setattr(runtime, "_read_version", version)
    monkeypatch.setattr(runtime, "_inspect_profile", inspect)

    with pytest.raises(AgentRuntimeError) as raised:
        asyncio.run(runtime.preflight())
    assert raised.value.code == "runtime_auth_required"
    assert asyncio.run(runtime.status()).status == "auth_required"


def test_failed_preflight_invalidates_cached_ready_status(monkeypatch, tmp_path):
    async def run():
        runtime, _clients, _events = await _ready_runtime(tmp_path)
        assert (await runtime.preflight()).status == "ready"

        async def unavailable():
            raise AgentRuntimeError(
                code="runtime_unavailable",
                message="CLI disappeared",
                status_code=503,
                runtime="grok",
            )

        monkeypatch.setattr(runtime, "_read_version", unavailable)
        with pytest.raises(AgentRuntimeError):
            await runtime.preflight()

        status = await runtime.status()
        assert status.status == "unavailable"
        assert status.detail == "CLI disappeared"
        await runtime.close()

    asyncio.run(run())


def test_service_registers_disabled_muse_and_grok_independently(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_LLM_MUSE_ENABLED", "false")
    muse = MuseConfig.from_env(repo_root=tmp_path)
    grok = replace(_config(tmp_path), enabled=False)
    service = build_agent_service(muse, grok)

    statuses = asyncio.run(service.list_runtimes())

    assert [(item["id"], item["status"]) for item in statuses] == [
        ("muse", "disabled"),
        ("grok", "disabled"),
    ]
    asyncio.run(service.close())
