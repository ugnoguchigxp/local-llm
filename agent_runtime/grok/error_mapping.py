from __future__ import annotations

import re
from typing import Any

from agent_runtime.errors import AgentRuntimeError


_SECRET_ASSIGNMENT = re.compile(
    r'''(?ix)
    (
        ["']?(?:
            authorization
            |api[_-]?key
            |(?:access|refresh|session|id)?[_-]?token
            |client[_-]?secret
            |password
            |cookie
        )["']?
        \s*[:=]\s*["']?(?:bearer\s+)?
    )
    ([^"'\s,;}]+)
    '''
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_PREFIXED_SECRET = re.compile(r"(?i)\b(?:xai-|sk-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}")


def redact_provider_text(value: str, *, limit: int = 4096) -> str:
    assigned = _SECRET_ASSIGNMENT.sub(r"\1[redacted]", value[:limit])
    bearer_redacted = _BEARER_VALUE.sub("Bearer [redacted]", assigned)
    return _PREFIXED_SECRET.sub("[redacted]", bearer_redacted)


def grok_error(
    message: str,
    *,
    code: str = "runtime_unavailable",
    status_code: int = 503,
    retryable: bool = False,
    data: dict[str, Any] | None = None,
) -> AgentRuntimeError:
    return AgentRuntimeError(
        code=code,
        message=message,
        status_code=status_code,
        runtime="grok",
        retryable=retryable,
        data=data or {},
    )


def protocol_error(message: str) -> AgentRuntimeError:
    return grok_error(message, code="runtime_protocol_mismatch")


def host_exited(message: str = "The Grok ACP process exited unexpectedly.") -> AgentRuntimeError:
    return grok_error(message, code="provider_host_exited", retryable=True)


def map_rpc_error(error: Any) -> AgentRuntimeError:
    if not isinstance(error, dict):
        return protocol_error("Grok returned an invalid JSON-RPC error.")
    code = error.get("code")
    message = error.get("message")
    if (
        not isinstance(code, int)
        or isinstance(code, bool)
        or not -(2**31) <= code <= 2**31 - 1
        or not isinstance(message, str)
        or not message
    ):
        return protocol_error("Grok returned an invalid JSON-RPC error.")
    text = redact_provider_text(message) if message else "Grok request failed."
    lowered = text.lower()
    if (
        "auth" in lowered
        or "login" in lowered
        or "sign in" in lowered
        or code in {-32000, -32001}
    ):
        return grok_error(text, code="runtime_auth_required", status_code=401)
    if "rate limit" in lowered or "usage limit" in lowered or "quota" in lowered:
        return grok_error(text, code="provider_rate_limited", status_code=429, retryable=True)
    if "cancel" in lowered or code == -32800:
        return grok_error(text, code="provider_cancelled", status_code=409)
    return grok_error(
        text,
        code="provider_request_failed",
        status_code=502,
        retryable=code == -32603 or -32099 <= code <= -32003,
        data={"provider_code": code},
    )
