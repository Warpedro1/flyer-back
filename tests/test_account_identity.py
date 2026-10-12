"""CPF and phone on every account (decisions 2026-10-12).

* Sign-up by e-mail asks name, e-mail, CPF, mobile and password; CPF, mobile and e-mail
  are unique, and a taken one is said plainly ("Este CPF já tem uma conta").
* Accounts without CPF and phone (Google) complete them on the first access: until
  then the API answers 403 `profile_incomplete`, except the routes that complete it.
* CPF and phone are only ever shown to their owner, masked.
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
from app.services.db_service import DBService

AUTH = {"Authorization": "Bearer token"}
SIGNUP = {
    "name": "Ana Lima",
    "email": "Ana@Example.com",
    "cpf": "529.982.247-25",
    "phone": "(81) 91234-5678",
    "password": "frevo2026",
}


def _payload(method: str = "oauth") -> dict[str, Any]:
    return {
        "sub": "user-1",
        "email": "ana@example.com",
        "session_id": "sess-1",
        "amr": [{"method": method, "timestamp": 1760000000}],
    }


@pytest.fixture
def db() -> Iterator[MagicMock]:
    limiter.reset()
    deps.reset_verified_sessions_cache()
    fake = MagicMock()
    fake.get_events_by_creator = AsyncMock(return_value=[])
    fake.is_session_verified = AsyncMock(return_value=True)
    fake.is_profile_complete = AsyncMock(return_value=False)
    fake.get_profile_identity = AsyncMock(return_value={"cpf": None, "phone": None})
    fake.find_identity_conflicts = AsyncMock(return_value=set())
    fake.signup_user = AsyncMock()
    fake.complete_profile_identity = AsyncMock(return_value=True)
    app.dependency_overrides[get_db_service] = lambda: fake
    with patch("app.api.deps.DBService", return_value=fake):
        yield fake
    app.dependency_overrides.clear()


def _as(payload: dict[str, Any]) -> Any:
    return patch("app.api.deps.decode_access_token", return_value=payload)


# ----------------------------------------------------------------- sign-up --


def test_sign_up_creates_the_account_with_cpf_and_phone(db: MagicMock) -> None:
    response = TestClient(app).post("/auth/signup", json=SIGNUP)

    assert response.status_code == 201
    db.find_identity_conflicts.assert_awaited_once_with(
        email="ana@example.com", cpf="52998224725", phone="+5581912345678"
    )
    db.signup_user.assert_awaited_once_with(
        email="ana@example.com",
        password="frevo2026",
        data={"name": "Ana Lima", "full_name": "Ana Lima", "cpf": "52998224725", "phone": "+5581912345678"},
    )


@pytest.mark.parametrize(
    ("taken", "message"),
    [
        ({"cpf"}, "Este CPF já tem uma conta."),
        ({"phone"}, "Este telefone já tem uma conta."),
        ({"email"}, "Este e-mail já tem uma conta."),
    ],
)
def test_taken_cpf_phone_or_email_is_said_plainly(db: MagicMock, taken: set[str], message: str) -> None:
    db.find_identity_conflicts.return_value = taken

    response = TestClient(app).post("/auth/signup", json=SIGNUP)

    assert response.status_code == 409
    assert response.json()["detail"] == message
    db.signup_user.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cpf", "529.982.247-24"),
        ("phone", "(81) 3123-4567"),
        ("password", "abcdefgh"),
        ("password", "abc12"),
        ("email", "sem-arroba"),
        ("name", "  "),
    ],
)
def test_invalid_fields_are_refused_before_anything(db: MagicMock, field: str, value: str) -> None:
    response = TestClient(app).post("/auth/signup", json={**SIGNUP, field: value})

    assert response.status_code == 422
    db.find_identity_conflicts.assert_not_called()
    db.signup_user.assert_not_called()


def test_a_race_on_the_unique_cpf_is_a_conflict(db: MagicMock) -> None:
    # Two sign-ups with the same CPF at once: the unique index stops the second one
    # inside Supabase, which answers 500 "Database error saving new user".
    db.signup_user.side_effect = httpx.HTTPStatusError(
        "boom", request=httpx.Request("POST", "http://x"), response=httpx.Response(500)
    )

    response = TestClient(app).post("/auth/signup", json=SIGNUP)

    assert response.status_code == 409


def test_sign_up_is_rate_limited_per_client(db: MagicMock) -> None:
    client = TestClient(app)
    codes = [client.post("/auth/signup", json=SIGNUP).status_code for _ in range(6)]

    assert codes[:5] == [201] * 5
    assert codes[5] == 429


# ------------------------------------------------------- complete profile --


def test_an_account_without_cpf_and_phone_cannot_use_the_app(db: MagicMock) -> None:
    with _as(_payload("oauth")):
        response = TestClient(app).get("/events/mine", headers=AUTH)

    assert response.status_code == 403
    assert response.json()["detail"] == "profile_incomplete"


def test_a_complete_account_passes_and_is_remembered(db: MagicMock) -> None:
    db.is_profile_complete.return_value = True
    client = TestClient(app)

    with _as(_payload("oauth")):
        assert client.get("/events/mine", headers=AUTH).status_code == 200
        assert client.get("/events/mine", headers=AUTH).status_code == 200

    assert db.is_profile_complete.await_count == 1


def test_the_email_code_comes_before_the_profile(db: MagicMock) -> None:
    db.is_session_verified.return_value = False

    with _as(_payload("password")):
        response = TestClient(app).get("/events/mine", headers=AUTH)

    assert response.json()["detail"] == "email_code_required"


def test_status_shows_the_owner_masked_data(db: MagicMock) -> None:
    db.get_profile_identity.return_value = {"cpf": "52998224725", "phone": "+5581912345678"}

    with _as(_payload()):
        body = TestClient(app).get("/auth/profile/status", headers=AUTH).json()

    assert body == {"complete": True, "cpf_masked": "***.982.247-**", "phone_masked": "(81) 9****-5678"}


def test_status_of_an_incomplete_account(db: MagicMock) -> None:
    with _as(_payload()):
        body = TestClient(app).get("/auth/profile/status", headers=AUTH).json()

    assert body == {"complete": False, "cpf_masked": None, "phone_masked": None}


def test_completing_the_profile(db: MagicMock) -> None:
    with _as(_payload()):
        response = TestClient(app).put(
            "/auth/profile/identity", headers=AUTH, json={"cpf": "529.982.247-25", "phone": "(81) 91234-5678"}
        )

    assert response.status_code == 204
    db.find_identity_conflicts.assert_awaited_once_with(
        email=None, cpf="52998224725", phone="+5581912345678", exclude_user_id="user-1"
    )
    db.complete_profile_identity.assert_awaited_once_with("user-1", "52998224725", "+5581912345678")


def test_completing_with_a_taken_cpf(db: MagicMock) -> None:
    db.find_identity_conflicts.return_value = {"cpf"}

    with _as(_payload()):
        response = TestClient(app).put(
            "/auth/profile/identity", headers=AUTH, json={"cpf": "529.982.247-25", "phone": "(81) 91234-5678"}
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Este CPF já tem uma conta."
    db.complete_profile_identity.assert_not_called()


def test_cpf_cannot_be_changed_once_set(db: MagicMock) -> None:
    db.complete_profile_identity.return_value = False

    with _as(_payload()):
        response = TestClient(app).put(
            "/auth/profile/identity", headers=AUTH, json={"cpf": "529.982.247-25", "phone": "(81) 91234-5678"}
        )

    assert response.status_code == 409


def test_completing_needs_the_email_code_first(db: MagicMock) -> None:
    db.is_session_verified.return_value = False

    with _as(_payload("password")):
        response = TestClient(app).put(
            "/auth/profile/identity", headers=AUTH, json={"cpf": "529.982.247-25", "phone": "(81) 91234-5678"}
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "email_code_required"


# ----------------------------------------------------------- db requests --


async def test_signup_goes_to_supabase_with_the_metadata() -> None:
    client = MagicMock()
    client.post = AsyncMock(return_value=httpx.Response(200, json={}, request=httpx.Request("POST", "http://x")))

    await DBService(client).signup_user(email="ana@example.com", password="frevo2026", data={"cpf": "1"})

    args, kwargs = client.post.await_args
    assert args[0] == "/auth/v1/signup"
    assert kwargs["json"] == {"email": "ana@example.com", "password": "frevo2026", "data": {"cpf": "1"}}
    assert kwargs["params"]["redirect_to"].endswith("/")


async def test_conflicts_look_at_cpf_phone_and_email() -> None:
    rows = [{"id": "u2", "cpf": "52998224725", "phone": None, "email": "outra@example.com"}]
    client = MagicMock()
    client.get = AsyncMock(return_value=httpx.Response(200, json=rows, request=httpx.Request("GET", "http://x")))

    taken = await DBService(client).find_identity_conflicts(
        email="ana@example.com", cpf="52998224725", phone="+5581912345678"
    )

    assert taken == {"cpf"}
    or_filter = client.get.await_args.kwargs["params"]["or"]
    assert "cpf.eq.52998224725" in or_filter
    assert "phone.eq.+5581912345678" in or_filter
    assert "email.eq.ana@example.com" in or_filter


async def test_completing_only_fills_an_empty_cpf() -> None:
    client = MagicMock()
    client.patch = AsyncMock(
        return_value=httpx.Response(200, json=[{"id": "user-1"}], request=httpx.Request("PATCH", "http://x"))
    )

    assert await DBService(client).complete_profile_identity("user-1", "52998224725", "+5581912345678") is True

    params = client.patch.await_args.kwargs["params"]
    assert params == {"id": "eq.user-1", "cpf": "is.null"}
