from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class RuntimeStatus:
    id: str
    status: str
    billing_mode: str = "unknown"
    auth: str = "unknown"
    protocol_fingerprint: str | None = None
    active_sessions: int = 0
    active_turns: int = 0
    detail: str | None = None


@dataclass(frozen=True)
class AgentModel:
    id: str
    runtime: str
    provider_id: str
    native_model_id: str
    display_name: str
    context_limit: int | None = None
    output_limit: int | None = None
    capabilities: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class NativeSession:
    session_id: str
    view_cursor: str
    status: str
    model_id: str | None = None
    provider_id: str | None = None


@dataclass(frozen=True)
class NativeTurn:
    turn_id: str
    status: str
    disposition: str


@dataclass(frozen=True)
class NativeEvent:
    runtime_id: str
    event_type: str
    native_session_id: str
    native_turn_id: str | None
    native_cursor: str
    data: dict[str, Any]


EventHandler = Callable[[NativeEvent], Awaitable[None]]


class AgentRuntime(Protocol):
    id: str

    def set_event_handler(self, handler: EventHandler) -> None: ...

    async def status(self) -> RuntimeStatus: ...

    async def preflight(self) -> RuntimeStatus: ...

    async def list_models(self) -> list[AgentModel]: ...

    async def start_session(
        self,
        *,
        workspace_root: str,
        model_id: str,
        provider_id: str,
        approval_mode: str,
        command_id: str,
    ) -> NativeSession: ...

    async def resume_session(
        self,
        *,
        native_session_id: str,
        cursor: str | None,
        command_id: str,
    ) -> NativeSession: ...

    async def release_session(self, *, native_session_id: str) -> None: ...

    async def start_turn(
        self,
        *,
        native_session_id: str,
        text: str,
        command_id: str,
    ) -> NativeTurn: ...

    async def cancel_turn(
        self,
        *,
        native_session_id: str,
        native_turn_id: str,
        command_id: str,
    ) -> NativeTurn: ...

    async def decide_approval(
        self,
        *,
        native_session_id: str,
        approval_id: str,
        decision: str,
        command_id: str,
    ) -> dict[str, Any]: ...

    async def answer_user_input(
        self,
        *,
        native_session_id: str,
        user_input_id: str,
        answers: list[dict[str, Any]],
        command_id: str,
    ) -> dict[str, Any]: ...

    async def page_events(
        self,
        *,
        native_session_id: str,
        cursor: str | None,
        limit: int = 200,
    ) -> list[NativeEvent]: ...

    async def close(self) -> None: ...
