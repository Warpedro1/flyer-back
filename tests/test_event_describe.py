"""The "Melhorar descrição" button (#22 part C, decisions 2026-10-18): POST /events/describe.

Free for everybody, 10 an hour per person; never invents facts; the input and the
suggestion go through the moderation; nothing is saved.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user_with_token, get_db_service
from app.core.config import settings
from app.core.limiter import limiter
from app.core.personal_data import CONTACT_REMOVED
from app.main import app
from app.services.ai_service import ai_service


@pytest.fixture
def client() -> Iterator[TestClient]:
    limiter.reset()
    app.dependency_overrides[get_current_user_with_token] = lambda: ("user-1", "jwt")
    app.dependency_overrides[get_db_service] = lambda: MagicMock()
    yield TestClient(app)
    app.dependency_overrides.clear()


def _ask(client: TestClient, **body: Any) -> Any:
    return client.post("/events/describe", json={"title": "Roda de samba", **body})


def test_it_suggests_from_what_the_creator_wrote(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    complete = AsyncMock(return_value="  Venha para a roda de samba no Recife Antigo.  ")
    monkeypatch.setattr(ai_service, "complete", complete)

    response = _ask(client, description="samba no recife antigo", category="musica_show")

    assert response.status_code == 200
    assert response.json() == {"description": "Venha para a roda de samba no Recife Antigo."}
    system, user = complete.await_args.args
    assert "Não invente datas, horários, preços, endereços" in system
    assert "Título: Roda de samba" in user
    assert "Categoria: Música/Show" in user
    assert "<descricao>\nsamba no recife antigo\n</descricao>" in user
    assert complete.await_args.kwargs["model"] == settings.DESCRIBE_MODEL == "gpt-4o-mini"


def test_a_long_suggestion_is_cut_at_a_sentence_and_loses_contacts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_text = "Frase com contato (81) 99999-8888. " + "Mais uma frase do evento. " * 120
    monkeypatch.setattr(ai_service, "complete", AsyncMock(return_value=long_text))

    text = _ask(client).json()["description"]

    assert len(text) <= 2000
    assert text.endswith(".")
    assert CONTACT_REMOVED in text and "99999" not in text


def test_contacts_in_the_input_are_refused(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    complete = AsyncMock()
    monkeypatch.setattr(ai_service, "complete", complete)

    response = _ask(client, description="Reservas: fulano@gmail.com")

    assert response.status_code == 422
    assert response.json()["code"] == "personal_data"
    complete.assert_not_awaited()


def test_a_refused_input_gets_no_suggestion(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def moderate(inputs: list[Any]) -> list[tuple[bool, list[str]]]:
        return [(False, ["hate"]) if "ódio" in t else (True, []) for t in inputs]

    monkeypatch.setattr(ai_service, "moderate_inputs", moderate)
    complete = AsyncMock()
    monkeypatch.setattr(ai_service, "complete", complete)

    response = _ask(client, description="texto de ódio")

    assert response.status_code == 422
    assert response.json()["detail"].startswith("A descrição foi recusada pela moderação (discurso de ódio)")
    complete.assert_not_awaited()


def test_a_refused_suggestion_is_not_shown(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def moderate(inputs: list[Any]) -> list[tuple[bool, list[str]]]:
        return [(False, ["violence"]) if "briga" in t else (True, []) for t in inputs]

    monkeypatch.setattr(ai_service, "moderate_inputs", moderate)
    monkeypatch.setattr(ai_service, "complete", AsyncMock(return_value="Uma briga feia."))

    response = _ask(client)

    assert response.status_code == 422
    assert response.json()["code"] == "content_blocked"


def test_without_the_model_there_is_a_clear_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, recorded: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(ai_service, "complete", AsyncMock(side_effect=RuntimeError("down")))

    response = _ask(client)

    assert response.status_code == 503
    assert response.json()["code"] == "describe_unavailable"
    assert any(r["code"] == "describe_unavailable" for r in recorded)


def test_ten_an_hour(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "complete", AsyncMock(return_value="Ok."))

    codes = [_ask(client).status_code for _ in range(11)]

    assert codes[:10] == [200] * 10
    assert codes[10] == 429


def test_the_suggestion_is_bounded_like_the_field(client: TestClient) -> None:
    assert _ask(client, description="x" * 2001).status_code == 422
