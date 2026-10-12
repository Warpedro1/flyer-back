"""Trophies: default trophy on check-in, custom trophy on event creation, signed icons.

Decisions (2026-10-12):
  * every event gives a trophy at check-in; without a custom one, a default
    "Presença: <título>" is created on the first check-in;
  * a recurring event gets one trophy per occurrence (each date is its own event);
  * the creator may customise name, description and icon (an image they uploaded).
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
from app.services.db_service import DBService

EVENT = {"id": "evt-1", "title": "Noite de Frevo", "lat": -8.06, "long": -34.87}
NEAR = {"event_id": "evt-1", "lat": -8.06, "long": -34.87}
VEC = [0.1] * 1536


@pytest.fixture
def db() -> Iterator[MagicMock]:
    limiter.reset()
    fake = MagicMock()
    fake.get_event_by_id = AsyncMock(return_value=EVENT)
    fake.get_trophy_template_by_event = AsyncMock(return_value="tpl-1")
    fake.ensure_default_trophy_template = AsyncMock(return_value="tpl-new")
    fake.insert_trophy = AsyncMock(return_value={"id": "tr-1", "acquired_at": "2026-10-12T10:00:00Z"})
    fake.get_trophy_template_full = AsyncMock(
        return_value={"id": "tpl-1", "name": "Frevo!", "description": "Dançaste", "icon_url": "user-1/icon.png"}
    )
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


def test_an_event_without_a_trophy_gets_the_default_one(db: MagicMock) -> None:
    # Before: 404 "No trophy template" on every event, so check-in never worked.
    db.get_trophy_template_by_event.return_value = None
    db.get_trophy_template_full.return_value = {"id": "tpl-new", "name": "Presença: Noite de Frevo"}

    response = TestClient(app).post("/trophies/checkin", json=NEAR)

    assert response.status_code == 201
    assert response.json()["name"] == "Presença: Noite de Frevo"
    db.ensure_default_trophy_template.assert_awaited_once_with("evt-1", "Presença: Noite de Frevo")
    db.insert_trophy.assert_awaited_once_with("user-1", "tpl-new", "jwt")


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


# ---------------------------------------------------------- event creation --


def _create(body: dict[str, Any]) -> httpx.Response:
    with patch("app.api.events.ai_service.generate_embedding", AsyncMock(return_value=VEC)):
        return TestClient(app).post("/events/", json={"title": "Aula", "lat": -8.06, "long": -34.87, **body})


def test_a_custom_trophy_is_created_for_every_occurrence(db: MagicMock) -> None:
    response = _create(
        {"trophy": {"name": "Aluno dedicado", "description": "Foste à aula", "icon_url": "user-1/trofeu.png"}}
    )

    assert response.status_code == 201
    rows = db.create_trophy_templates.await_args.args[0]
    assert rows == [
        {"event_id": "evt-a", "name": "Aluno dedicado", "description": "Foste à aula", "icon_url": "user-1/trofeu.png"},
        {"event_id": "evt-b", "name": "Aluno dedicado", "description": "Foste à aula", "icon_url": "user-1/trofeu.png"},
    ]


def test_only_an_icon_keeps_the_default_name(db: MagicMock) -> None:
    _create({"trophy": {"icon_url": "user-1/trofeu.webp"}})

    row = db.create_trophy_templates.await_args.args[0][0]
    assert row["name"] == "Presença: Aula"
    assert row["description"] is None
    assert row["icon_url"] == "user-1/trofeu.webp"


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
    client.get = AsyncMock(return_value=httpx.Response(200, json=[{"id": "tpl-9"}], request=httpx.Request("GET", "http://x")))

    template_id = await DBService(client).ensure_default_trophy_template("evt-1", "Presença: X")

    assert template_id == "tpl-9"
    kwargs = client.post.await_args.kwargs
    assert kwargs["params"] == {"on_conflict": "event_id"}
    assert "resolution=ignore-duplicates" in kwargs["headers"]["Prefer"]
    assert kwargs["json"] == {"event_id": "evt-1", "name": "Presença: X"}
