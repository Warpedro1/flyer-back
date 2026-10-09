"""POST /events/ only attaches media from the creator's own Storage folder."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user_with_token, get_db_service
from app.main import app


def _event_body(media_url: str) -> dict[str, object]:
    return {
        "title": "Concerto",
        "lat": 38.7,
        "long": -9.1,
        "media": [{"media_url": media_url, "type": "image"}],
    }


@pytest.fixture
def db() -> Iterator[MagicMock]:
    fake = MagicMock()
    fake.create_events_bulk = AsyncMock(
        return_value=[{"id": "evt-1", "creator_id": "user-1", "title": "Concerto", "lat": 38.7, "long": -9.1}]
    )
    fake.insert_event_media_rows = AsyncMock()
    fake.set_events_embedding = AsyncMock()
    fake.batch_fetch_event_media = AsyncMock(return_value={})

    app.dependency_overrides[get_current_user_with_token] = lambda: ("user-1", "jwt")
    app.dependency_overrides[get_db_service] = lambda: fake
    # Event creation embeds the text best-effort; keep OpenAI out of the test.
    with patch("app.api.events.ai_service.generate_embedding", AsyncMock(return_value=None)):
        yield fake
    app.dependency_overrides.clear()


def test_someone_elses_path_is_refused_before_anything_is_written(db: MagicMock) -> None:
    client = TestClient(app)

    response = client.post("/events/", json=_event_body("user-2/private.jpg"))

    assert response.status_code == 403
    db.create_events_bulk.assert_not_called()
    db.insert_event_media_rows.assert_not_called()


def test_own_path_is_attached(db: MagicMock) -> None:
    client = TestClient(app)

    response = client.post("/events/", json=_event_body("user-1/photo.jpg"))

    assert response.status_code == 201
    rows = db.insert_event_media_rows.await_args.args[0]
    assert [r["media_url"] for r in rows] == ["user-1/photo.jpg"]
