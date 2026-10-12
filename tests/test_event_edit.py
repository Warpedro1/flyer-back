"""Editing an event and opening/closing its check-in (decisions 2026-10-12).

Only the creator edits. Everything can change except the recurrence: a recurring
event is edited one date at a time. Imported events and places are not editable.
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

NOW = datetime.now(timezone.utc)
ROW: dict[str, Any] = {
    "id": "evt-1",
    "kind": "event",
    "source": None,
    "creator_id": "user-1",
    "title": "Aula de frevo",
    "description": "Para iniciantes",
    "category": "musica_show",
    "place_id": None,
    "location_name": "Paço do Frevo",
    "lat": -8.06,
    "long": -34.87,
    "event_date": "2026-10-16T23:00:00+00:00",
    "ends_at": "2026-10-17T02:00:00+00:00",
    "checkin_opens_at": "2026-10-16T23:00:00+00:00",
    "checkin_closed_at": None,
    "price": "20.00",
    "capacity": 30,
    "waitlist_enabled": True,
    "call_ttl_minutes": 10,
    "auto_call_next": True,
}
UPDATE: dict[str, Any] = {
    "title": "Aula de frevo avançada",
    "description": None,
    "category": "musica_show",
    "location_name": "Paço do Frevo",
    "lat": -8.06,
    "long": -34.87,
    "event_date": "2026-10-16T21:00:00",
    "ends_at": "2026-10-16T23:30:00",
    "price": "25.00",
    "capacity": None,
    "media": [{"media_url": "user-1/nova.jpg", "type": "image", "order_index": 0}],
    "trophy": {"name": "Passista", "description": None, "icon_url": "user-1/trofeu.png"},
}


@pytest.fixture
def db() -> Iterator[MagicMock]:
    limiter.reset()
    fake = MagicMock()
    fake.get_event_by_id = AsyncMock(return_value=dict(ROW))

    async def update(event_id: str, creator_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {**ROW, **payload}

    fake.update_event = AsyncMock(side_effect=update)
    fake.replace_event_media = AsyncMock()
    fake.upsert_trophy_template = AsyncMock()
    fake.get_trophy_template_by_event = AsyncMock(
        return_value={"id": "tpl-1", "name": "Passista", "description": "Dançaste", "icon_url": "user-1/trofeu.png"}
    )
    fake.get_event_media_rows = AsyncMock(
        return_value=[{"media_url": "user-1/foto.jpg", "type": "image", "order_index": 0}]
    )
    fake.sign_media_paths = AsyncMock(side_effect=lambda paths: {p: f"https://signed/{p}" for p in paths})
    fake.batch_fetch_event_media = AsyncMock(return_value={})
    fake.set_events_embedding = AsyncMock()
    app.dependency_overrides[get_current_user_with_token] = lambda: ("user-1", "jwt")
    app.dependency_overrides[get_db_service] = lambda: fake
    with patch("app.api.events.ai_service.generate_embedding", AsyncMock(return_value=None)):
        yield fake
    app.dependency_overrides.clear()


def _as(user_id: str) -> None:
    app.dependency_overrides[get_current_user_with_token] = lambda: (user_id, "jwt")


def _put(body: dict[str, Any] | None = None) -> httpx.Response:
    return TestClient(app).put("/events/evt-1", json=body or UPDATE)


# ------------------------------------------------------------------- edit --


def test_the_creator_edits_the_event(db: MagicMock) -> None:
    response = _put()

    assert response.status_code == 200
    event_id, creator_id, payload = db.update_event.await_args.args
    assert (event_id, creator_id) == ("evt-1", "user-1")
    assert payload["title"] == "Aula de frevo avançada"
    assert payload["event_date"] == "2026-10-16T21:00:00-03:00"
    assert payload["ends_at"] == "2026-10-16T23:30:00-03:00"
    assert payload["checkin_opens_at"] == "2026-10-16T21:00:00-03:00"
    assert payload["price"] == "25.00"
    assert payload["capacity"] is None
    assert response.json()["title"] == "Aula de frevo avançada"


def test_editing_replaces_the_media(db: MagicMock) -> None:
    _put()

    db.replace_event_media.assert_awaited_once_with(
        "evt-1", [{"event_id": "evt-1", "media_url": "user-1/nova.jpg", "type": "image", "order_index": 0}]
    )


def test_editing_saves_the_trophy(db: MagicMock) -> None:
    _put()

    db.upsert_trophy_template.assert_awaited_once_with(
        {"event_id": "evt-1", "name": "Passista", "description": None, "icon_url": "user-1/trofeu.png"}
    )


def test_without_a_custom_trophy_the_default_follows_the_new_title(db: MagicMock) -> None:
    _put({**UPDATE, "trophy": None})

    db.upsert_trophy_template.assert_awaited_once_with(
        {"event_id": "evt-1", "name": "Presença: Aula de frevo avançada", "description": None, "icon_url": None}
    )


def test_someone_else_cannot_edit(db: MagicMock) -> None:
    _as("user-2")

    assert _put().status_code == 403
    db.update_event.assert_not_called()


@pytest.mark.parametrize("row", [{"source": "sympla", "creator_id": None}, {"kind": "place"}])
def test_imported_events_and_places_are_not_editable(db: MagicMock, row: dict[str, Any]) -> None:
    db.get_event_by_id.return_value = {**ROW, **row, "creator_id": "user-1"}

    assert _put().status_code == 422
    db.update_event.assert_not_called()


def test_the_end_must_be_after_the_start(db: MagicMock) -> None:
    assert _put({**UPDATE, "ends_at": "2026-10-16T20:00:00"}).status_code == 422
    db.update_event.assert_not_called()


def test_the_recurrence_cannot_be_edited(db: MagicMock) -> None:
    body = {**UPDATE, "recurrence": {"mode": "count", "every": "week", "occurrences": 3}}

    assert _put(body).status_code == 422


@pytest.mark.parametrize(
    "change",
    [
        {"media": [{"media_url": "user-2/foto.jpg", "type": "image"}]},
        {"trophy": {"icon_url": "user-2/trofeu.png"}},
    ],
)
def test_only_your_own_uploads_are_attached(db: MagicMock, change: dict[str, Any]) -> None:
    assert _put({**UPDATE, **change}).status_code == 403
    db.update_event.assert_not_called()


def test_edit_form_gets_paths_and_previews(db: MagicMock) -> None:
    response = TestClient(app).get("/events/evt-1/edit")

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Aula de frevo"
    assert body["media"] == [
        {"media_url": "user-1/foto.jpg", "type": "image", "order_index": 0,
         "preview_url": "https://signed/user-1/foto.jpg"}
    ]
    assert body["trophy"] == {
        "name": "Passista",
        "description": "Dançaste",
        "icon_url": "user-1/trofeu.png",
        "icon_preview_url": "https://signed/user-1/trofeu.png",
    }
    assert body["ends_at"].startswith("2026-10-17T02:00:00")


def test_only_the_creator_gets_the_edit_form(db: MagicMock) -> None:
    _as("user-2")

    assert TestClient(app).get("/events/evt-1/edit").status_code == 403


# ------------------------------------------------------- open/close check-in --


def test_the_creator_opens_check_in_now(db: MagicMock) -> None:
    db.get_event_by_id.return_value = {
        **ROW,
        "event_date": (NOW + timedelta(hours=1)).isoformat(),
        "checkin_opens_at": (NOW + timedelta(hours=1)).isoformat(),
        "ends_at": (NOW + timedelta(hours=4)).isoformat(),
        "checkin_closed_at": (NOW - timedelta(minutes=5)).isoformat(),
    }

    response = TestClient(app).post("/events/evt-1/checkin/open")

    assert response.status_code == 200
    payload = db.update_event.await_args.args[2]
    assert payload["checkin_closed_at"] is None
    opened = datetime.fromisoformat(payload["checkin_opens_at"])
    assert abs((opened - NOW).total_seconds()) < 60
    assert response.json()["checkin_status"] == "open"


def test_check_in_cannot_open_after_the_event_ended(db: MagicMock) -> None:
    db.get_event_by_id.return_value = {**ROW, "ends_at": (NOW - timedelta(minutes=1)).isoformat()}

    response = TestClient(app).post("/events/evt-1/checkin/open")

    assert response.status_code == 422
    db.update_event.assert_not_called()


def test_the_creator_closes_check_in(db: MagicMock) -> None:
    db.get_event_by_id.return_value = {
        **ROW,
        "event_date": (NOW - timedelta(hours=1)).isoformat(),
        "checkin_opens_at": (NOW - timedelta(hours=1)).isoformat(),
        "ends_at": (NOW + timedelta(hours=2)).isoformat(),
    }

    response = TestClient(app).post("/events/evt-1/checkin/close")

    assert response.status_code == 200
    closed = datetime.fromisoformat(db.update_event.await_args.args[2]["checkin_closed_at"])
    assert abs((closed - NOW).total_seconds()) < 60
    assert response.json()["checkin_status"] == "closed"


@pytest.mark.parametrize("action", ["open", "close"])
def test_only_the_creator_opens_or_closes(db: MagicMock, action: str) -> None:
    _as("user-2")

    assert TestClient(app).post(f"/events/evt-1/checkin/{action}").status_code == 403
    db.update_event.assert_not_called()


# ----------------------------------------------------------- db requests --


async def test_update_event_is_scoped_to_the_creator() -> None:
    from app.services.db_service import DBService

    client = MagicMock()
    client.patch = AsyncMock(
        return_value=httpx.Response(200, json=[ROW], request=httpx.Request("PATCH", "http://x"))
    )

    row = await DBService(client).update_event("evt-1", "user-1", {"title": "X"})

    assert row == ROW
    params = client.patch.await_args.kwargs["params"]
    assert params == {"id": "eq.evt-1", "creator_id": "eq.user-1"}


async def test_delete_events_is_scoped_to_the_creator() -> None:
    from app.services.db_service import DBService

    client = MagicMock()
    client.delete = AsyncMock(
        return_value=httpx.Response(204, request=httpx.Request("DELETE", "http://x"))
    )

    await DBService(client).delete_events(["evt-1", "evt-2"], "user-1")

    params = client.delete.await_args.kwargs["params"]
    assert params == {"id": "in.(evt-1,evt-2)", "creator_id": "eq.user-1"}


async def test_the_trophy_is_upserted_by_event() -> None:
    from app.services.db_service import DBService

    client = MagicMock()
    client.post = AsyncMock(return_value=httpx.Response(201, request=httpx.Request("POST", "http://x")))

    await DBService(client).upsert_trophy_template({"event_id": "evt-1", "name": "X"})

    kwargs = client.post.await_args.kwargs
    assert kwargs["params"] == {"on_conflict": "event_id"}
    assert "resolution=merge-duplicates" in kwargs["headers"]["Prefer"]
