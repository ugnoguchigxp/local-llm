from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


@dataclass
class SpeechAPIError(Exception):
    status_code: int
    message: str
    code: str
    error_type: str = "invalid_request_error"
    param: str | None = None


def error_payload(
    message: str,
    *,
    code: str,
    error_type: str,
    param: str | None = None,
) -> dict[str, object]:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
        }
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(SpeechAPIError)
    async def speech_error_handler(
        _request: Request,
        exc: SpeechAPIError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(
                exc.message,
                code=exc.code,
                error_type=exc.error_type,
                param=exc.param,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        location = first.get("loc") or []
        param = ".".join(
            str(item) for item in location if item not in {"body", "query"}
        )
        return JSONResponse(
            status_code=422,
            content=error_payload(
                str(first.get("msg") or "request validation failed"),
                code="validation_error",
                error_type="invalid_request_error",
                param=param or None,
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        _request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict):
            message = str(detail.get("message") or detail.get("detail") or detail)
            code = str(detail.get("code") or "http_error")
        else:
            message = str(detail)
            code = "http_error"
        error_type = (
            "authentication_error"
            if exc.status_code == 401
            else "invalid_request_error"
        )
        if exc.status_code >= 500:
            error_type = "server_error"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(message, code=code, error_type=error_type),
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        logger.exception(
            "Unhandled speech API error for %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
        )
        return JSONResponse(
            status_code=500,
            content=error_payload(
                "An internal server error occurred",
                code="internal_server_error",
                error_type="server_error",
            ),
        )
