from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import httpx


COMMANDCODE_MODEL_ID = "commandcode/deepseek-v4.1-flash"
COMMANDCODE_UPSTREAM_MODEL_ID = "deepseek/deepseek-v4.1-flash"
DEFAULT_BASE_URL = "https://api.commandcode.ai/provider/v1"
MAX_AUTH_FILE_BYTES = 64 * 1024


def _truthy(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def commandcode_enabled() -> bool:
    return _truthy("LOCAL_LLM_COMMANDCODE_ENABLED")


def is_commandcode_model(model: str | None) -> bool:
    return model in {COMMANDCODE_MODEL_ID, COMMANDCODE_UPSTREAM_MODEL_ID}


def public_commandcode_models() -> tuple[str, ...]:
    return (COMMANDCODE_MODEL_ID,) if commandcode_enabled() else ()


@dataclass(frozen=True)
class CommandCodeConfig:
    enabled: bool
    base_url: str
    timeout_seconds: float
    api_key: str | None
    credential_source: str | None

    @classmethod
    def from_env(cls) -> CommandCodeConfig:
        api_key, credential_source = _load_api_key()
        base_url = os.getenv("LOCAL_LLM_COMMANDCODE_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
        if not base_url:
            base_url = DEFAULT_BASE_URL
        if not base_url.startswith("https://") and not _truthy(
            "LOCAL_LLM_COMMANDCODE_ALLOW_INSECURE_HTTP"
        ):
            raise ValueError("Command Code base URL must use HTTPS")
        try:
            timeout_seconds = float(os.getenv("LOCAL_LLM_COMMANDCODE_TIMEOUT_SECONDS", "300"))
        except ValueError:
            timeout_seconds = 300.0
        if timeout_seconds <= 0 or timeout_seconds > 1800:
            timeout_seconds = 300.0
        return cls(
            enabled=commandcode_enabled(),
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            api_key=api_key,
            credential_source=credential_source,
        )

    def status(self) -> dict[str, object]:
        if not self.enabled:
            state = "disabled"
        elif self.api_key:
            state = "configured"
        else:
            state = "auth_required"
        return {
            "status": state,
            "baseUrl": self.base_url,
            "credentialSource": self.credential_source,
            "models": list(public_commandcode_models()),
        }


@dataclass
class RemoteResponse:
    status_code: int
    content: bytes
    content_type: str
    headers: dict[str, str]


@dataclass
class RemoteStream:
    status_code: int
    content_type: str
    headers: dict[str, str]
    response: httpx.Response
    client: httpx.AsyncClient

    async def body(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.response.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            await self.response.aclose()
            await self.client.aclose()

    async def read_error(self) -> bytes:
        try:
            return await self.response.aread()
        finally:
            await self.response.aclose()
            await self.client.aclose()


class CommandCodeProvider:
    def __init__(
        self,
        config: CommandCodeConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config or CommandCodeConfig.from_env()
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        if not self.config.enabled:
            raise RuntimeError("commandcode_disabled")
        if not self.config.api_key:
            raise RuntimeError("commandcode_auth_required")
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

    def prepare_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        prepared = dict(payload)
        prepared["model"] = COMMANDCODE_UPSTREAM_MODEL_ID
        prepared.pop("priority", None)
        return prepared

    async def request(self, endpoint: str, payload: dict[str, Any]) -> RemoteResponse:
        try:
            async with httpx.AsyncClient(
                timeout=self.config.timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    f"{self.config.base_url}/{endpoint}",
                    headers=self._headers(),
                    json=self.prepare_payload(payload),
                )
                return RemoteResponse(
                    status_code=response.status_code,
                    content=response.content,
                    content_type=response.headers.get("content-type", "application/json"),
                    headers=_forward_headers(response.headers),
                )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"commandcode_upstream_error: {exc}") from exc

    async def list_models(self) -> RemoteResponse:
        try:
            async with httpx.AsyncClient(
                timeout=self.config.timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    f"{self.config.base_url}/models",
                    headers=self._headers(),
                )
                return RemoteResponse(
                    status_code=response.status_code,
                    content=response.content,
                    content_type=response.headers.get("content-type", "application/json"),
                    headers=_forward_headers(response.headers),
                )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"commandcode_upstream_error: {exc}") from exc

    async def open_stream(self, endpoint: str, payload: dict[str, Any]) -> RemoteStream:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds),
            transport=self._transport,
        )
        try:
            request = client.build_request(
                "POST",
                f"{self.config.base_url}/{endpoint}",
                headers=self._headers(),
                json=self.prepare_payload(payload),
            )
            response = await client.send(request, stream=True)
            return RemoteStream(
                status_code=response.status_code,
                content_type=response.headers.get("content-type", "text/event-stream"),
                headers=_forward_headers(response.headers),
                response=response,
                client=client,
            )
        except httpx.HTTPError as exc:
            await client.aclose()
            raise RuntimeError(f"commandcode_upstream_error: {exc}") from exc
        except Exception:
            await client.aclose()
            raise


def commandcode_status() -> dict[str, object]:
    try:
        return CommandCodeConfig.from_env().status()
    except ValueError as exc:
        return {"status": "invalid_config", "detail": str(exc), "models": []}


def _load_api_key() -> tuple[str | None, str | None]:
    for name in ("COMMAND_CODE_API_KEY", "COMMANDCODE_API_KEY"):
        value = os.getenv(name, "").strip()
        if value:
            return value, f"env:{name}"

    configured = os.getenv("LOCAL_LLM_COMMANDCODE_AUTH_FILE", "").strip()
    auth_file = Path(configured).expanduser() if configured else Path.home() / ".commandcode" / "auth.json"
    try:
        if auth_file.is_symlink() or not auth_file.is_file():
            return None, None
        stat = auth_file.stat()
        if stat.st_mode & 0o077 or stat.st_size > MAX_AUTH_FILE_BYTES:
            return None, None
        raw = json.loads(auth_file.read_text(encoding="utf-8"))
        api_key = raw.get("apiKey") if isinstance(raw, dict) else None
        if isinstance(api_key, str) and api_key.strip():
            return api_key.strip(), "commandcode_auth_file"
    except (OSError, ValueError, TypeError):
        return None, None
    return None, None


def _forward_headers(headers: httpx.Headers) -> dict[str, str]:
    forwarded: dict[str, str] = {}
    for name in ("retry-after", "x-request-id"):
        value = headers.get(name)
        if value:
            forwarded[name] = value
    return forwarded
