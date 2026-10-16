"""Per-request context: the `request_id` and the user as a hash (#30, #25 phase 1).

Every response carries ``X-Request-Id``. The same id is in the log lines of that
request and in its ``app_errors`` row, so a reference the user quotes ("ref
a1b2c3…") leads straight to what happened.

The context is one mutable object per request held in a ``ContextVar``: route code
runs in child tasks (SlowAPI's ``BaseHTTPMiddleware``), which see the object but
whose own ``ContextVar.set`` would not reach back to this middleware.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-Id"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")

INTERNAL_ERROR_CODE = "internal_error"
INTERNAL_ERROR_MESSAGE = "Erro interno. Tenta de novo daqui a pouco."

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("app.access")


@dataclass
class RequestContext:
    request_id: str
    user_hash: str | None = None


_context: ContextVar[RequestContext | None] = ContextVar("flyer_request_context", default=None)


def hash_user_id(user_id: str) -> str:
    """The user in logs and error records: never the raw id (LGPD, decision #25)."""
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]


def current_request_id() -> str | None:
    ctx = _context.get()
    return ctx.request_id if ctx else None


def current_user_hash() -> str | None:
    ctx = _context.get()
    return ctx.user_hash if ctx else None


def set_current_user(user_id: str) -> None:
    """Called once the token is validated (`app/api/deps.py`)."""
    ctx = _context.get()
    if ctx is not None:
        ctx.user_hash = hash_user_id(user_id)


def route_of(scope: Scope) -> str:
    """The route template (`/events/{event_id}`), or the raw path before routing."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else str(scope.get("path", ""))


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"x-request-id":
            candidate = value.decode("latin-1")
            return candidate if _VALID_REQUEST_ID.match(candidate) else None
    return None


class RequestContextMiddleware:
    """Request id on every response, one access log line, and the last-resort 500.

    Registered inside ``CORSMiddleware``: an unhandled exception becomes a JSON 500
    *here*, so it still gets the CORS headers. Starlette's own handler for
    ``Exception`` runs outside every middleware, and the browser saw that 500 as a
    "network error" without a body.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        ctx = RequestContext(request_id=_incoming_request_id(scope) or uuid.uuid4().hex[:12])
        token = _context.set(ctx)
        start = time.perf_counter()
        state: dict[str, Any] = {"status": 500, "started": False}

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["started"] = True
                state["status"] = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, ctx.request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            if state["started"]:
                raise
            await self._internal_error(scope, receive, send_with_id, exc, ctx)
        finally:
            access_logger.info(
                "%s %s %s",
                scope.get("method"),
                route_of(scope),
                state["status"],
                extra={
                    "method": scope.get("method"),
                    "route": route_of(scope),
                    "status": state["status"],
                    "duration_ms": round((time.perf_counter() - start) * 1000),
                },
            )
            _context.reset(token)

    @staticmethod
    async def _internal_error(
        scope: Scope, receive: Receive, send: Send, exc: Exception, ctx: RequestContext
    ) -> None:
        # Imported here: error_log reaches the database layer, which the app imports later.
        from app.services import error_log

        method = str(scope.get("method", ""))
        route = route_of(scope)
        logger.exception("Unhandled error on %s %s", method, route)
        await error_log.record_error(
            source="api",
            code=INTERNAL_ERROR_CODE,
            status=500,
            error_type=type(exc).__name__,
            message=str(exc),
            method=method,
            route=route,
        )
        response = JSONResponse(
            {"detail": INTERNAL_ERROR_MESSAGE, "code": INTERNAL_ERROR_CODE, "request_id": ctx.request_id},
            status_code=500,
        )
        await response(scope, receive, send)
