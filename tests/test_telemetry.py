"""Errors from the app's error screen reach `app_errors` (#33, #25 phase 1).

Decision 2026-10-16: only with a session (a normal protected route). Errors before
login stay in the browser console.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import get_db_service
from app.core.limiter import limiter
from app.main import app

AUTH = {"Authorization": "Bearer token"}
PAYLOAD = {"sub": "user-9", "email": "ana@example.com", "amr": [{"method": "oauth", "timestamp": 1}]}
BODY = {
    "message": "Cannot read properties of undefined (reading 'title')",
    "route": "/events/123",
    "code": "render_error",
    "request_id": "abc123def456",
    "stack": "TypeError: at EventDetailsPage",
}


@pytest.fixture
def db() -> Iterator[MagicMock]:
    limiter.reset()
    deps.reset_verified_sessions_cache()
    fake = MagicMock()
    fake.get_account_state = AsyncMock(return_value="ok")
    app.dependency_overrides[get_db_service] = lambda: fake
    with patch("app.api.deps.DBService", return_value=fake), patch(
        "app.api.deps.decode_access_token", return_value=PAYLOAD
    ):
        yield fake
    app.dependency_overrides.clear()


def test_a_client_error_is_recorded(db: MagicMock, recorded: list[dict[str, Any]]) -> None:
    response = TestClient(app).post("/telemetry/client-error", headers=AUTH, json=BODY)

    assert response.status_code == 204
    row = recorded[0]
    assert row["source"] == "client"
    assert row["code"] == "render_error"
    assert row["route"] == "/events/123"
    assert row["message"] == BODY["message"]
    assert row["details"] == {"api_request_id": "abc123def456", "stack": BODY["stack"]}


def test_personal_data_is_scrubbed(db: MagicMock, recorded: list[dict[str, Any]]) -> None:
    body = {**BODY, "message": "falhou para bia@example.com", "stack": "cpf 52998224725"}

    TestClient(app).post("/telemetry/client-error", headers=AUTH, json=body)

    assert "bia@example.com" not in str(recorded[0])
    assert "52998224725" not in str(recorded[0])


def test_it_needs_a_session() -> None:
    response = TestClient(app).post("/telemetry/client-error", json=BODY)

    assert response.status_code in (401, 403)


def test_extra_fields_are_refused(db: MagicMock, recorded: list[dict[str, Any]]) -> None:
    response = TestClient(app).post(
        "/telemetry/client-error", headers=AUTH, json={**BODY, "user_id": "someone"}
    )

    assert response.status_code == 422
    assert recorded == []


def test_long_fields_are_refused(db: MagicMock) -> None:
    response = TestClient(app).post(
        "/telemetry/client-error", headers=AUTH, json={**BODY, "message": "x" * 501}
    )

    assert response.status_code == 422


def test_it_is_rate_limited(db: MagicMock, recorded: list[dict[str, Any]]) -> None:
    client = TestClient(app)
    codes = [client.post("/telemetry/client-error", headers=AUTH, json=BODY).status_code for _ in range(11)]

    assert codes[:10] == [204] * 10
    assert codes[10] == 429
