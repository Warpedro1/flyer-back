"""Errors with a stable code (#30).

The app shows ``detail`` and keeps ``code`` and ``request_id`` to tell the user (and
us) what happened: ``{"detail": "...", "code": "session_invalid", "request_id": "..."}``.
``detail`` stays a string, so screens that only read it keep working.

Also here: the handlers that record 5xx in ``app_errors`` and give the rate limit the
same shape.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler as default_http_exception_handler,
)
from fastapi.responses import JSONResponse, Response
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.request_context import current_request_id, route_of

SESSION_INVALID = "session_invalid"
SESSION_INVALID_MESSAGE = "Sessão inválida: entre novamente."
RATE_LIMITED = "rate_limited"
RATE_LIMITED_MESSAGE = "Muitas tentativas seguidas. Aguarde um pouco e tente de novo."


class ApiError(Exception):
    """An HTTP error the app can recognise by ``code``."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def session_invalid() -> ApiError:
    """The session belongs to an account that no longer exists: sign in again."""
    return ApiError(401, SESSION_INVALID, SESSION_INVALID_MESSAGE)


def error_body(message: str, code: str) -> dict[str, str | None]:
    return {"detail": message, "code": code, "request_id": current_request_id()}


async def _record_5xx(request: Request, status: int, code: str, error_type: str, message: str) -> None:
    from app.services import error_log

    await error_log.record_error(
        source="api",
        code=code,
        status=status,
        error_type=error_type,
        message=message,
        method=request.method,
        route=route_of(request.scope),
    )


async def api_error_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    if exc.status_code >= 500:
        await _record_5xx(request, exc.status_code, exc.code, "ApiError", exc.message)
    return JSONResponse(error_body(exc.message, exc.code), status_code=exc.status_code)


async def http_exception_handler(request: Request, exc: Exception) -> Response:
    """FastAPI's own handler, after recording the 5xx raised on purpose (503, 502…)."""
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code >= 500:
        detail = exc.detail
        code = detail.get("code") if isinstance(detail, dict) else None
        await _record_5xx(
            request, exc.status_code, str(code or f"http_{exc.status_code}"), "HTTPException", str(detail)
        )
    return await default_http_exception_handler(request, exc)


async def rate_limit_handler(request: Request, exc: Exception) -> Response:
    """SlowAPI's 429 with ``detail`` and ``code`` (it answered ``{"error": ...}``)."""
    assert isinstance(exc, RateLimitExceeded)
    response = JSONResponse(error_body(RATE_LIMITED_MESSAGE, RATE_LIMITED), status_code=429)
    return request.app.state.limiter._inject_headers(response, request.state.view_rate_limit)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RateLimitExceeded, rate_limit_handler)
