"""Introduction tour on the first login (Warpedro1/flyer#23, decisions 2026-10-17).

* ``GET /auth/profile/status`` says whether the tour was seen and whether the chat
  onboarding was done (where the tour sends you at the end).
* ``POST /auth/profile/intro-seen`` records it once, only for the token's owner.
"""

from __future__ import annotations

from collections.abc import Iterator
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


@pytest.fixture
def db() -> Iterator[MagicMock]:
    limiter.reset()
    deps.reset_verified_sessions_cache()
    fake = MagicMock()
    fake.get_account_state = AsyncMock(return_value="ok")
    fake.get_profile_identity = AsyncMock(
        return_value={"cpf": "52998224725", "phone": None, "intro_seen_at": None, "onboarding_preferences_hash": None}
    )
    fake.mark_intro_seen = AsyncMock()
    app.dependency_overrides[get_db_service] = lambda: fake
    with patch("app.api.deps.DBService", return_value=fake), patch(
        "app.api.deps.decode_access_token", return_value=PAYLOAD
    ):
        yield fake
    app.dependency_overrides.clear()


def test_a_new_account_has_not_seen_the_tour(db: MagicMock) -> None:
    body = TestClient(app).get("/auth/profile/status", headers=AUTH).json()

    assert body["intro_seen"] is False
    assert body["onboarded"] is False


def test_seen_and_onboarded(db: MagicMock) -> None:
    db.get_profile_identity.return_value = {
        "cpf": "52998224725",
        "phone": None,
        "intro_seen_at": "2026-10-17T12:00:00+00:00",
        "onboarding_preferences_hash": "abc",
    }

    body = TestClient(app).get("/auth/profile/status", headers=AUTH).json()

    assert body["intro_seen"] is True
    assert body["onboarded"] is True


def test_marking_the_tour_seen(db: MagicMock) -> None:
    response = TestClient(app).post("/auth/profile/intro-seen", headers=AUTH)

    assert response.status_code == 204
    db.mark_intro_seen.assert_awaited_once_with("user-1")


def test_marking_needs_a_session() -> None:
    assert TestClient(app).post("/auth/profile/intro-seen").status_code == 401


async def test_marking_only_fills_an_empty_date() -> None:
    client = MagicMock()
    client.patch = AsyncMock(return_value=httpx.Response(204, request=httpx.Request("PATCH", "http://x")))

    await DBService(client).mark_intro_seen("user-1")

    kwargs = client.patch.await_args.kwargs
    assert kwargs["params"] == {"id": "eq.user-1", "intro_seen_at": "is.null"}
    assert set(kwargs["json"]) == {"intro_seen_at"}


async def test_status_reads_the_tour_and_onboarding_columns() -> None:
    client = MagicMock()
    client.get = AsyncMock(return_value=httpx.Response(200, json=[], request=httpx.Request("GET", "http://x")))

    await DBService(client).get_profile_identity("user-1")

    select = client.get.await_args.kwargs["params"]["select"]
    assert "intro_seen_at" in select and "onboarding_preferences_hash" in select
