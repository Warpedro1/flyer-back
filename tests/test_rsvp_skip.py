"""Pular a vez (#5 part 1): the creator passes the turn of someone already called.

`POST /events/{id}/waitlist/{rsvp_id}/skip` calls `rsvp_skip` with the creator from the
token; the function checks the creator and the `called` status atomically.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import get_db_service
from app.api.rsvp import _http_from_rpc
from app.core.limiter import limiter
from app.main import app
from app.services.db_service import DBService, RpcError

AUTH = {"Authorization": "Bearer token"}
PAYLOAD = {"sub": "creator-1", "amr": [{"method": "oauth", "timestamp": 1}]}
SKIPPED = {
    "id": "rsvp-1",
    "event_id": "evt-1",
    "user_id": "user-2",
    "status": "no_show",
    "waitlist_position": None,
    "called_at": None,
    "call_expires_at": None,
    "admitted_at": None,
    "no_show_count": 1,
    "created_at": "2026-10-18T00:00:00+00:00",
}


@pytest.fixture
def db() -> Iterator[MagicMock]:
    limiter.reset()
    deps.reset_verified_sessions_cache()
    fake = MagicMock()
    fake.get_account_state = AsyncMock(return_value="ok")
    fake.rsvp_skip = AsyncMock(return_value=SKIPPED)
    app.dependency_overrides[get_db_service] = lambda: fake
    with patch("app.api.deps.DBService", return_value=fake), patch(
        "app.api.deps.decode_access_token", return_value=PAYLOAD
    ):
        yield fake
    app.dependency_overrides.clear()


def _skip() -> httpx.Response:
    return TestClient(app).post("/events/evt-1/waitlist/rsvp-1/skip", headers=AUTH)


def test_the_creator_skips_someone_called(db: MagicMock) -> None:
    response = _skip()

    assert response.status_code == 200
    assert response.json()["status"] == "no_show"
    db.rsvp_skip.assert_awaited_once_with("evt-1", "rsvp-1", "creator-1")


@pytest.mark.parametrize(
    ("message", "status", "detail"),
    [
        ("not_event_creator", 403, "Só o criador do evento pode fazer isto."),
        ("rsvp_not_skippable", 409, "Só dá para pular a vez de quem já foi chamado."),
        ("event_not_found", 404, "Evento não encontrado."),
    ],
)
def test_refusals_are_explained(db: MagicMock, message: str, status: int, detail: str) -> None:
    db.rsvp_skip.side_effect = RpcError(message)

    response = _skip()

    assert response.status_code == status
    assert response.json()["detail"] == detail


def test_skip_maps_like_the_other_rpc_errors() -> None:
    assert _http_from_rpc(RpcError("rsvp_not_skippable")).status_code == 409


def test_skip_needs_a_session(db: MagicMock) -> None:
    assert TestClient(app).post("/events/evt-1/waitlist/rsvp-1/skip").status_code == 401
    db.rsvp_skip.assert_not_called()


async def test_skip_calls_the_rpc_with_the_creator() -> None:
    client = MagicMock()
    client.post = AsyncMock(
        return_value=httpx.Response(200, json=SKIPPED, request=httpx.Request("POST", "http://x"))
    )

    await DBService(client).rsvp_skip("evt-1", "rsvp-1", "creator-1")

    args, kwargs = client.post.await_args
    assert args[0].endswith("/rpc/rsvp_skip")
    assert kwargs["json"] == {"p_event_id": "evt-1", "p_rsvp_id": "rsvp-1", "p_creator_id": "creator-1"}
