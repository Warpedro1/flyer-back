"""Second factor by e-mail for password logins (decisions 2026-10-12).

A session opened with a password only reaches the API after the 6-digit code sent to
the account's e-mail is verified. Google sessions, and those opened from an e-mail
link (confirmation, recovery), already proved the e-mail and pass. Enforced here,
in the backend: a check in the app alone stops nobody who has the password.
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
from app.core.security import session_needs_email_code
from app.main import app
from app.services.db_service import DBService

AUTH = {"Authorization": "Bearer token"}


def _payload(method: str, session_id: str = "sess-1") -> dict[str, Any]:
    return {
        "sub": "user-1",
        "email": "ana@example.com",
        "session_id": session_id,
        "amr": [{"method": method, "timestamp": 1760000000}],
    }


@pytest.fixture
def db() -> Iterator[MagicMock]:
    limiter.reset()
    deps.reset_verified_sessions_cache()
    fake = MagicMock()
    fake.get_events_by_creator = AsyncMock(return_value=[])
    fake.send_email_code = AsyncMock()
    fake.verify_email_code = AsyncMock(return_value=True)
    fake.mark_session_verified = AsyncMock()
    fake.is_session_verified = AsyncMock(return_value=False)
    app.dependency_overrides[get_db_service] = lambda: fake
    with patch("app.api.deps.DBService", return_value=fake):
        yield fake
    app.dependency_overrides.clear()


def _as(payload: dict[str, Any]) -> Any:
    return patch("app.api.deps.decode_access_token", return_value=payload)


# ------------------------------------------------------------------ the rule --


@pytest.mark.parametrize(
    ("amr", "needs"),
    [
        ([{"method": "password"}], True),
        ([{"method": "oauth"}], False),
        ([{"method": "otp"}], False),
        ([{"method": "recovery"}], False),
        ([], False),
        (None, False),
    ],
)
def test_only_password_sessions_need_the_code(amr: Any, needs: bool) -> None:
    assert session_needs_email_code({"sub": "u", "amr": amr}) is needs


# ------------------------------------------------------------ enforcement --


def test_a_password_session_without_the_code_is_refused(db: MagicMock) -> None:
    with _as(_payload("password")):
        response = TestClient(app).get("/events/mine", headers=AUTH)

    assert response.status_code == 403
    assert response.json()["detail"] == "email_code_required"
    db.get_events_by_creator.assert_not_called()


@pytest.mark.parametrize("method", ["oauth", "otp", "recovery"])
def test_google_and_email_link_sessions_pass(db: MagicMock, method: str) -> None:
    with _as(_payload(method)):
        response = TestClient(app).get("/events/mine", headers=AUTH)

    assert response.status_code == 200
    db.is_session_verified.assert_not_called()


def test_after_the_code_the_same_session_passes(db: MagicMock) -> None:
    db.is_session_verified.return_value = True

    with _as(_payload("password")):
        response = TestClient(app).get("/events/mine", headers=AUTH)

    assert response.status_code == 200
    db.is_session_verified.assert_awaited_once_with("sess-1")


def test_a_verified_session_is_remembered(db: MagicMock) -> None:
    db.is_session_verified.return_value = True
    client = TestClient(app)

    with _as(_payload("password")):
        client.get("/events/mine", headers=AUTH)
        client.get("/events/mine", headers=AUTH)

    assert db.is_session_verified.await_count == 1


def test_a_password_session_without_session_id_is_refused(db: MagicMock) -> None:
    payload = _payload("password")
    del payload["session_id"]

    with _as(payload):
        assert TestClient(app).get("/events/mine", headers=AUTH).status_code == 403


# ------------------------------------------------------------------ routes --


def test_status_says_whether_the_code_is_needed(db: MagicMock) -> None:
    client = TestClient(app)

    with _as(_payload("password")):
        assert client.get("/auth/email-code/status", headers=AUTH).json() == {
            "required": True,
            "verified": False,
        }
    with _as(_payload("oauth")):
        assert client.get("/auth/email-code/status", headers=AUTH).json() == {
            "required": False,
            "verified": True,
        }


def test_the_code_goes_to_the_accounts_email(db: MagicMock) -> None:
    with _as(_payload("password")):
        response = TestClient(app).post("/auth/email-code/send", headers=AUTH)

    assert response.status_code == 204
    db.send_email_code.assert_awaited_once_with("ana@example.com")


def test_send_takes_no_address_from_the_client(db: MagicMock) -> None:
    with _as(_payload("password")):
        response = TestClient(app).post(
            "/auth/email-code/send", headers=AUTH, json={"email": "evil@example.com"}
        )

    assert response.status_code in {204, 422}
    for call in db.send_email_code.await_args_list:
        assert call.args == ("ana@example.com",)


def test_no_code_for_sessions_that_do_not_need_it(db: MagicMock) -> None:
    with _as(_payload("oauth")):
        response = TestClient(app).post("/auth/email-code/send", headers=AUTH)

    assert response.status_code == 409
    db.send_email_code.assert_not_called()


def test_the_right_code_verifies_the_session(db: MagicMock) -> None:
    client = TestClient(app)
    with _as(_payload("password")):
        response = client.post("/auth/email-code/verify", headers=AUTH, json={"code": "123456"})

        assert response.status_code == 204
        db.verify_email_code.assert_awaited_once_with("ana@example.com", "123456")
        db.mark_session_verified.assert_awaited_once_with("sess-1", "user-1")
        # Remembered without another trip to the database.
        db.is_session_verified.return_value = False
        assert client.get("/events/mine", headers=AUTH).status_code == 200


def test_a_wrong_code_is_refused(db: MagicMock) -> None:
    db.verify_email_code.return_value = False

    with _as(_payload("password")):
        response = TestClient(app).post(
            "/auth/email-code/verify", headers=AUTH, json={"code": "000000"}
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Código inválido ou expirado."
    db.mark_session_verified.assert_not_called()


@pytest.mark.parametrize("code", ["12345", "1234567", "abcdef", ""])
def test_the_code_has_six_digits(db: MagicMock, code: str) -> None:
    with _as(_payload("password")):
        response = TestClient(app).post("/auth/email-code/verify", headers=AUTH, json={"code": code})

    assert response.status_code == 422
    db.verify_email_code.assert_not_called()


def test_sending_is_rate_limited(db: MagicMock) -> None:
    client = TestClient(app)
    with _as(_payload("password")):
        codes = [client.post("/auth/email-code/send", headers=AUTH).status_code for _ in range(4)]

    assert codes == [204, 204, 204, 429]


def test_guessing_is_rate_limited(db: MagicMock) -> None:
    db.verify_email_code.return_value = False
    client = TestClient(app)
    with _as(_payload("password")):
        codes = [
            client.post("/auth/email-code/verify", headers=AUTH, json={"code": "000000"}).status_code
            for _ in range(11)
        ]

    assert codes[-1] == 429
    assert codes[:10] == [400] * 10


# ----------------------------------------------------------- db requests --


def _client(*responses: httpx.Response) -> MagicMock:
    client = MagicMock()
    client.post = AsyncMock(side_effect=list(responses))
    return client


def _resp(status: int, payload: Any = None, method: str = "POST") -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request(method, "http://x"))


async def test_send_asks_supabase_for_a_code_without_creating_users() -> None:
    client = _client(_resp(200, {}))

    await DBService(client).send_email_code("ana@example.com")

    url = client.post.await_args.args[0]
    assert url == "/auth/v1/otp"
    assert client.post.await_args.kwargs["json"] == {"email": "ana@example.com", "create_user": False}


async def test_a_valid_code_is_checked_with_supabase_and_the_extra_session_revoked() -> None:
    client = _client(_resp(200, {"access_token": "extra-session"}), _resp(204))

    assert await DBService(client).verify_email_code("ana@example.com", "123456") is True

    verify, logout = client.post.await_args_list
    assert verify.args[0] == "/auth/v1/verify"
    assert verify.kwargs["json"] == {"type": "email", "email": "ana@example.com", "token": "123456"}
    assert logout.args[0] == "/auth/v1/logout"
    assert logout.kwargs["headers"]["Authorization"] == "Bearer extra-session"


async def test_an_invalid_code_is_false() -> None:
    client = _client(_resp(403, {"error_code": "otp_expired"}))

    assert await DBService(client).verify_email_code("ana@example.com", "000000") is False
    assert client.post.await_count == 1
