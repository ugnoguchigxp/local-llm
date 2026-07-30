from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from fastapi import WebSocket

from shared.auth import require_auth, validate_authorization_header


@dataclass
class EphemeralTokenStore:
    ttl_seconds: float = 60.0
    _tokens: dict[str, float] = field(default_factory=dict)

    def issue(self) -> tuple[str, int]:
        self.cleanup()
        token = f"st_{secrets.token_urlsafe(24)}"
        self._tokens[token] = time.monotonic() + self.ttl_seconds
        return token, int(self.ttl_seconds)

    def consume(self, token: str | None) -> bool:
        self.cleanup()
        if not token:
            return False
        expires = self._tokens.pop(token, None)
        return expires is not None and expires > time.monotonic()

    def cleanup(self) -> None:
        now = time.monotonic()
        expired = [token for token, expires in self._tokens.items() if expires <= now]
        for token in expired:
            self._tokens.pop(token, None)


def websocket_authorized(
    websocket: WebSocket,
    token_store: EphemeralTokenStore,
) -> bool:
    if not require_auth():
        return True
    authorization = websocket.headers.get("authorization")
    ok, _reason = validate_authorization_header(authorization)
    if ok:
        return True
    return token_store.consume(websocket.query_params.get("session_token"))
