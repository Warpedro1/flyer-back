"""Start, end and check-in opening when creating an event (decisions 2026-10-12).

An event created in Flyer must have a start and an end; check-in opens at the time
the creator chose (the start by default) and closes at the end. Times without a
timezone are Recife time, which is how the app sends them.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user_with_token, get_db_service
from app.core.limiter import limiter
from app.main import app

BASE = {"title": "Aula de frevo", "lat": -8.06, "long": -34.87}


@pytest.fixture
def db() -> Iterator[MagicMock]:
    limiter.reset()
    fake = MagicMock()

    async def create(user_id: str, rows: list[dict[str, Any]], jwt: str) -> list[dict[str, Any]]:
        return [{"id": f"evt-{i}", **BASE, **row} for i, row in enumerate(rows)]

    fake.create_events_bulk = AsyncMock(side_effect=create)
    fake.insert_event_media_rows = AsyncMock()
    fake.create_trophy_templates = AsyncMock()
    fake.delete_events = AsyncMock()
    fake.set_events_embedding = AsyncMock()
    fake.batch_fetch_event_media = AsyncMock(return_value={})
    app.dependency_overrides[get_current_user_with_token] = lambda: ("user-1", "jwt")
    app.dependency_overrides[get_db_service] = lambda: fake
    with patch("app.api.events.ai_service.generate_embedding", AsyncMock(return_value=None)):
        yield fake
    app.dependency_overrides.clear()


def _create(**body: Any) -> httpx.Response:
    return TestClient(app).post("/events/", json={**BASE, **body})


def _rows(db: MagicMock) -> list[dict[str, Any]]:
    return db.create_events_bulk.await_args.args[1]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"event_date": "2026-10-16T20:00:00"},
        {"ends_at": "2026-10-16T23:00:00"},
    ],
)
def test_start_and_end_are_required(db: MagicMock, body: dict[str, str]) -> None:
    assert _create(**body).status_code == 422
    db.create_events_bulk.assert_not_called()


def test_the_end_must_be_after_the_start(db: MagicMock) -> None:
    response = _create(event_date="2026-10-16T20:00:00", ends_at="2026-10-16T20:00:00")

    assert response.status_code == 422
    db.create_events_bulk.assert_not_called()


def test_check_in_cannot_open_after_the_end(db: MagicMock) -> None:
    response = _create(
        event_date="2026-10-16T20:00:00",
        ends_at="2026-10-16T23:00:00",
        checkin_opens_at="2026-10-16T23:30:00",
    )

    assert response.status_code == 422


def test_times_without_a_timezone_are_recife_time(db: MagicMock) -> None:
    # Before: "20:00" was stored as 20:00 UTC and shown as 17:00.
    assert _create(event_date="2026-10-16T20:00:00", ends_at="2026-10-16T23:00:00").status_code == 201

    row = _rows(db)[0]
    assert row["event_date"] == "2026-10-16T20:00:00-03:00"
    assert row["ends_at"] == "2026-10-16T23:00:00-03:00"


def test_check_in_opens_at_the_start_by_default(db: MagicMock) -> None:
    _create(event_date="2026-10-16T20:00:00", ends_at="2026-10-16T23:00:00")

    assert _rows(db)[0]["checkin_opens_at"] == "2026-10-16T20:00:00-03:00"


def test_times_with_a_timezone_are_kept(db: MagicMock) -> None:
    _create(
        event_date="2026-10-16T23:00:00Z",
        ends_at="2026-10-17T02:00:00Z",
        checkin_opens_at="2026-10-16T22:30:00Z",
    )

    row = _rows(db)[0]
    assert row["event_date"] == "2026-10-16T20:00:00-03:00"
    assert row["ends_at"] == "2026-10-16T23:00:00-03:00"
    assert row["checkin_opens_at"] == "2026-10-16T19:30:00-03:00"


def test_every_occurrence_gets_its_own_end_and_opening(db: MagicMock) -> None:
    _create(
        event_date="2026-10-16T20:00:00",
        ends_at="2026-10-16T23:00:00",
        checkin_opens_at="2026-10-16T19:30:00",
        recurrence={"mode": "count", "every": "week", "occurrences": 3},
    )

    rows = _rows(db)
    assert [(r["event_date"], r["ends_at"], r["checkin_opens_at"]) for r in rows] == [
        ("2026-10-16T20:00:00-03:00", "2026-10-16T23:00:00-03:00", "2026-10-16T19:30:00-03:00"),
        ("2026-10-23T20:00:00-03:00", "2026-10-23T23:00:00-03:00", "2026-10-23T19:30:00-03:00"),
        ("2026-10-30T20:00:00-03:00", "2026-10-30T23:00:00-03:00", "2026-10-30T19:30:00-03:00"),
    ]


def test_a_weekly_series_keeps_the_events_duration(db: MagicMock) -> None:
    # Weekly series choose their own time of day: the duration comes from start → end.
    _create(
        event_date="2026-10-19T20:00:00",  # Monday
        ends_at="2026-10-19T22:30:00",
        recurrence={"mode": "weekly", "weekdays": [0, 2], "time_of_day": "19:00", "until": "2026-10-21"},
    )

    rows = _rows(db)
    assert [(r["event_date"], r["ends_at"]) for r in rows] == [
        ("2026-10-19T19:00:00-03:00", "2026-10-19T21:30:00-03:00"),
        ("2026-10-21T19:00:00-03:00", "2026-10-21T21:30:00-03:00"),
    ]


def test_media_that_cannot_be_saved_does_not_leave_the_event_behind(db: MagicMock) -> None:
    db.insert_event_media_rows.side_effect = RuntimeError("db down")

    response = _create(
        event_date="2026-10-16T20:00:00",
        ends_at="2026-10-16T23:00:00",
        media=[{"media_url": "user-1/foto.jpg", "type": "image"}],
    )

    assert response.status_code == 500
    db.delete_events.assert_awaited_once_with(["evt-0"], "user-1")


def test_the_new_event_says_when_check_in_opens(db: MagicMock) -> None:
    body = _create(event_date="2030-01-10T20:00:00", ends_at="2030-01-10T23:00:00").json()

    assert body["checkin_status"] == "not_yet"
    assert body["checkin_opens_at"].startswith("2030-01-10T20:00:00")
    assert body["ends_at"].startswith("2030-01-10T23:00:00")
