"""Meus eventos: the ones you created and the ones you are going to (#40, 2026-10-17).

* ``GET /events/mine`` keeps its answer without parameters (what you created).
* ``role=going``: events with your RSVP ``confirmed``, ``waitlisted``, ``called`` or
  ``admitted`` (never ``cancelled`` nor ``no_show``), with your status and queue number.
* ``role=all``: both, each event once.
* ``when=upcoming|past``: split at the end of the event (``ends_at``, or the start + 4 h
  without one), soonest first for upcoming and latest first for past.
* Places never come (their check-ins are trophies); the trophy now points at its event.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import get_db_service
from app.core.limiter import limiter
from app.main import app
from app.services.db_service import DBService

AUTH = {"Authorization": "Bearer token"}
PAYLOAD = {"sub": "user-1", "amr": [{"method": "oauth", "timestamp": 1}]}
NOW = datetime.now(timezone.utc)


def _event(event_id: str, *, days: int, creator: str = "someone") -> dict[str, Any]:
    start = NOW + timedelta(days=days)
    return {
        "id": event_id,
        "kind": "event",
        "creator_id": creator,
        "title": f"Evento {event_id}",
        "lat": -8.06,
        "long": -34.87,
        "event_date": start.isoformat(),
        "ends_at": (start + timedelta(hours=2)).isoformat(),
    }


@pytest.fixture
def db() -> Iterator[MagicMock]:
    limiter.reset()
    deps.reset_verified_sessions_cache()
    fake = MagicMock()
    fake.get_account_state = AsyncMock(return_value="ok")
    fake.get_events_by_creator = AsyncMock(return_value=[])
    fake.get_events_going = AsyncMock(return_value=[])
    fake.batch_fetch_event_media = AsyncMock(return_value={})
    app.dependency_overrides[get_db_service] = lambda: fake
    with patch("app.api.deps.DBService", return_value=fake), patch(
        "app.api.deps.decode_access_token", return_value=PAYLOAD
    ):
        yield fake
    app.dependency_overrides.clear()


def _get(query: str = "") -> httpx.Response:
    return TestClient(app).get(f"/events/mine{query}", headers=AUTH)


# ------------------------------------------------------------------ route --


def test_without_parameters_it_is_what_you_created(db: MagicMock) -> None:
    db.get_events_by_creator.return_value = [_event("e1", days=3, creator="user-1")]

    response = _get()

    assert response.status_code == 200
    assert [e["id"] for e in response.json()] == ["e1"]
    db.get_events_by_creator.assert_awaited_once_with("user-1", "token", limit=50, offset=0, when=None)
    db.get_events_going.assert_not_called()


def test_going_brings_your_status_and_queue_number(db: MagicMock) -> None:
    db.get_events_going.return_value = [
        {**_event("e2", days=5), "_rsvp": {"status": "waitlisted", "waitlist_position": 3}},
        {**_event("e1", days=1), "_rsvp": {"status": "confirmed", "waitlist_position": None}},
    ]

    body = _get("?role=going&when=upcoming").json()

    assert [e["id"] for e in body] == ["e1", "e2"]
    assert body[0]["my_rsvp"] == {"status": "confirmed", "waitlist_position": None}
    assert body[1]["my_rsvp"] == {"status": "waitlisted", "waitlist_position": 3}
    db.get_events_going.assert_awaited_once_with("user-1", "token", when="upcoming", limit=50)
    db.get_events_by_creator.assert_not_called()


def test_all_merges_both_without_repeating(db: MagicMock) -> None:
    mine = _event("e1", days=2, creator="user-1")
    db.get_events_by_creator.return_value = [mine, _event("e3", days=4, creator="user-1")]
    db.get_events_going.return_value = [
        {**mine, "_rsvp": {"status": "confirmed", "waitlist_position": None}},
        {**_event("e2", days=3), "_rsvp": {"status": "called", "waitlist_position": None}},
    ]

    body = _get("?role=all&when=upcoming").json()

    assert [e["id"] for e in body] == ["e1", "e2", "e3"]
    assert body[0]["my_rsvp"]["status"] == "confirmed"
    assert body[2]["my_rsvp"] is None


def test_past_is_latest_first(db: MagicMock) -> None:
    db.get_events_by_creator.return_value = [_event("old", days=-30), _event("recent", days=-2)]

    body = _get("?role=created&when=past").json()

    assert [e["id"] for e in body] == ["recent", "old"]
    db.get_events_by_creator.assert_awaited_once_with("user-1", "token", limit=50, offset=0, when="past")


def test_pages_through_the_merged_list(db: MagicMock) -> None:
    db.get_events_by_creator.return_value = [_event(f"c{i}", days=i, creator="user-1") for i in range(1, 4)]
    db.get_events_going.return_value = [
        {**_event(f"g{i}", days=i), "_rsvp": {"status": "confirmed", "waitlist_position": None}} for i in range(1, 4)
    ]

    body = _get("?role=all&when=upcoming&limit=2&offset=2").json()

    assert len(body) == 2
    db.get_events_by_creator.assert_awaited_once_with("user-1", "token", limit=4, offset=0, when="upcoming")
    db.get_events_going.assert_awaited_once_with("user-1", "token", when="upcoming", limit=4)


@pytest.mark.parametrize("query", ["?role=everyone", "?when=tomorrow", "?limit=0", "?limit=101", "?offset=-1"])
def test_bad_filters_are_422(db: MagicMock, query: str) -> None:
    assert _get(query).status_code == 422


# -------------------------------------------------------------- db requests --


def _client(rows: list[dict[str, Any]]) -> MagicMock:
    client = MagicMock()
    client.get = AsyncMock(return_value=httpx.Response(200, json=rows, request=httpx.Request("GET", "http://x")))
    return client


async def test_going_asks_only_active_rsvps_of_events() -> None:
    rsvps = [{"status": "confirmed", "waitlist_position": None, "events": _event("e1", days=1)}]
    client = _client(rsvps)

    rows = await DBService(client).get_events_going("user-1", "jwt", when="upcoming", limit=10)

    params = client.get.await_args.kwargs["params"]
    assert client.get.await_args.args[0] == "/rest/v1/event_rsvps"
    assert params["user_id"] == "eq.user-1"
    assert params["status"] == "in.(confirmed,waitlisted,called,admitted)"
    assert params["select"] == "status,waitlist_position,events!inner(*)"
    assert params["events.kind"] == "eq.event"
    assert params["events.or"].startswith("(ends_at.gte.")
    assert rows == [{**_event("e1", days=1), "_rsvp": {"status": "confirmed", "waitlist_position": None}}]


async def test_past_and_upcoming_split_at_the_end_of_the_event() -> None:
    client = _client([])
    db = DBService(client)

    await db.get_events_by_creator("user-1", "jwt", when="past")
    past = client.get.await_args.kwargs["params"]
    await db.get_events_by_creator("user-1", "jwt", when="upcoming")
    upcoming = client.get.await_args.kwargs["params"]

    # Without an end, the start + 4 h (as imported events close).
    assert past["or"].startswith("(ends_at.lt.") and "and(ends_at.is.null,event_date.lt." in past["or"]
    assert past["order"] == "event_date.desc"
    assert "and(ends_at.is.null,event_date.gte." in upcoming["or"]
    assert "and(ends_at.is.null,event_date.is.null)" in upcoming["or"]
    assert upcoming["order"] == "event_date.asc"
    assert upcoming["kind"] == "eq.event"


async def test_without_when_the_creator_list_is_as_before() -> None:
    client = _client([])

    await DBService(client).get_events_by_creator("user-1", "jwt")

    params = client.get.await_args.kwargs["params"]
    assert params["order"] == "created_at.desc"
    assert "or" not in params


async def test_trophies_bring_their_event() -> None:
    client = _client([])

    await DBService(client).get_user_trophies("user-1", "jwt")

    assert "trophy_templates(name,description,icon_url,event_id)" in client.get.await_args.kwargs["params"]["select"]


def test_the_trophy_list_has_the_event_id(db: MagicMock) -> None:
    db.get_user_trophies = AsyncMock(
        return_value=[
            {"id": "t1", "template_id": "tpl1", "acquired_at": None, "trophy_templates": {"name": "Presença", "event_id": "e1"}},
            {"id": "t2", "template_id": "tpl2", "acquired_at": None, "trophy_templates": {"name": "Antigo"}},
        ]
    )
    db.sign_storage_paths = AsyncMock(return_value={})

    body = TestClient(app).get("/trophies/", headers=AUTH).json()

    assert [t["event_id"] for t in body] == ["e1", None]
