"""Realtime notices to the app over Pusher (decisions 2026-10-17).

Each user listens on their own private channel, ``private-user-{id}``: the app asks
``POST /realtime/auth`` to sign the subscription, and only your own channel is
signed. The backend publishes through Pusher's HTTP API with the shared ``httpx``
client (the ``pusher`` library sends with a blocking ``requests`` session).

Best effort: publishing never raises and never holds a request for more than
``_TIMEOUT_SECONDS``. Without credentials nothing is sent and the app falls back to
polling. Payloads go through a third party: ids and trophy names only, never
personal data.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 2.0
CHANNEL_PREFIX = "private-user-"

_client: httpx.AsyncClient | None = None


def set_client(client: httpx.AsyncClient | None) -> None:
    global _client
    _client = client


def enabled() -> bool:
    return bool(
        (settings.PUSHER_APP_ID or "").strip()
        and (settings.PUSHER_KEY or "").strip()
        and (settings.PUSHER_SECRET or "").strip()
    )


def user_channel(user_id: str) -> str:
    return f"{CHANNEL_PREFIX}{user_id}"


def _sign(message: str) -> str:
    return hmac.new(settings.PUSHER_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()


def authorize_channel(socket_id: str, channel_name: str) -> dict[str, str]:
    """Pusher's private-channel auth: ``{"auth": "<key>:<hmac(socket_id:channel)>"}``."""
    return {"auth": f"{settings.PUSHER_KEY}:{_sign(f'{socket_id}:{channel_name}')}"}


def signed_query(method: str, path: str, body: bytes, *, timestamp: int) -> dict[str, str]:
    """Query string of a signed Pusher HTTP API call (auth_version 1.0)."""
    params = {
        "auth_key": settings.PUSHER_KEY,
        "auth_timestamp": str(timestamp),
        "auth_version": "1.0",
        "body_md5": hashlib.md5(body).hexdigest(),  # noqa: S324 — Pusher's protocol
    }
    query = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return {**params, "auth_signature": _sign(f"{method}\n{path}\n{query}")}


async def _post(user_id: str, event: str, data: dict[str, Any]) -> None:
    assert _client is not None
    path = f"/apps/{settings.PUSHER_APP_ID}/events"
    body = json.dumps(
        {"name": event, "channels": [user_channel(user_id)], "data": json.dumps(data)}
    ).encode()
    response = await _client.post(
        f"https://api-{settings.PUSHER_CLUSTER}.pusher.com{path}",
        params=signed_query("POST", path, body, timestamp=int(time.time())),
        content=body,
        headers={"Content-Type": "application/json"},
    )
    response.raise_for_status()


async def publish(user_id: str, event: str, data: dict[str, Any]) -> bool:
    """Send ``event`` to the user's channel. True when Pusher accepted it."""
    if _client is None or not enabled():
        return False
    try:
        await asyncio.wait_for(_post(user_id, event, data), timeout=_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — a notice must never break the request
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        logger.warning("Pusher publish failed (%s, status %s)", type(exc).__name__, status)
        return False
    return True
