from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse

from core.commandcode_provider import CommandCodeProvider


async def proxy_commandcode(
    endpoint: str,
    payload: dict[str, Any],
    *,
    stream: bool,
):
    try:
        provider = CommandCodeProvider()
        if stream:
            upstream = await provider.open_stream(endpoint, payload)
            if upstream.status_code >= 400:
                content = await upstream.read_error()
                return Response(
                    content=content,
                    status_code=upstream.status_code,
                    media_type=upstream.content_type,
                    headers=upstream.headers,
                )
            return StreamingResponse(
                upstream.body(),
                status_code=upstream.status_code,
                media_type=upstream.content_type,
                headers=upstream.headers,
            )

        upstream = await provider.request(endpoint, payload)
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.content_type,
            headers=upstream.headers,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "commandcode_invalid_config", "message": str(exc)},
        ) from exc
    except RuntimeError as exc:
        code = str(exc)
        if code == "commandcode_disabled":
            status_code = 404
        elif code == "commandcode_auth_required":
            status_code = 503
        else:
            status_code = 502
        raise HTTPException(
            status_code=status_code,
            detail={"code": code.split(":", 1)[0], "message": code},
        ) from exc
