"""Errors that reach the user with a code and a reference, and stay on record (#30, #25 phase 1).

* Every response carries ``X-Request-Id``: the user can quote it, and it ties the
  response to the log line and to the ``app_errors`` row.
* An unhandled exception answers 500 ``internal_error`` **with** the CORS headers, so
  the browser can read it (it used to be a bare "network error").
* 5xx and unhandled errors are written to ``app_errors`` (30 days), without e-mail,
  CPF or phone, and with the user as a hash.
* A rate limit answers ``detail`` and ``code`` like every other error.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import get_db_service
from app.core.limiter import limiter
from app.main import app
from app.services import error_log

AUTH = {"Authorization": "Bearer token"}
ORIGIN = {"Origin": "https://flyer-inky.vercel.app"}
PAYLOAD = {"sub": "user-9", "email": "ana@example.com", "amr": [{"method": "oauth", "timestamp": 1}]}


@pytest.fixture
def db() -> Iterator[MagicMock]:
    limiter.reset()
    deps.reset_verified_sessions_cache()
    fake = MagicMock()
    fake.get_account_state = AsyncMock(return_value="ok")
    fake.get_events_by_creator = AsyncMock(return_value=[])
    app.dependency_overrides[get_db_service] = lambda: fake
    with patch("app.api.deps.DBService", return_value=fake), patch(
        "app.api.deps.decode_access_token", return_value=PAYLOAD
    ):
        yield fake
    app.dependency_overrides.clear()


# -------------------------------------------------------------- request id --


def test_every_response_has_a_request_id() -> None:
    response = TestClient(app).get("/health")

    assert len(response.headers["x-request-id"]) >= 8


def test_a_valid_request_id_from_the_client_is_kept() -> None:
    response = TestClient(app).get("/health", headers={"X-Request-Id": "app-1234abcd"})

    assert response.headers["x-request-id"] == "app-1234abcd"


def test_a_strange_request_id_is_replaced() -> None:
    response = TestClient(app).get("/health", headers={"X-Request-Id": "<script>"})

    assert response.headers["x-request-id"] != "<script>"


def test_the_browser_can_read_the_request_id() -> None:
    response = TestClient(app).get("/health", headers=ORIGIN)

    assert "x-request-id" in response.headers["access-control-expose-headers"].lower()


# ------------------------------------------------------- unhandled errors --


def test_an_unhandled_error_is_a_500_with_cors_code_and_reference(db: MagicMock) -> None:
    db.get_events_by_creator.side_effect = RuntimeError("boom")

    response = TestClient(app).get("/events/mine", headers={**AUTH, **ORIGIN})

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == "*"
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["request_id"] == response.headers["x-request-id"]
    assert isinstance(body["detail"], str)


def test_an_unhandled_error_is_recorded_without_personal_data(
    db: MagicMock, recorded: list[dict[str, Any]]
) -> None:
    db.get_events_by_creator.side_effect = RuntimeError(
        "falhou para ana@example.com cpf 529.982.247-25 tel +5581912345678"
    )

    response = TestClient(app).get("/events/mine", headers=AUTH)

    assert len(recorded) == 1
    row = recorded[0]
    assert row["source"] == "api"
    assert row["status"] == 500
    assert row["code"] == "internal_error"
    assert row["error_type"] == "RuntimeError"
    assert row["method"] == "GET"
    assert row["route"] == "/events/mine"
    assert row["request_id"] == response.headers["x-request-id"]
    assert row["user_hash"] == hashlib.sha256(b"user-9").hexdigest()[:16]
    for secret in ("ana@example.com", "529.982.247-25", "+5581912345678", "user-9"):
        assert secret not in str(row)


def test_recording_failures_never_change_the_answer(db: MagicMock) -> None:
    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
    error_log.set_client(client)
    db.get_events_by_creator.side_effect = RuntimeError("boom")
    try:
        response = TestClient(app).get("/events/mine", headers=AUTH)
    finally:
        error_log.set_client(None)

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"


def test_a_5xx_raised_on_purpose_is_recorded_with_its_code(
    db: MagicMock, recorded: list[dict[str, Any]]
) -> None:
    failure = HTTPException(status_code=503, detail={"code": "moderation_unavailable", "reason_codes": []})
    with patch("app.api.chat.run_complete_onboarding_pipeline", AsyncMock(side_effect=failure)):
        response = TestClient(app).post(
            "/chat/complete-onboarding", headers=AUTH, json={"answers": {"a": "b"}}
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "moderation_unavailable"
    assert [r["code"] for r in recorded] == ["moderation_unavailable"]
    assert recorded[0]["status"] == 503


def test_4xx_are_not_recorded(db: MagicMock, recorded: list[dict[str, Any]]) -> None:
    TestClient(app).get("/events/mine")  # no token: 401/403

    assert recorded == []


# ------------------------------------------------------------- rate limit --


def test_a_rate_limit_answers_detail_and_code() -> None:
    limiter.reset()
    client = TestClient(app)
    codes = [client.get("/").status_code for _ in range(31)]

    assert codes[-1] == 429
    body = client.get("/").json()
    assert body["code"] == "rate_limited"
    assert "Muitos pedidos" in body["detail"]
    limiter.reset()
