"""POST /events/discover end to end with a fake DB (#4)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user_with_token, get_db_service
from app.main import app
from app.models.schemas import EMBEDDING_DIMENSIONS
from app.services.db_service import DBService

VEC = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
BODY = {"latitude": 38.72, "longitude": -9.14}
TOMORROW = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()


def _event_row(event_id: str) -> dict[str, object]:
    return {"id": event_id, "title": "Concerto", "lat": 38.7, "long": -9.1, "event_date": TOMORROW}


@pytest.fixture
def db() -> Iterator[MagicMock]:
    fake = MagicMock()
    fake.get_profile = AsyncMock(return_value={"id": "user-1", "interest_embedding": VEC})
    fake.get_social_avg_embedding = AsyncMock(return_value=None)
    fake.call_match_events = AsyncMock(return_value=[{"id": "evt-1"}])
    fake.batch_fetch_events_by_ids = AsyncMock(return_value={"evt-1": _event_row("evt-1")})
    fake.batch_fetch_event_media = AsyncMock(return_value={})
    app.dependency_overrides[get_current_user_with_token] = lambda: ("user-1", "jwt")
    app.dependency_overrides[get_db_service] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


def test_with_an_interest_vector_it_is_personalised(db: MagicMock) -> None:
    response = TestClient(app).post("/events/discover", json=BODY)

    assert response.status_code == 200
    assert [e["id"] for e in response.json()] == ["evt-1"]
    assert response.headers["X-Discovery-Mode"] == "personalized"
    assert db.call_match_events.await_args.args[0] == VEC


def test_without_a_vector_it_falls_back_to_nearby_events(db: MagicMock) -> None:
    # Decision for #4: no more 400 "Chat first" — show what is close, by date,
    # and tell the app so it can suggest the onboarding.
    db.get_profile.return_value = {"id": "user-1", "interest_embedding": None}

    with patch("app.api.events.ensure_interest_embedding", AsyncMock(return_value=None)):
        response = TestClient(app).post("/events/discover", json=BODY)

    assert response.status_code == 200
    assert [e["id"] for e in response.json()] == ["evt-1"]
    assert response.headers["X-Discovery-Mode"] == "nearby"
    assert db.call_match_events.await_args.args[0] is None


def test_the_mode_header_is_readable_by_the_browser(db: MagicMock) -> None:
    # Without expose_headers a cross-origin fetch cannot see custom headers.
    response = TestClient(app).post(
        "/events/discover", json=BODY, headers={"Origin": "https://flyer.example"}
    )

    exposed = response.headers.get("Access-Control-Expose-Headers", "")
    assert "x-discovery-mode" in exposed.lower()


def test_a_failed_event_embedding_is_logged_as_an_error(
    db: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    # It used to be a warning, swallowed: every created event silently missed
    # its vector and nobody noticed.
    db.create_events_bulk = AsyncMock(return_value=[_event_row("evt-9")])
    db.insert_event_media_rows = AsyncMock()
    failing = AsyncMock(side_effect=RuntimeError("openai down"))

    with patch("app.api.events.ai_service.generate_embedding", failing):
        with caplog.at_level(logging.ERROR, logger="app.api.events"):
            response = TestClient(app).post(
                "/events/", json={"title": "Concerto", "lat": 38.7, "long": -9.1}
            )

    assert response.status_code == 201
    assert any(r.levelno >= logging.ERROR and "evt-9" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------ db requests --


def _client(method: str, payload: object) -> MagicMock:
    client = MagicMock()
    response = httpx.Response(200, json=payload, request=httpx.Request(method.upper(), "http://x"))
    setattr(client, method, AsyncMock(return_value=response))
    return client


async def test_match_events_sends_a_null_vector_for_the_fallback() -> None:
    client = _client("post", [])

    await DBService(client).call_match_events(None, 38.7, -9.1, 10.0)

    sent = client.post.await_args.kwargs["json"]
    assert sent == {"query_embedding": None, "user_lat": 38.7, "user_lng": -9.1, "radius_km": 10.0}


async def test_event_vectors_go_to_the_existing_column() -> None:
    # The table has `event_embedding` (what the ETL writes); `embedding` never existed.
    client = _client("patch", [])

    await DBService(client).set_events_embedding(["evt-1"], VEC)

    assert client.patch.await_args.kwargs["json"] == {"event_embedding": VEC}
