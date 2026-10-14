"""Sign-up confirmed by code, tied to the tab that asked for it (decision 2026-10-14).

* The sign-up e-mail carries only a 6-digit code, no link.
* The code only counts in the session that signed up: `POST /auth/signup` answers a
  secret ticket that stays in that tab, and the account stays *pending* until
  `POST /auth/signup/activate` presents it. Supabase codes can be redeemed directly
  with the publishable key, so whoever receives the e-mail by mistake (a mistyped
  address) gets a Supabase session but no access to the Flyer API.
* A new sign-up that only clashes with pending accounts replaces them (wrong e-mail,
  closed tab, lost code); a clash with an active account is still a 409.
"""

from __future__ import annotations

import hashlib
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
from app.services.db_service import DBService

AUTH = {"Authorization": "Bearer token"}
SIGNUP = {
    "name": "Ana Lima",
    "email": "ana@example.com",
    "cpf": "529.982.247-25",
    "phone": "(81) 91234-5678",
    "password": "frevo2026",
}


def _payload(user_id: str = "user-9", method: str = "otp") -> dict[str, Any]:
    return {
        "sub": user_id,
        "email": "ana@example.com",
        "session_id": "sess-9",
        "amr": [{"method": method, "timestamp": 1760000000}],
    }


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def db() -> Iterator[MagicMock]:
    limiter.reset()
    deps.reset_verified_sessions_cache()
    fake = MagicMock()
    fake.get_events_by_creator = AsyncMock(return_value=[])
    fake.is_session_verified = AsyncMock(return_value=True)
    fake.get_account_state = AsyncMock(return_value="pending")
    fake.get_profile_identity = AsyncMock(
        return_value={"cpf": "52998224725", "phone": "+5581912345678", "signup_pending": True}
    )
    fake.find_identity_conflicts = AsyncMock(return_value=set())
    fake.find_pending_signups = AsyncMock(return_value=[])
    fake.delete_auth_user = AsyncMock()
    fake.signup_user = AsyncMock(return_value="user-9")
    fake.create_signup_ticket = AsyncMock()
    fake.consume_signup_ticket = AsyncMock(return_value=True)
    fake.activate_signup = AsyncMock()
    app.dependency_overrides[get_db_service] = lambda: fake
    with patch("app.api.deps.DBService", return_value=fake):
        yield fake
    app.dependency_overrides.clear()


def _as(payload: dict[str, Any]) -> Any:
    return patch("app.api.deps.decode_access_token", return_value=payload)


# ----------------------------------------------------------------- sign-up --


def test_sign_up_answers_a_ticket_and_stores_only_its_hash(db: MagicMock) -> None:
    response = TestClient(app).post("/auth/signup", json=SIGNUP)

    assert response.status_code == 201
    ticket = response.json()["ticket"]
    assert len(ticket) >= 40
    db.create_signup_ticket.assert_awaited_once_with("user-9", _sha256(ticket))


def test_each_sign_up_gets_its_own_ticket(db: MagicMock) -> None:
    client = TestClient(app)
    first = client.post("/auth/signup", json=SIGNUP).json()["ticket"]
    second = client.post("/auth/signup", json=SIGNUP).json()["ticket"]

    assert first != second


def test_the_account_is_marked_as_a_flyer_sign_up(db: MagicMock) -> None:
    # The profile trigger reads `flyer_signup` and creates the profile as pending.
    TestClient(app).post("/auth/signup", json=SIGNUP)

    assert db.signup_user.await_args.kwargs["data"]["flyer_signup"] is True


def test_only_active_accounts_block_a_sign_up(db: MagicMock) -> None:
    db.find_identity_conflicts.return_value = {"cpf"}

    response = TestClient(app).post("/auth/signup", json=SIGNUP)

    assert response.status_code == 409
    assert response.json()["detail"] == "Este CPF já tem uma conta."
    assert db.find_identity_conflicts.await_args.kwargs["active_only"] is True
    db.delete_auth_user.assert_not_called()
    db.signup_user.assert_not_called()


def test_a_new_sign_up_replaces_pending_ones(db: MagicMock) -> None:
    # Wrong e-mail, a closed tab or a lost code: signing up again starts over.
    db.find_pending_signups.return_value = ["old-1", "old-2"]

    response = TestClient(app).post("/auth/signup", json=SIGNUP)

    assert response.status_code == 201
    db.find_pending_signups.assert_awaited_once_with(
        email="ana@example.com", cpf="52998224725", phone="+5581912345678"
    )
    assert [c.args[0] for c in db.delete_auth_user.await_args_list] == ["old-1", "old-2"]
    db.signup_user.assert_awaited_once()


def test_completing_a_google_account_replaces_a_pending_sign_up(db: MagicMock) -> None:
    # The CPF was typed in a sign-up that never got its code: it does not block.
    db.get_profile_identity.return_value = {"cpf": None, "phone": None, "signup_pending": False}
    db.find_pending_signups.return_value = ["old-1"]
    db.complete_profile_identity = AsyncMock(return_value=True)

    with _as(_payload(user_id="google-1", method="oauth")):
        response = TestClient(app).put(
            "/auth/profile/identity", headers=AUTH, json={"cpf": "529.982.247-25", "phone": "(81) 91234-5678"}
        )

    assert response.status_code == 204
    db.delete_auth_user.assert_awaited_once_with("old-1")


# ----------------------------------------------------------- pending account --


def test_a_pending_account_cannot_use_the_app(db: MagicMock) -> None:
    # Someone who got the code by mistake opens a Supabase session with it.
    with _as(_payload()):
        response = TestClient(app).get("/events/mine", headers=AUTH)

    assert response.status_code == 403
    assert response.json()["detail"] == "signup_pending"
    db.get_events_by_creator.assert_not_called()


