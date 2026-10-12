"""Places in the API: discovery filters, place fields, event→place links (Lote 5B)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user_with_token, get_db_service
from app.main import app
from app.models.schemas import EMBEDDING_DIMENSIONS, EVENT_CATEGORIES
from app.services.db_service import DBService

VEC = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
BODY = {"latitude": -8.06, "longitude": -34.87}
TOMORROW = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

PLACE_ROW: dict[str, Any] = {
    "id": "place-1",
    "kind": "place",
    "title": "Parraxaxá",
    "category": "comida_bebida",
    "location_name": "Av. Fernando Simões Barbosa, 1200 - Boa Viagem",
    "lat": -8.11,
    "long": -34.89,
    "event_date": None,
    "price": None,
    "price_level": 2,
    "opening_hours": [{"day": "segunda-feira", "hours": "11:30–22:00"}],
    "source": "google_maps",
    "source_url": "https://maps.google.com/?cid=1",
    "cover_url": "https://lh3.googleusercontent.com/p/1",
}


def _event_row(event_id: str, **extra: Any) -> dict[str, Any]:
    return {"id": event_id, "title": "Show", "lat": -8.1, "long": -34.9, "event_date": TOMORROW, **extra}


@pytest.fixture
def db() -> Iterator[MagicMock]:
    fake = MagicMock()
    fake.get_profile = AsyncMock(return_value={"id": "user-1", "interest_embedding": VEC})
    fake.get_social_avg_embedding = AsyncMock(return_value=None)
    fake.call_match_events = AsyncMock(return_value=[{"id": "place-1"}])
    fake.batch_fetch_events_by_ids = AsyncMock(return_value={"place-1": PLACE_ROW})
    fake.batch_fetch_event_media = AsyncMock(return_value={})
    fake.get_event_by_id = AsyncMock(return_value=PLACE_ROW)
    fake.create_events_bulk = AsyncMock(return_value=[_event_row("evt-9")])
    fake.insert_event_media_rows = AsyncMock()
    fake.list_place_events = AsyncMock(return_value=[_event_row("evt-2", place_id="place-1")])
    fake.rsvp_join = AsyncMock()
    fake.set_events_embedding = AsyncMock()
    app.dependency_overrides[get_current_user_with_token] = lambda: ("user-1", "jwt")
    app.dependency_overrides[get_db_service] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


# --------------------------------------------------------------- discover --


def test_discover_sends_the_filters_to_the_database(db: MagicMock) -> None:
    body = {
        **BODY,
        "radius_km": 5,
        "kinds": ["place"],
        "categories": ["comida_bebida", "praia_natureza"],
        "date_from": "2026-10-12T00:00:00Z",
        "date_to": "2026-10-13T00:00:00Z",
        "max_price": "50",
    }

    response = TestClient(app).post("/events/discover", json=body)

    assert response.status_code == 200
    kwargs = db.call_match_events.await_args.kwargs
    assert kwargs["kinds"] == ["place"]
    assert kwargs["categories"] == ["comida_bebida", "praia_natureza"]
    assert kwargs["date_from"] == datetime(2026, 10, 12, tzinfo=timezone.utc)
    assert kwargs["date_to"] == datetime(2026, 10, 13, tzinfo=timezone.utc)
    assert str(kwargs["max_price"]) == "50"
    assert db.call_match_events.await_args.args[3] == 5


def test_discover_without_filters_sends_none(db: MagicMock) -> None:
    TestClient(app).post("/events/discover", json=BODY)

    kwargs = db.call_match_events.await_args.kwargs
    assert kwargs == {
        "kinds": None,
        "categories": None,
        "date_from": None,
        "date_to": None,
        "max_price": None,
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"categories": ["festa_na_lua"]},
        {"kinds": ["museum"]},
        {"max_price": "-1"},
        {"date_from": "2026-10-13T00:00:00Z", "date_to": "2026-10-12T00:00:00Z"},
    ],
)
def test_discover_rejects_invalid_filters(db: MagicMock, extra: dict[str, Any]) -> None:
    response = TestClient(app).post("/events/discover", json={**BODY, **extra})

    assert response.status_code == 422
    db.call_match_events.assert_not_called()


def test_a_place_comes_back_with_its_own_fields(db: MagicMock) -> None:
    response = TestClient(app).post("/events/discover", json=BODY)

    place = response.json()[0]
    assert place["kind"] == "place"
    assert place["price_level"] == 2
    assert place["opening_hours"] == [{"day": "segunda-feira", "hours": "11:30–22:00"}]
    assert place["source_url"] == "https://maps.google.com/?cid=1"
    assert place["cover_url"] == "https://lh3.googleusercontent.com/p/1"
    assert place["place_id"] is None


def test_rows_from_before_the_migration_are_events(db: MagicMock) -> None:
    db.batch_fetch_events_by_ids.return_value = {"place-1": _event_row("place-1")}

    event = TestClient(app).post("/events/discover", json=BODY).json()[0]

    assert event["kind"] == "event"
    assert event["opening_hours"] is None


def test_the_taxonomy_has_the_agreed_slugs() -> None:
    # Slugs are stored in the database and mirrored by the app: renaming one is a
    # data migration, not a refactor.
    assert "outros" in EVENT_CATEGORIES
    assert {"comida_bebida", "praia_natureza", "academia_exercicio", "feira_mercado"} <= set(EVENT_CATEGORIES)
    assert len(EVENT_CATEGORIES) == len(set(EVENT_CATEGORIES)) == 22


# --------------------------------------------------------- create + place --


def _create(body: dict[str, Any]) -> httpx.Response:
    with patch("app.api.events.ai_service.generate_embedding", AsyncMock(return_value=VEC)):
        when = {"event_date": "2026-10-16T20:00:00", "ends_at": "2026-10-16T23:00:00"}
        return TestClient(app).post("/events/", json={**when, **body})


def test_an_event_can_be_created_at_a_place(db: MagicMock) -> None:
    response = _create({"title": "Noite de forró", "lat": -8.11, "long": -34.89, "place_id": "place-1"})

    assert response.status_code == 201
    row = db.create_events_bulk.await_args.args[1][0]
    assert row["place_id"] == "place-1"
    db.get_event_by_id.assert_awaited_with("place-1", "jwt")


@pytest.mark.parametrize("found", [None, _event_row("evt-5")])
def test_place_id_must_point_at_a_place(db: MagicMock, found: dict[str, Any] | None) -> None:
    db.get_event_by_id.return_value = found

    response = _create({"title": "Noite de forró", "lat": -8.11, "long": -34.89, "place_id": "evt-5"})

    assert response.status_code == 422
    db.create_events_bulk.assert_not_called()


def test_the_category_must_be_in_the_taxonomy(db: MagicMock) -> None:
    response = _create({"title": "Noite de forró", "lat": -8.11, "long": -34.89, "category": "teste"})

    assert response.status_code == 422
    db.create_events_bulk.assert_not_called()


# ---------------------------------------------------------- place events --


def test_a_place_lists_its_upcoming_events(db: MagicMock) -> None:
    response = TestClient(app).get("/events/place-1/events")

    assert response.status_code == 200
    assert [e["id"] for e in response.json()] == ["evt-2"]
    db.list_place_events.assert_awaited_once_with("place-1", "jwt")


@pytest.mark.parametrize("found", [None, _event_row("evt-5")])
def test_only_places_have_an_events_list(db: MagicMock, found: dict[str, Any] | None) -> None:
    db.get_event_by_id.return_value = found

    response = TestClient(app).get("/events/evt-5/events")

    assert response.status_code == 404
    db.list_place_events.assert_not_called()


def test_nobody_rsvps_to_a_place(db: MagicMock) -> None:
    # A place has no date nor capacity: "Vou" only makes sense for events.
    response = TestClient(app).post("/events/place-1/rsvp")

    assert response.status_code == 422
    db.rsvp_join.assert_not_called()


# ---------------------------------------------------------- db requests --


def _client(method: str, payload: object) -> MagicMock:
    client = MagicMock()
    response = httpx.Response(200, json=payload, request=httpx.Request(method.upper(), "http://x"))
    setattr(client, method, AsyncMock(return_value=response))
    return client


async def test_match_events_sends_only_the_filters_that_are_set() -> None:
    client = _client("post", [])

    await DBService(client).call_match_events(
        None, -8.06, -34.87, 5.0, kinds=["place"], categories=None, date_from=None,
        date_to=datetime(2026, 10, 13, tzinfo=timezone.utc), max_price=None,
    )

    sent = client.post.await_args.kwargs["json"]
    assert sent == {
        "query_embedding": None,
        "user_lat": -8.06,
        "user_lng": -34.87,
        "radius_km": 5.0,
        "kinds": ["place"],
        "date_to": "2026-10-13T00:00:00+00:00",
    }


async def test_place_events_are_upcoming_and_soonest_first() -> None:
    client = _client("get", [])

    await DBService(client).list_place_events("place-1", "jwt")

    params = client.get.await_args.kwargs["params"]
    assert params["place_id"] == "eq.place-1"
    assert params["event_date"].startswith("gte.")
    assert params["order"] == "event_date.asc"
