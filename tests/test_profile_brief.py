"""Other people's e-mail is never in a list (#30 item 7, from #32).

`ProfileBrief` is what one user sees of another: followers, follow requests, the
attendee list of an event and the ticket scanner. It carried the e-mail, so the
creator of an event saw the address of everyone going. Until the public @username of
#32, lists show only the name.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import get_db_service
from app.api.rsvp import _row_to_rsvp_with_profile
from app.main import app
from app.models.schemas import ProfileBrief
from app.services.db_service import DBService

AUTH = {"Authorization": "Bearer token"}
PAYLOAD = {"sub": "user-1", "email": "me@example.com", "amr": [{"method": "oauth", "timestamp": 1}]}


def test_the_schema_has_no_email() -> None:
    assert "email" not in ProfileBrief.model_fields


@pytest.fixture
def db() -> Iterator[MagicMock]:
    deps.reset_verified_sessions_cache()
    fake = MagicMock()
    fake.get_account_state = AsyncMock(return_value="ok")
    app.dependency_overrides[get_db_service] = lambda: fake
    with patch("app.api.deps.DBService", return_value=fake), patch(
        "app.api.deps.decode_access_token", return_value=PAYLOAD
    ):
        yield fake
    app.dependency_overrides.clear()


@pytest.mark.parametrize("path", ["/social/followers", "/social/following"])
def test_social_lists_have_no_email(db: MagicMock, path: str) -> None:
    rows = [{"id": "user-2", "name": "Bia"}]
    db.get_followers_list = AsyncMock(return_value=rows)
    db.get_following_list = AsyncMock(return_value=rows)

    body = TestClient(app).get(path, headers=AUTH).json()

    assert body == [{"id": "user-2", "name": "Bia"}]


def test_follow_requests_have_no_email(db: MagicMock) -> None:
    db.get_pending_requests = AsyncMock(
        return_value=[
            {
                "follower_id": "user-2",
                "following_id": "user-1",
                "status": "pending",
                "created_at": None,
                "follower_name": "Bia",
            }
        ]
    )

    body = TestClient(app).get("/social/requests", headers=AUTH).json()

    assert body[0]["follower"] == {"id": "user-2", "name": "Bia"}


def test_attendee_rows_have_no_email() -> None:
    row = {
        "id": "rsvp-1",
        "event_id": "evt-1",
        "user_id": "user-2",
        "status": "confirmed",
        "waitlist_position": None,
        "profiles": {"id": "user-2", "name": "Bia"},
    }

    out = _row_to_rsvp_with_profile(row).model_dump()

    assert out["profile"] == {"id": "user-2", "name": "Bia"}


# ------------------------------------------- the database is not asked for it --


def _client() -> MagicMock:
    client = MagicMock()
    client.get = AsyncMock(
        return_value=httpx.Response(200, json=[], request=httpx.Request("GET", "http://x"))
    )
    return client


async def test_profile_batches_do_not_select_the_email() -> None:
    client = _client()

    await DBService(client)._fetch_profiles_batch(["user-2"], "jwt")

    assert client.get.await_args.kwargs["params"]["select"] == "id,name"


async def test_event_rsvps_do_not_select_the_email() -> None:
    client = _client()

    await DBService(client).get_event_rsvps("evt-1", "jwt")

    assert "email" not in client.get.await_args.kwargs["params"]["select"]


async def test_the_scanner_profile_does_not_select_the_email() -> None:
    client = _client()

    await DBService(client).get_rsvp_profile("user-2", "jwt")

    assert client.get.await_args.kwargs["params"]["select"] == "id,name"

