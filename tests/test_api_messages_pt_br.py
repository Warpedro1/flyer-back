"""Messages FastAPI and Pydantic write themselves also reach the user in pt-BR (#42).

`tests/test_pt_br.py` only reads our own string literals. Two kinds of message came
from the libraries, in English:

* no ``Authorization`` header: ``HTTPBearer`` answered 403 "Not authenticated";
* a 422: Pydantic's own messages ("Field required", "String should have at most…").

Now a missing token is 401 ``not_authenticated`` and every validation message is
translated, with the same ``{"detail": [{"loc", "msg", "type"}]}`` shape the app reads.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import get_db_service
from app.core.limiter import limiter
from app.core.validation_messages import translate_error
from app.main import app

ENGLISH = re.compile(r"\b(required|should|authenticated|valid|input|field|string|value error)\b", re.IGNORECASE)

SIGNUP = {
    "name": "Ana",
    "email": "ana@example.com",
    "cpf": "529.982.247-25",
    "phone": "(81) 99999-0000",
    "password": "frevo2026",
}


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    limiter.reset()
    deps.reset_verified_sessions_cache()
    # No lifespan here, so no shared client: the routes get a fake database.
    app.dependency_overrides[get_db_service] = lambda: MagicMock()
    yield
    app.dependency_overrides.clear()


def _messages(body: dict[str, object]) -> list[str]:
    detail = body["detail"]
    assert isinstance(detail, list)
    return [str(item["msg"]) for item in detail]  # type: ignore[index]


# ------------------------------------------------------------ no token --


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Basic YWRtaW46YWRtaW4="}, {"Authorization": "Bearer"}])
def test_without_a_bearer_token_is_401_not_authenticated_in_pt_br(headers: dict[str, str]) -> None:
    response = TestClient(app).get("/events/mine", headers=headers)

    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "not_authenticated"
    assert body["detail"] == "Entre na sua conta para continuar."
    assert body["request_id"]
    assert response.headers["www-authenticate"] == "Bearer"


def test_the_account_403s_stay_403() -> None:
    fake = MagicMock()
    fake.get_account_state = AsyncMock(return_value="incomplete")
    app.dependency_overrides[get_db_service] = lambda: fake
    payload = {"sub": "user-1", "amr": [{"method": "oauth", "timestamp": 1}]}
    with patch("app.api.deps.DBService", return_value=fake), patch(
        "app.api.deps.decode_access_token", return_value=payload
    ):
        response = TestClient(app).get("/events/mine", headers={"Authorization": "Bearer token"})

    assert response.status_code == 403
    assert response.json()["detail"] == "profile_incomplete"


# ---------------------------------------------------------- validation --


def test_a_missing_field_says_so_in_pt_br() -> None:
    body = {k: v for k, v in SIGNUP.items() if k != "cpf"}

    response = TestClient(app).post("/auth/signup", json=body)

    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == "validation_error"
    assert payload["request_id"]
    assert _messages(payload) == ["CPF: campo obrigatório."]
    assert payload["detail"][0]["loc"] == ["body", "cpf"]
    assert payload["detail"][0]["type"] == "missing"


def test_a_too_long_text_gives_the_limit() -> None:
    response = TestClient(app).post("/auth/signup", json={**SIGNUP, "name": "A" * 101})

    assert response.status_code == 422
    assert _messages(response.json()) == ["Nome: use no máximo 100 caracteres."]


def test_our_own_value_errors_lose_the_english_prefix() -> None:
    response = TestClient(app).post("/auth/signup", json={**SIGNUP, "email": "sem-arroba"})

    assert response.status_code == 422
    assert _messages(response.json()) == ["E-mail inválido."]


def test_a_body_that_is_not_json_is_explained() -> None:
    response = TestClient(app).post(
        "/auth/signup", content=b"{not json", headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert _messages(response.json()) == ["O corpo do pedido não é um JSON válido."]


def test_validation_errors_never_echo_what_was_sent() -> None:
    # Pydantic puts the rejected `input` (a password, a CPF…) in each error.
    response = TestClient(app).post("/auth/signup", json={**SIGNUP, "name": "A" * 101, "password": 7})

    for item in response.json()["detail"]:
        assert set(item) == {"loc", "msg", "type"}


def test_no_validation_message_is_in_english() -> None:
    bodies = [
        {},
        {**SIGNUP, "name": "A" * 101},
        {**SIGNUP, "password": 7},
        {**SIGNUP, "extra": "x"},
        {**SIGNUP, "name": ""},
    ]
    for body in bodies:
        response = TestClient(app).post("/auth/signup", json=body)
        assert response.status_code == 422, body
        for message in _messages(response.json()):
            assert not ENGLISH.search(message), message


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ({"type": "missing", "loc": ("body", "title")}, "Título: campo obrigatório."),
        ({"type": "string_too_short", "loc": ("body", "x"), "ctx": {"min_length": 3}}, "Use pelo menos 3 caracteres."),
        ({"type": "int_parsing", "loc": ("query", "limit")}, "Informe um número."),
        ({"type": "greater_than_equal", "loc": ("query", "limit"), "ctx": {"ge": 1}}, "O valor mínimo é 1."),
        ({"type": "less_than_equal", "loc": ("query", "limit"), "ctx": {"le": 50}}, "O valor máximo é 50."),
        ({"type": "datetime_from_date_parsing", "loc": ("body", "x")}, "Data inválida."),
        ({"type": "literal_error", "loc": ("body", "x")}, "Opção inválida."),
        ({"type": "extra_forbidden", "loc": ("body", "x")}, "Campo não permitido."),
        ({"type": "value_error", "loc": ("body", "cpf"), "msg": "Value error, CPF inválido."}, "CPF inválido."),
        ({"type": "something_new_in_pydantic", "loc": ("body", "x"), "msg": "Input is weird"}, "Valor inválido."),
    ],
)
def test_translate_error(error: dict[str, object], expected: str) -> None:
    assert translate_error(error) == expected
