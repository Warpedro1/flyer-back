"""What one person sees of another: the public identity (#30, #32; decisions 2026-11-01).

`ProfileBrief` is what one user sees of another: followers, follow requests, the
attendee list of an event and the ticket scanner. It carried the e-mail (#30), so the
creator of an event saw the address of everyone going; since #32 it is the @username,
the nickname and the photo, never the name (private), e-mail, CPF or phone. Photos are
signed URLs from the avatars bucket; one that cannot be signed is left out, never sent
as its raw path.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import get_db_service
from app.api.rsvp import _row_to_rsvp_with_profile
from app.main import app
from app.models.schemas import ProfileBrief
from app.services.db_service import PUBLIC_PROFILE_SELECT, DBService

AUTH = {"Authorization": "Bearer token"}
PAYLOAD = {"sub": "user-1", "email": "me@example.com", "amr": [{"method": "oauth", "timestamp": 1}]}
BIA = {"id": "user-2", "username": "bia.souza", "nickname": "Bia", "avatar_path": "user-2/photo.webp"}
BIA_OUT = {
    "id": "user-2",
    "username": "bia.souza",
    "nickname": "Bia",
    "avatar_url": "https://storage.example/user-2/photo.webp?token=t",
}


def _signed(paths: list[str], **_: Any) -> dict[str, str]:
    return {p: f"https://storage.example/{p}?token=t" for p in paths}


def test_the_schema_is_the_public_identity_only() -> None:
    assert set(ProfileBrief.model_fields) == {"id", "username", "nickname", "avatar_url"}


def test_the_database_is_asked_for_the_public_identity_only() -> None:
    assert set(PUBLIC_PROFILE_SELECT.split(",")) == {"id", "username", "nickname", "avatar_path"}


@pytest.fixture
def db() -> Iterator[MagicMock]:
    deps.reset_verified_sessions_cache()
    fake = MagicMock()
    fake.get_account_state = AsyncMock(return_value="ok")
    fake.sign_media_paths = AsyncMock(side_effect=_signed)
    app.dependency_overrides[get_db_service] = lambda: fake
    with patch("app.api.deps.DBService", return_value=fake), patch(
        "app.api.deps.decode_access_token", return_value=PAYLOAD
    ):
        yield fake
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path", ["/social/followers", "/social/following"])
def test_social_lists_show_the_public_identity(db: MagicMock, path: str) -> None:
    db.get_followers_list = AsyncMock(return_value=[BIA])
    db.get_following_list = AsyncMock(return_value=[BIA])

    body = TestClient(app).get(path, headers=AUTH).json()

    assert body == [BIA_OUT]
    assert db.sign_media_paths.await_args.kwargs == {"bucket": "avatars"}


def test_follow_requests_show_the_public_identity(db: MagicMock) -> None:
    db.get_pending_requests = AsyncMock(
        return_value=[
            {
                "follower_id": "user-2",
                "following_id": "user-1",
                "status": "pending",
                "created_at": None,
                "follower": BIA,
            }
        ]
    )

    body = TestClient(app).get("/social/requests", headers=AUTH).json()

    assert body[0]["follower"] == BIA_OUT


def test_a_photo_that_cannot_be_signed_is_left_out(db: MagicMock) -> None:
    db.sign_media_paths.side_effect = RuntimeError("storage down")
    db.get_followers_list = AsyncMock(return_value=[BIA])

    body = TestClient(app).get("/social/followers", headers=AUTH).json()

    assert body == [{**BIA_OUT, "avatar_url": None}]


def test_attendee_rows_show_the_public_identity() -> None:
    row = {
        "id": "rsvp-1",
        "event_id": "evt-1",
        "user_id": "user-2",
        "status": "confirmed",
        "waitlist_position": None,
        "profiles": BIA,
    }

    out = _row_to_rsvp_with_profile(row, _signed([BIA["avatar_path"]])).model_dump()

    assert out["profile"] == BIA_OUT


def test_the_attendee_list_signs_every_photo_at_once(db: MagicMock) -> None:
    rows = [
        {"id": f"rsvp-{i}", "event_id": "evt-1", "user_id": f"user-{i}", "status": "confirmed",
         "waitlist_position": None, "profiles": {**BIA, "id": f"user-{i}", "avatar_path": f"user-{i}/p.webp"}}
        for i in range(3)
    ]
    db.get_event_rsvps = AsyncMock(return_value=rows)
    db.sweep_expired_calls = AsyncMock()

    with patch("app.api.rsvp.require_event_creator", AsyncMock(return_value={"capacity": None})):
        body = TestClient(app).get("/events/evt-1/attendees", headers=AUTH).json()

    assert [p["profile"]["avatar_url"] for p in body["confirmed"]] == [
        f"https://storage.example/user-{i}/p.webp?token=t" for i in range(3)
    ]
    db.sign_media_paths.assert_awaited_once()


# ------------------------------------------- the database is not asked for more --


def _client() -> MagicMock:
    client = MagicMock()
    client.get = AsyncMock(
        return_value=httpx.Response(200, json=[], request=httpx.Request("GET", "http://x"))
    )
    return client


async def test_profile_batches_select_the_public_identity() -> None:
    client = _client()

    await DBService(client)._fetch_profiles_batch(["user-2"], "jwt")

    assert client.get.await_args.kwargs["params"]["select"] == PUBLIC_PROFILE_SELECT


async def test_event_rsvps_select_the_public_identity() -> None:
    client = _client()

    await DBService(client).get_event_rsvps("evt-1", "jwt")

    assert f"profiles({PUBLIC_PROFILE_SELECT})" in client.get.await_args.kwargs["params"]["select"]


async def test_the_scanner_profile_selects_the_public_identity() -> None:
    client = _client()

    await DBService(client).get_rsvp_profile("user-2", "jwt")

    assert client.get.await_args.kwargs["params"]["select"] == PUBLIC_PROFILE_SELECT
