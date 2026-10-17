"""Trophies: default trophy on check-in, custom trophy on event creation, signed icons.

Decisions (2026-10-12):
  * every event gives a trophy at check-in; without a custom one, a default
    "Presença: <título>" is created on the first check-in;
  * a recurring event gets one trophy per occurrence (each date is its own event);
  * the creator may customise name, description and icon (an image they uploaded).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user_with_token, get_db_service
from app.core.limiter import limiter
from app.main import app
from app.services.db_service import DBService

NOW = datetime.now(timezone.utc)
# Check-in is open: the event started an hour ago and ends in two.
EVENT = {
    "id": "evt-1",
    "kind": "event",
    "title": "Noite de Frevo",
    "lat": -8.06,
    "long": -34.87,
    "event_date": (NOW - timedelta(hours=1)).isoformat(),
    "ends_at": (NOW + timedelta(hours=2)).isoformat(),
    "checkin_opens_at": (NOW - timedelta(hours=1)).isoformat(),
    "checkin_closed_at": None,
}
NEAR = {"event_id": "evt-1", "lat": -8.06, "long": -34.87}
VEC = [0.1] * 1536
TEMPLATE = {"id": "tpl-1", "name": "Frevo!", "description": "Dançou muito", "icon_url": "user-1/icon.png"}
# Start and end are required to create an event (decision 2026-10-12).
WHEN = {"event_date": "2026-10-16T20:00:00", "ends_at": "2026-10-16T23:00:00"}


@pytest.fixture
def db() -> Iterator[MagicMock]:
    limiter.reset()
    fake = MagicMock()
    fake.get_event_by_id = AsyncMock(return_value=EVENT)
    fake.get_trophy_template_by_event = AsyncMock(return_value=TEMPLATE)
    fake.ensure_default_trophy_template = AsyncMock(
        return_value={"id": "tpl-new", "name": "Presença: Noite de Frevo", "description": None, "icon_url": None}
    )
    fake.insert_trophy = AsyncMock(return_value={"id": "tr-1", "acquired_at": "2026-10-12T10:00:00Z"})
    fake.sign_media_paths = AsyncMock(side_effect=lambda paths: {p: f"https://signed/{p}" for p in paths})
    fake.get_user_trophies = AsyncMock(return_value=[])
    fake.create_events_bulk = AsyncMock(
        return_value=[{"id": "evt-a", "title": "Aula", "lat": -8.06, "long": -34.87},
                      {"id": "evt-b", "title": "Aula", "lat": -8.06, "long": -34.87}]
    )
    fake.insert_event_media_rows = AsyncMock()
    fake.batch_fetch_event_media = AsyncMock(return_value={})
    fake.set_events_embedding = AsyncMock()
    fake.create_trophy_templates = AsyncMock()
    fake.delete_events = AsyncMock()
    app.dependency_overrides[get_current_user_with_token] = lambda: ("user-1", "jwt")
    app.dependency_overrides[get_db_service] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


# ---------------------------------------------------------------- check-in --


def test_check_in_gives_the_events_trophy_with_a_signed_icon(db: MagicMock) -> None:
    response = TestClient(app).post("/trophies/checkin", json=NEAR)

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Frevo!"
    assert body["icon_url"] == "https://signed/user-1/icon.png"
    db.ensure_default_trophy_template.assert_not_called()


def test_check_in_reads_only_the_columns_it_needs(db: MagicMock) -> None:
    # The full row carries a 1536-dimension embedding the check-in never uses.
    TestClient(app).post("/trophies/checkin", json=NEAR)

    select = db.get_event_by_id.await_args.kwargs["select"]
    assert "embedding" not in select
    assert {"lat", "long", "title", "ends_at", "checkin_opens_at", "opening_hours"} <= set(select.split(","))


def test_an_event_without_a_trophy_gets_the_default_one(db: MagicMock) -> None:
    # Before: 404 "No trophy template" on every event, so check-in never worked.
    db.get_trophy_template_by_event.return_value = None

    response = TestClient(app).post("/trophies/checkin", json=NEAR)

    assert response.status_code == 201
    assert response.json()["name"] == "Presença: Noite de Frevo"
    db.ensure_default_trophy_template.assert_awaited_once_with("evt-1", "Presença: Noite de Frevo")
    db.insert_trophy.assert_awaited_once_with("user-1", "tpl-new", "jwt")


# ---------------------------------------------------- check-in time window --


def test_check_in_before_it_opens_is_refused(db: MagicMock) -> None:
    db.get_event_by_id.return_value = {
        **EVENT,
        "event_date": (NOW + timedelta(days=7)).isoformat(),
        "checkin_opens_at": (NOW + timedelta(days=7)).isoformat(),
        "ends_at": (NOW + timedelta(days=7, hours=3)).isoformat(),
    }

    response = TestClient(app).post("/trophies/checkin", json=NEAR)

    assert response.status_code == 403
    assert "abre" in response.json()["detail"]
    db.insert_trophy.assert_not_called()


def test_check_in_after_the_event_ended_is_refused(db: MagicMock) -> None:
    db.get_event_by_id.return_value = {**EVENT, "ends_at": (NOW - timedelta(minutes=1)).isoformat()}

    response = TestClient(app).post("/trophies/checkin", json=NEAR)

    assert response.status_code == 403
    assert "fechou" in response.json()["detail"]
    db.insert_trophy.assert_not_called()


def test_a_recurring_event_only_gives_todays_trophy(db: MagicMock) -> None:
    # Every occurrence shares the venue: standing there once must not collect them all.
    occurrences = {
        "today": EVENT,
        "next-week": {
            **EVENT,
            "id": "next-week",
            "event_date": (NOW + timedelta(days=6, hours=23)).isoformat(),
            "checkin_opens_at": (NOW + timedelta(days=6, hours=23)).isoformat(),
            "ends_at": (NOW + timedelta(days=7, hours=2)).isoformat(),
        },
    }
    db.get_event_by_id.side_effect = lambda event_id, jwt, **_: occurrences[event_id]
    client = TestClient(app)

    assert client.post("/trophies/checkin", json={**NEAR, "event_id": "today"}).status_code == 201
    assert client.post("/trophies/checkin", json={**NEAR, "event_id": "next-week"}).status_code == 403
    assert db.insert_trophy.await_count == 1


def test_an_event_without_dates_has_no_check_in(db: MagicMock) -> None:
    db.get_event_by_id.return_value = {**EVENT, "event_date": None, "ends_at": None, "checkin_opens_at": None}

    assert TestClient(app).post("/trophies/checkin", json=NEAR).status_code == 403
    db.insert_trophy.assert_not_called()


def test_a_closed_place_gives_no_trophy(db: MagicMock) -> None:
    db.get_event_by_id.return_value = {
        **EVENT,
        "kind": "place",
        "opening_hours": [{"day": d, "hours": "Fechado"} for d in (
            "domingo", "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado"
        )],
    }

    response = TestClient(app).post("/trophies/checkin", json=NEAR)

    assert response.status_code == 403
    assert "fechado" in response.json()["detail"]


# --------------------------------------------------------------- icons --


def test_a_signing_failure_does_not_fail_a_check_in_already_saved(db: MagicMock) -> None:
    # The trophy row is written first: a 500 here would leave the user with a trophy
    # they never saw and a 409 on retry.
    db.sign_media_paths.side_effect = RuntimeError("storage down")

    response = TestClient(app).post("/trophies/checkin", json=NEAR)

    assert response.status_code == 201
    assert response.json()["icon_url"] is None


def test_an_icon_that_could_not_be_signed_is_not_leaked_as_a_path(db: MagicMock) -> None:
    db.sign_media_paths.side_effect = None
    db.sign_media_paths.return_value = {}

    response = TestClient(app).post("/trophies/checkin", json=NEAR)

    assert response.json()["icon_url"] is None


def test_check_in_far_away_is_refused(db: MagicMock) -> None:
    response = TestClient(app).post("/trophies/checkin", json={**NEAR, "lat": -8.2})

    assert response.status_code == 403
    db.insert_trophy.assert_not_called()


def test_check_in_on_an_unknown_event_is_404(db: MagicMock) -> None:
    db.get_event_by_id.return_value = None

    assert TestClient(app).post("/trophies/checkin", json=NEAR).status_code == 404


def test_listed_trophies_have_signed_icons(db: MagicMock) -> None:
    db.get_user_trophies.return_value = [
        {"id": "tr-1", "template_id": "tpl-1", "acquired_at": None,
         "trophy_templates": {"name": "Frevo!", "description": None, "icon_url": "user-1/icon.png"}},
        {"id": "tr-2", "template_id": "tpl-2", "acquired_at": None,
         "trophy_templates": {"name": "Presença", "description": None, "icon_url": None}},
    ]

    trophies = TestClient(app).get("/trophies/").json()

    assert [t["icon_url"] for t in trophies] == ["https://signed/user-1/icon.png", None]


def test_the_trophy_shelf_survives_a_signing_failure(db: MagicMock) -> None:
    db.get_user_trophies.return_value = [
        {"id": "tr-1", "template_id": "tpl-1", "acquired_at": None,
         "trophy_templates": {"name": "Frevo!", "description": None, "icon_url": "user-1/icon.png"}},
    ]
    db.sign_media_paths.side_effect = RuntimeError("storage down")

    response = TestClient(app).get("/trophies/")

    assert response.status_code == 200
    assert response.json()[0]["icon_url"] is None


# ---------------------------------------------------------- event creation --


def _create(body: dict[str, Any]) -> httpx.Response:
    with patch("app.api.events.ai_service.generate_embedding", AsyncMock(return_value=VEC)):
        return TestClient(app).post(
            "/events/", json={"title": "Aula", "lat": -8.06, "long": -34.87, **WHEN, **body}
        )


def test_a_custom_trophy_is_created_for_every_occurrence(db: MagicMock) -> None:
    response = _create(
        {"trophy": {"name": "Aluno dedicado", "description": "Foi à aula", "icon_url": "user-1/trofeu.png"}}
    )

    assert response.status_code == 201
    rows = db.create_trophy_templates.await_args.args[0]
    assert rows == [
        {"event_id": "evt-a", "name": "Aluno dedicado", "description": "Foi à aula", "icon_url": "user-1/trofeu.png"},
        {"event_id": "evt-b", "name": "Aluno dedicado", "description": "Foi à aula", "icon_url": "user-1/trofeu.png"},
    ]


def test_only_an_icon_keeps_the_default_name(db: MagicMock) -> None:
    _create({"trophy": {"icon_url": "user-1/trofeu.webp"}})

    row = db.create_trophy_templates.await_args.args[0][0]
    assert row["name"] == "Presença: Aula"
    assert row["description"] is None
    assert row["icon_url"] == "user-1/trofeu.webp"


@pytest.mark.parametrize("blank", ["   ", ""])
def test_a_blank_name_keeps_the_default_name(db: MagicMock, blank: str) -> None:
    _create({"trophy": {"name": blank, "description": "  ", "icon_url": "user-1/trofeu.png"}})

    row = db.create_trophy_templates.await_args.args[0][0]
    assert row["name"] == "Presença: Aula"
    assert row["description"] is None


def test_if_the_trophy_cannot_be_saved_the_event_is_not_left_behind(db: MagicMock) -> None:
    # Otherwise the creator retries and gets duplicate events, the first without the trophy.
    db.create_trophy_templates.side_effect = RuntimeError("db down")

    response = _create({"trophy": {"name": "Passista"}})

    assert response.status_code == 500
    db.delete_events.assert_awaited_once_with(["evt-a", "evt-b"], "user-1")


def test_without_a_trophy_nothing_is_created_up_front(db: MagicMock) -> None:
    _create({})

    db.create_trophy_templates.assert_not_called()


@pytest.mark.parametrize(
    ("icon", "status"),
    [
        ("user-2/trofeu.png", 403),  # someone else's upload
        ("user-1/../user-2/x.png", 403),
        ("user-1/video.mp4", 422),  # a trophy icon is an image
        ("https://evil.example/x.png", 422),  # only icons uploaded to Flyer
    ],
)
def test_the_icon_must_be_an_image_the_creator_uploaded(db: MagicMock, icon: str, status: int) -> None:
    response = _create({"trophy": {"icon_url": icon}})

    assert response.status_code == status
    db.create_events_bulk.assert_not_called()


def test_trophy_fields_have_limits(db: MagicMock) -> None:
    assert _create({"trophy": {"name": "x" * 81}}).status_code == 422
    assert _create({"trophy": {"description": "x" * 301}}).status_code == 422


# ------------------------------------------------------------- db requests --


async def test_the_default_template_is_created_once_per_event() -> None:
    # Two people checking in at the same time must not create two default trophies:
    # the insert ignores the duplicate (unique event_id) and the id is read back.
    client = MagicMock()
    client.post = AsyncMock(return_value=httpx.Response(201, json=[], request=httpx.Request("POST", "http://x")))
    row = {"id": "tpl-9", "name": "Presença: X", "description": None, "icon_url": None}
    client.get = AsyncMock(return_value=httpx.Response(200, json=[row], request=httpx.Request("GET", "http://x")))

    template = await DBService(client).ensure_default_trophy_template("evt-1", "Presença: X")

    assert template == row
    kwargs = client.post.await_args.kwargs
    assert kwargs["params"] == {"on_conflict": "event_id"}
    assert "resolution=ignore-duplicates" in kwargs["headers"]["Prefer"]
    assert kwargs["json"] == {"event_id": "evt-1", "name": "Presença: X"}
