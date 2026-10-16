"""Supabase refusals on the login code, said plainly instead of a 500 (#30).

Seen in production: ``/auth/v1/otp`` answers 429 when a code was sent less than a
minute ago, and 422 when the session belongs to an account that no longer exists.
Both reached the user as "problema do nosso lado".
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import get_db_service
from app.core.limiter import limiter
from app.main import app

AUTH = {"Authorization": "Bearer token"}
PAYLOAD: dict[str, Any] = {
    "sub": "user-1",
    "email": "ana@example.com",
    "session_id": "sess-1",
    "amr": [{"method": "password", "timestamp": 1760000000}],
}


def _supabase_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://x/auth/v1/otp")
    response = httpx.Response(status, json={"msg": "nope"}, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


@pytest.fixture
def db() -> Iterator[MagicMock]:
    limiter.reset()
    deps.reset_verified_sessions_cache()
    fake = MagicMock()
    fake.send_email_code = AsyncMock()
    app.dependency_overrides[get_db_service] = lambda: fake
    with patch("app.api.deps.decode_access_token", return_value=PAYLOAD):
        yield fake
    app.dependency_overrides.clear()


def test_a_code_sent_less_than_a_minute_ago_is_a_429(db: MagicMock) -> None:
    db.send_email_code.side_effect = _supabase_error(429)

    response = TestClient(app).post("/auth/email-code/send", headers=AUTH)

    assert response.status_code == 429
    body = response.json()
    assert body["code"] == "email_code_recently_sent"
    assert "menos de 1 minuto" in body["detail"]


@pytest.mark.parametrize("status", [400, 404, 422])
def test_an_account_that_no_longer_exists_is_a_401(db: MagicMock, status: int) -> None:
    db.send_email_code.side_effect = _supabase_error(status)

    response = TestClient(app).post("/auth/email-code/send", headers=AUTH)

    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "session_invalid"
    assert body["detail"] == "Sessão inválida: entra outra vez."


def test_other_supabase_failures_stay_a_500_with_a_reference(db: MagicMock) -> None:
    db.send_email_code.side_effect = _supabase_error(500)

    response = TestClient(app).post("/auth/email-code/send", headers=AUTH)

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
