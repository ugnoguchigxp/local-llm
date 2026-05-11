from __future__ import annotations

from fastapi import Header, HTTPException, status

from shared.auth import validate_authorization_header


def require_api_auth(authorization: str | None = Header(default=None)) -> None:
    ok, reason = validate_authorization_header(authorization)
    if ok:
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=reason or "unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )
