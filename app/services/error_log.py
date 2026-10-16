"""Error records in `app_errors`, kept 30 days (decisions 2026-10-16, #25 phase 1).

Best effort: recording never raises and never holds a request for more than
``_TIMEOUT_SECONDS``. Each row has the ``request_id`` and the user as a hash, and
the text goes through ``scrub`` (no e-mail, CPF or phone).

The shared ``httpx.AsyncClient`` is set by the app's lifespan; without it (unit
tests, scripts) nothing is recorded.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

import httpx

from app.core.log_scrub import scrub, scrub_value
from app.core.request_context import current_request_id, current_user_hash
from app.services.db_service import DBService

logger = logging.getLogger(__name__)

Source = Literal["api", "ai", "client"]

_TIMEOUT_SECONDS = 2.0
_MESSAGE_MAX = 2000

_client: httpx.AsyncClient | None = None


def set_client(client: httpx.AsyncClient | None) -> None:
    global _client
    _client = client


async def record_error(
    *,
    source: Source,
    code: str | None = None,
    message: str | None = None,
    error_type: str | None = None,
    status: int | None = None,
    method: str | None = None,
    route: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    client = _client
    if client is None:
        return
    row = {
        "request_id": current_request_id(),
        "source": source,
        "method": method,
        "route": route,
        "status": status,
        "code": code,
        "error_type": error_type,
        "message": scrub(message)[:_MESSAGE_MAX] if message else None,
        "user_hash": current_user_hash(),
        "details": scrub_value(details) if details is not None else None,
    }
    try:
        await asyncio.wait_for(DBService(client).record_app_error(row), timeout=_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — recording must never break the request
        logger.warning("Could not record an app error (%s)", type(exc).__name__)