def test_an_active_account_passes_and_is_remembered(db: MagicMock) -> None:
    db.get_account_state.return_value = "ok"
    client = TestClient(app)

    with _as(_payload()):
        assert client.get("/events/mine", headers=AUTH).status_code == 200
        assert client.get("/events/mine", headers=AUTH).status_code == 200

    assert db.get_account_state.await_count == 1


def test_status_tells_the_app_the_sign_up_is_pending(db: MagicMock) -> None:
    with _as(_payload()):
        body = TestClient(app).get("/auth/profile/status", headers=AUTH).json()

    assert body["pending"] is True
    assert body["complete"] is True


# ---------------------------------------------------------------- activate --


def test_activating_with_the_ticket_of_this_tab(db: MagicMock) -> None:
    with _as(_payload()):
        response = TestClient(app).post("/auth/signup/activate", headers=AUTH, json={"ticket": "t" * 43})

    assert response.status_code == 204
    db.consume_signup_ticket.assert_awaited_once_with("user-9", _sha256("t" * 43))
    db.activate_signup.assert_awaited_once_with("user-9")


def test_after_activating_the_app_opens(db: MagicMock) -> None:
    client = TestClient(app)
    with _as(_payload()):
        client.post("/auth/signup/activate", headers=AUTH, json={"ticket": "t" * 43})
        db.get_account_state.return_value = "pending"  # the cache answers, not the db
        assert client.get("/events/mine", headers=AUTH).status_code == 200


def test_a_wrong_ticket_does_not_activate(db: MagicMock) -> None:
    # Another tab, another browser, or another account's ticket.
    db.consume_signup_ticket.return_value = False

    with _as(_payload()):
        response = TestClient(app).post("/auth/signup/activate", headers=AUTH, json={"ticket": "x" * 43})

    assert response.status_code == 403
    db.activate_signup.assert_not_called()


def test_activate_refuses_extra_fields(db: MagicMock) -> None:
    with _as(_payload()):
        response = TestClient(app).post(
            "/auth/signup/activate", headers=AUTH, json={"ticket": "t" * 43, "user_id": "other"}
        )

    assert response.status_code == 422
    db.consume_signup_ticket.assert_not_called()


def test_activate_is_rate_limited(db: MagicMock) -> None:
    db.consume_signup_ticket.return_value = False
    client = TestClient(app)
    with _as(_payload()):
        codes = [
            client.post("/auth/signup/activate", headers=AUTH, json={"ticket": "x" * 43}).status_code
            for _ in range(11)
        ]

    assert codes[:10] == [403] * 10
    assert codes[10] == 429


def test_activate_needs_a_token(db: MagicMock) -> None:
    response = TestClient(app).post("/auth/signup/activate", json={"ticket": "t" * 43})

    assert response.status_code in (401, 403)
    db.consume_signup_ticket.assert_not_called()


# ----------------------------------------------------------- db requests --


def _client(method: str, response: httpx.Response) -> MagicMock:
    client = MagicMock()
    setattr(client, method, AsyncMock(return_value=response))
    return client


def _response(status: int, body: Any = None) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("GET", "http://x"))


async def test_signup_returns_the_new_user_id_without_a_link() -> None:
    client = _client("post", _response(200, {"id": "user-9", "email": "ana@example.com"}))

    user_id = await DBService(client).signup_user(email="ana@example.com", password="frevo2026", data={})

    assert user_id == "user-9"
    assert "params" not in client.post.await_args.kwargs  # no redirect_to: the e-mail has no link


async def test_consume_ticket_deletes_only_a_matching_row() -> None:
    client = _client("delete", _response(200, [{"user_id": "user-9"}]))

    assert await DBService(client).consume_signup_ticket("user-9", "abc") is True

    params = client.delete.await_args.kwargs["params"]
    assert params == {"user_id": "eq.user-9", "ticket_hash": "eq.abc"}


async def test_consume_ticket_is_false_when_nothing_matches() -> None:
    client = _client("delete", _response(200, []))

    assert await DBService(client).consume_signup_ticket("user-9", "abc") is False


async def test_pending_signups_are_found_by_any_of_the_three() -> None:
    client = _client("get", _response(200, [{"id": "old-1"}]))

    ids = await DBService(client).find_pending_signups(
        email="ana@example.com", cpf="52998224725", phone="+5581912345678"
    )

    assert ids == ["old-1"]
    params = client.get.await_args.kwargs["params"]
    assert params["signup_pending"] == "is.true"
    assert "cpf.eq.52998224725" in params["or"]


async def test_active_only_conflicts_skip_pending_accounts() -> None:
    client = _client("get", _response(200, []))

    await DBService(client).find_identity_conflicts(
        email="ana@example.com", cpf="52998224725", phone="+5581912345678", active_only=True
    )

    assert client.get.await_args.kwargs["params"]["signup_pending"] == "is.false"


@pytest.mark.parametrize(
    ("row", "state"),
    [
        ({"cpf": "52998224725", "phone": "+5581912345678", "signup_pending": False}, "ok"),
        ({"cpf": "52998224725", "phone": "+5581912345678", "signup_pending": True}, "pending"),
        ({"cpf": None, "phone": None, "signup_pending": False}, "incomplete"),
    ],
)
async def test_account_state(row: dict[str, Any], state: str) -> None:
    client = _client("get", _response(200, [row]))

    assert await DBService(client).get_account_state("user-9") == state
