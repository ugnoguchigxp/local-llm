from __future__ import annotations

from agent_runtime.errors import AgentRuntimeError


def map_bridge_error(
    kind: str,
    message: str,
    *,
    retryable: bool = False,
    data: dict[str, object] | None = None,
) -> AgentRuntimeError:
    status_code = 500
    code = "provider_error"
    normalized_data = dict(data or {})
    if kind in {"auth_required", "configError", "config_error", "usageError"}:
        status_code, code = 503, "runtime_auth_required"
    elif kind in {"sessionNotFound", "session_not_found"}:
        status_code, code = 404, "agent_session_not_found"
    elif kind == "notFound" and normalized_data.get("reason") == "missingAnchor":
        status_code, code = 410, "event_cursor_expired"
    elif kind == "notFound":
        status_code, code = 404, "provider_resource_not_found"
    elif kind in {"sessionInUse", "sessionAmbiguous", "leaseUnavailable", "session_in_use"}:
        status_code, code = 409, "agent_session_in_use"
    elif kind in {"sessionNotLoaded", "session_not_loaded"}:
        status_code, code = 409, "agent_session_not_loaded"
    elif kind in {"overloaded", "backpressured"}:
        status_code, code = 503, "runtime_overloaded"
        retryable = True
    elif kind in {"quota_exceeded", "rate_limited"}:
        status_code, code = 429, "runtime_quota_exceeded"
    elif kind in {"invalidParams", "inputTooLarge", "input_too_large", "invalid_params"}:
        status_code, code = 400, "invalid_agent_request"
    elif kind in {"capabilityRequired", "experimentalRequired"}:
        status_code, code = 400, "unsupported_capability"
    elif kind in {"commandRejected", "interrupted", "cancelled"}:
        status_code, code = 409, "agent_command_conflict"
    elif kind in {"approvalNotFound", "approvalAlreadyResolved", "approvalChoiceInvalid", "approvalRequirementStale"}:
        status_code, code = 409, "approval_conflict"
    elif kind in {"userInputNotFound", "userInputAlreadySettled", "userInputAnswerInvalid"}:
        status_code, code = 409, "user_input_conflict"
    elif kind == "approvalReviewerUnavailable":
        status_code, code = 503, "approval_unavailable"
    elif kind in {"viewTruncated", "boundaryPruned", "boundaryUnusable", "noBoundary"}:
        status_code, code = 410, "event_cursor_expired"
    elif kind in {
        "protocol_error",
        "fingerprint_mismatch",
        "schemaFingerprintMismatch",
        "methodNotFound",
        "notInitialized",
        "alreadyInitialized",
        "sessionStreamMismatch",
        "sdkSurfaceUnavailable",
    }:
        status_code, code = 503, "runtime_protocol_mismatch"
    elif kind in {
        "provider_host_exited",
        "bridge_eof",
        "bridge_not_running",
        "crash",
        "transportEof",
        "unhandledError",
    }:
        status_code, code = 503, "provider_host_exited"
    elif kind in {"pageEventTooLarge", "outputResultTooLarge", "output_too_large"}:
        status_code, code = 502, "provider_response_too_large"
    return AgentRuntimeError(
        code=code,
        message=message,
        status_code=status_code,
        runtime="muse",
        retryable=retryable,
        data=normalized_data,
    )
