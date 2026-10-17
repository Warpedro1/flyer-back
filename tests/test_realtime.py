"""Realtime notices over Pusher (decisions 2026-10-17).

The attendee's ticket screen hears that it was scanned on its own private channel,
``private-user-{id}``. The backend signs channel subscriptions (only your own
channel) and publishes through Pusher's HTTP API with the shared ``httpx`` client.
The signatures are checked against the official ``pusher`` library.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pusher
import pytest
from fastapi.testclient import TestClient
from pusher.http import Request as PusherRequest

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.limiter import limiter
from app.main import app
from app.services import realtime

KEY = "278d425bdf160c739803"
SECRET = "7ad3773142a6692b25b8"


@pytest.fixture(autouse=True)
def _pusher_settings() -> Iterator[None]:
    with patch.object(settings, "PUSHER_APP_ID", "123"), patch.object(
        settings, "PUSHER_KEY", KEY
    ), patch.object(settings, "PUSHER_SECRET", SECRET), patch.object(settings, "PUSHER_CLUSTER", "sa1"):
        yield


@pytest.fixture
def client() -> Iterator[TestClient]:
    limiter.reset()
    app.dependency_overrides[get_current_user] = lambda: "user-1"
    yield TestClient(app)
    app.dependency_overrides.clear()


# ----------------------------------------------------------------- signing --


def test_the_channel_signature_matches_the_pusher_library() -> None:
    official = pusher.Pusher(app_id="123", key=KEY, secret=SECRET, cluster="sa1")

    ours = realtime.authorize_channel("1234.1234", "private-user-1")

    assert ours == official.authenticate(channel="private-user-1", socket_id="1234.1234")


def test_the_publish_signature_matches_the_pusher_library() -> None:
    official = pusher.Pusher(app_id="123", key=KEY, secret=SECRET, cluster="sa1")
    body = {"name": "checkin:done", "channels": ["private-user-1"], "data": json.dumps({"a": 1})}
    with patch("time.time", return_value=1_700_000_000):
        expected = PusherRequest(official._pusher_client, "POST", "/apps/123/events", body)

    ours = realtime.signed_query("POST", "/apps/123/events", expected.body, timestamp=1_700_000_000)

    assert ours == expected.query_params


def test_each_user_has_their_own_channel() -> None:
    assert realtime.user_channel("abc") == "private-user-abc"


# ----------------------------------------------------------------- publish --


async def test_publish_posts_the_event_to_the_users_channel() -> None:
    sent: list[httpx.Request] = []

    async def post(url: str, **kwargs: Any) -> httpx.Response:
        sent.append(httpx.Request("POST", url, params=kwargs.get("params"), content=kwargs.get("content")))
        return httpx.Response(200, json={}, request=sent[-1])

    shared = MagicMock()
    shared.post = AsyncMock(side_effect=post)
    realtime.set_client(shared)
    try:
        ok = await realtime.publish("user-9", "checkin:done", {"event_id": "evt-1"})
    finally:
        realtime.set_client(None)

    assert ok is True
    request = sent[0]
    assert str(request.url).startswith("https://api-sa1.pusher.com/apps/123/events?")
    body = json.loads(request.content)
    assert body["name"] == "checkin:done"
    assert body["channels"] == ["private-user-user-9"]
    assert json.loads(body["data"]) == {"event_id": "evt-1"}
    assert "auth_signature" in request.url.params


async def test_a_pusher_failure_never_raises() -> None:
    shared = MagicMock()
    shared.post = AsyncMock(side_effect=httpx.ConnectError("down"))
    realtime.set_client(shared)
    try:
        ok = await realtime.publish("user-9", "checkin:done", {"event_id": "evt-1"})
    finally:
        realtime.set_client(None)

    assert ok is False


async def test_a_pusher_error_answer_is_a_failure() -> None:
    shared = MagicMock()
    shared.post = AsyncMock(
        return_value=httpx.Response(401, text="bad key", request=httpx.Request("POST", "https://x"))
    )
    realtime.set_client(shared)
    try:
        ok = await realtime.publish("user-9", "checkin:done", {})
    finally:
        realtime.set_client(None)

    assert ok is False


async def test_without_credentials_nothing_is_sent() -> None:
    shared = MagicMock()
    shared.post = AsyncMock()
    realtime.set_client(shared)
    try:
        with patch.object(settings, "PUSHER_SECRET", ""):
            ok = await realtime.publish("user-9", "checkin:done", {})
    finally:
        realtime.set_client(None)

    assert ok is False
    shared.post.assert_not_called()


# ------------------------------------------------------------------ routes --


def test_config_gives_the_public_key_and_cluster(client: TestClient) -> None:
    response = client.get("/realtime/config")

    assert response.status_code == 200
    assert response.json() == {"enabled": True, "key": KEY, "cluster": "sa1"}


def test_config_says_disabled_without_credentials(client: TestClient) -> None:
    with patch.object(settings, "PUSHER_KEY", ""):
        response = client.get("/realtime/config")

    assert response.json() == {"enabled": False, "key": None, "cluster": None}


def test_auth_signs_your_own_channel(client: TestClient) -> None:
    response = client.post(
        "/realtime/auth", json={"socket_id": "1234.1234", "channel_name": "private-user-user-1"}
    )

    assert response.status_code == 200
    assert response.json() == realtime.authorize_channel("1234.1234", "private-user-user-1")


@pytest.mark.parametrize("channel", ["private-user-user-2", "private-user-user-1x", "presence-x", "public"])
def test_auth_refuses_any_other_channel(client: TestClient, channel: str) -> None:
    response = client.post("/realtime/auth", json={"socket_id": "1234.1234", "channel_name": channel})

    assert response.status_code == 403
    assert response.json()["code"] == "channel_forbidden"


def test_auth_refuses_a_malformed_socket_id(client: TestClient) -> None:
    response = client.post(
        "/realtime/auth", json={"socket_id": "abc:def", "channel_name": "private-user-user-1"}
    )

    assert response.status_code == 422


def test_auth_is_unavailable_without_credentials(client: TestClient) -> None:
    with patch.object(settings, "PUSHER_SECRET", ""):
        response = client.post(
            "/realtime/auth", json={"socket_id": "1234.1234", "channel_name": "private-user-user-1"}
        )

    assert response.status_code == 503
    assert response.json()["code"] == "realtime_unavailable"
