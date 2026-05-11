from __future__ import annotations

import os
from hmac import compare_digest


def is_truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def require_auth() -> bool:
    return is_truthy(os.getenv("LOCAL_LLM_REQUIRE_AUTH"), default=False)


def access_token() -> str:
    return os.getenv("LOCAL_LLM_ACCESS_TOKEN", "").strip()


def auth_status() -> dict[str, object]:
    required = require_auth()
    token_set = bool(access_token())
    status = "ok"
    message = "auth disabled"

    if required and not token_set:
        status = "error"
        message = "LOCAL_LLM_REQUIRE_AUTH=true but LOCAL_LLM_ACCESS_TOKEN is empty"
    elif required:
        message = "auth enabled"
    elif token_set:
        message = "auth disabled (token is configured)"

    return {
        "required": required,
        "tokenConfigured": token_set,
        "status": status,
        "message": message,
    }


def validate_authorization_header(authorization: str | None) -> tuple[bool, str | None]:
    if not require_auth():
        return True, None

    token = access_token()
    if not token:
        return False, "server auth configuration is invalid"

    if not authorization:
        return False, "missing Authorization header"

    scheme, _, candidate = authorization.partition(" ")
    if scheme.lower() != "bearer" or not candidate:
        return False, "Authorization header must be Bearer token"

    if not compare_digest(candidate.strip(), token):
        return False, "invalid access token"

    return True, None
