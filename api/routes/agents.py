from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from agent_runtime.errors import AgentRuntimeError
from agent_runtime.service import get_agent_service
from api.agent_schemas import (
    AnswerUserInputRequest,
    ApprovalDecisionRequest,
    CreateAgentSessionRequest,
    StartAgentTurnRequest,
)


router = APIRouter(prefix="/v1/agents", tags=["agents"])


def _request_id(request: Request) -> str:
    existing = request.headers.get("X-Request-ID", "").strip()
    return existing[:128] if existing else f"req_{uuid.uuid4().hex}"


def _error(exc: AgentRuntimeError, request: Request) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.as_detail(_request_id(request))},
        headers={"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None,
    )


def _require_idempotency_key(value: str | None) -> str:
    key = (value or "").strip()
    if not key or len(key) > 256:
        raise AgentRuntimeError(
            code="invalid_idempotency_key",
            message="Idempotency-Key is required and must be at most 256 characters.",
            status_code=400,
        )
    return key


@router.get("/runtimes")
async def list_agent_runtimes(request: Request):
    try:
        return {"object": "list", "data": await get_agent_service().list_runtimes()}
    except AgentRuntimeError as exc:
        return _error(exc, request)


@router.post("/runtimes/{runtime_id}/preflight")
async def preflight_agent_runtime(runtime_id: str, request: Request):
    try:
        return await get_agent_service().preflight(runtime_id)
    except AgentRuntimeError as exc:
        return _error(exc, request)


@router.get("/models")
async def list_agent_models(request: Request, runtime: str = Query(default="muse")):
    service = get_agent_service()
    try:
        models = await service.list_models(runtime)
        statuses = await service.list_runtimes()
        selected = next((item for item in statuses if item["id"] == runtime), None)
        return {
            "object": "list",
            "data": models,
            "runtime": selected,
        }
    except AgentRuntimeError as exc:
        if exc.code in {"runtime_unavailable", "runtime_billing_unverified", "runtime_auth_required"}:
            statuses = await service.list_runtimes()
            selected = next((item for item in statuses if item["id"] == runtime), None)
            return {"object": "list", "data": [], "runtime": selected}
        return _error(exc, request)


@router.post("/sessions", status_code=201)
async def create_agent_session(
    body: CreateAgentSessionRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        return await get_agent_service().create_session(
            runtime_id=body.runtime,
            public_model_id=body.model,
            approval_policy=body.approval_policy,
            idempotency_key=_require_idempotency_key(idempotency_key),
        )
    except AgentRuntimeError as exc:
        return _error(exc, request)


@router.get("/sessions/{session_id}")
async def get_agent_session(session_id: str, request: Request):
    try:
        return get_agent_service().get_session(session_id)
    except AgentRuntimeError as exc:
        return _error(exc, request)


@router.post("/sessions/{session_id}/resume")
async def resume_agent_session(
    session_id: str,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        return await get_agent_service().resume_session(
            session_id,
            _require_idempotency_key(idempotency_key),
        )
    except AgentRuntimeError as exc:
        return _error(exc, request)


@router.post("/sessions/{session_id}/release")
async def release_agent_session(
    session_id: str,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        return await get_agent_service().release_session(
            session_id,
            _require_idempotency_key(idempotency_key),
        )
    except AgentRuntimeError as exc:
        return _error(exc, request)


@router.post("/sessions/{session_id}/turns", status_code=202)
async def start_agent_turn(
    session_id: str,
    body: StartAgentTurnRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        text = "\n".join(part.text for part in body.input)
        return await get_agent_service().start_turn(
            session_id=session_id,
            text=text,
            idempotency_key=_require_idempotency_key(idempotency_key),
        )
    except AgentRuntimeError as exc:
        return _error(exc, request)


@router.post("/sessions/{session_id}/turns/{turn_id}/cancel", status_code=202)
async def cancel_agent_turn(
    session_id: str,
    turn_id: str,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        return await get_agent_service().cancel_turn(
            session_id=session_id,
            turn_id=turn_id,
            idempotency_key=_require_idempotency_key(idempotency_key),
        )
    except AgentRuntimeError as exc:
        return _error(exc, request)


@router.get("/sessions/{session_id}/events")
async def stream_agent_events(
    session_id: str,
    request: Request,
    after: str | None = Query(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    try:
        cursor = after or last_event_id
        iterator = await get_agent_service().prepare_event_stream(session_id, cursor)
    except AgentRuntimeError as exc:
        return _error(exc, request)

    async def stream():
        try:
            async for event in iterator:
                if await request.is_disconnected():
                    break
                if event is None:
                    yield ": heartbeat\n\n"
                    continue
                payload = json.dumps(event.public_dict(), ensure_ascii=False, separators=(",", ":"))
                yield f"id: {event.cursor}\nevent: {event.type}\ndata: {payload}\n\n"
        except AgentRuntimeError as exc:
            if exc.code != "event_stream_overflow":
                raise

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{session_id}/approvals/{approval_id}/decision", status_code=202)
async def decide_agent_approval(
    session_id: str,
    approval_id: str,
    body: ApprovalDecisionRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        return await get_agent_service().decide_approval(
            session_id=session_id,
            approval_id=approval_id,
            decision=body.decision,
            idempotency_key=_require_idempotency_key(idempotency_key),
        )
    except AgentRuntimeError as exc:
        return _error(exc, request)


@router.post("/sessions/{session_id}/user-input/{user_input_id}/answer", status_code=202)
async def answer_agent_user_input(
    session_id: str,
    user_input_id: str,
    body: AnswerUserInputRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    try:
        return await get_agent_service().answer_user_input(
            session_id=session_id,
            user_input_id=user_input_id,
            answers=[answer.to_native() for answer in body.answers],
            idempotency_key=_require_idempotency_key(idempotency_key),
        )
    except AgentRuntimeError as exc:
        return _error(exc, request)
