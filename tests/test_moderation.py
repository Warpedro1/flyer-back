"""Content moderation of events and places (#22 part A, decisions 2026-10-18).

* Created or edited in the app: refused before anything is written, the reason in
  pt-BR; third-party personal data refused too; moderation down = 503, never a pass.
* Imported: refused text never enters (an existing row is hidden), contacts become
  "[contato removido]", a refused or unreadable cover is dropped, what could not be
  checked waits; unchanged rows are not checked again.
* The catalogue that was here before: POST /admin/events/moderation.

The network is stubbed by the autouse fixture in conftest (everything allowed);
these tests set `ai_service.moderate_inputs` to say otherwise.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import APIStatusError

from app.api.deps import get_current_user_with_token, get_db_service
from app.core.config import settings
from app.core.errors import ApiError
from app.core.limiter import limiter
from app.core.personal_data import CONTACT_REMOVED, find_personal_data, redact_personal_data
from app.main import app
from app.services import moderation
from app.services.ai_service import AIService, ai_service
from app.services.db_service import DBService
from app.services.etl import pipeline
from app.services.etl.load import upsert_events
from app.services.etl.schemas import StandardizedEvent
from app.services.etl.sources import SOURCES
from app.services.moderation import (
    PassResult,
    Verdict,
    check_event_content,
    check_imports,
    event_moderation_hash,
    moderate_stale,
    reasons,
)
from app.models.schemas import EventCreate

# Taken at import, before the autouse fixture swaps it for a stub.
from app.services.moderation import path_verdicts as real_path_verdicts

VEC = [0.1] * 1536
ADMIN = "admin-token-for-tests"


def _says(flagged: dict[str, list[str]], *, images: Any = None) -> Any:
    """A `moderate_inputs` that flags the texts containing a key (with those
    categories) and answers images with ``images`` (a verdict tuple or an exception)."""

    async def moderate(inputs: list[Any]) -> list[tuple[bool, list[str]]]:
        if inputs and isinstance(inputs[0], dict):
            if isinstance(images, BaseException):
                raise images
            return [images or (True, [])]
        out = []
        for text in inputs:
            hit = next((cats for word, cats in flagged.items() if word in text), None)
            out.append((False, hit) if hit else (True, []))
        return out

    return moderate


# ------------------------------------------------------------ personal data --


@pytest.mark.parametrize(
    ("text", "kinds"),
    [
        ("Ligue (81) 99999-8888", ["telefone"]),
        ("WhatsApp +55 (81) 9 9999-8888", ["telefone"]),
        ("fixo (81) 3333-4444", ["telefone"]),
        ("escreva para fulano@gmail.com", ["e-mail"]),
        ("CPF 529.982.247-25", ["CPF"]),
        ("52998224725", ["CPF"]),
        ("fulano@gmail.com ou 81 99999-8888", ["e-mail", "telefone"]),
        # As people write them in Recife: no area code, area code with a 0, dashes
        # pasted from Instagram or Word (#22 A review).
        ("Reservas pelo zap: 99999-8888", ["telefone"]),
        ("(081) 99999-8888", ["telefone"]),
        ("081 3333-4444", ["telefone"]),
        ("(81) 99999\u20138888", ["telefone"]),
        ("81 9 9999 8888", ["telefone"]),
        ("wa.me/5581999998888", ["telefone"]),
        ("Informações e reservas: 9 9876-5432 (Maria)", ["telefone"]),
        ("Reservas pelo zap 9.8765-4321", ["telefone"]),
        ("Reservas: (81) 98765 - 4321", ["telefone"]),
        ("(81) 99999 \u2013 8888", ["telefone"]),
        ("81 99999\u22128888", ["telefone"]),
        ("2025-2026, 2025\u20132026, das 19:00 às 23:00, 18/10/2026, 2026-10-18", []),
        ("Senha 1234-5678, sala 9000-1000", []),
        # Ranges with a spaced dash after a small number (#22 A review, round 3).
        ("Copa Sub-17 2025 - 2026", []),
        ("Baile de Formatura Turma 15 2020 - 2025", []),
        ("Categorias\nSub 11\n2014 - 2015\nSub 13\n2012 - 2013", []),
        ("Temporada 9 2025 - 2026", []),
        ("Capacidade: 15 3000 - 4000 pessoas", []),
        ("Anos 80 2000 - 2010", []),
        ("Copa Sub-17 2025-2026", []),
        ("Turma 15 2020-2025", []),
        ("Temporada 9 2025-2026", []),
        ("Sub 11 2014-2015, Anos 80 2000-2010", []),
        # With the area code in parentheses a spaced dash is a phone again.
        ("Informações: (81) 3222 - 1234", ["telefone"]),
        ("WhatsApp: 9 8765 - 4321", ["telefone"]),
        ("Ingresso R$ 1.234,56, dia 12/10/2026 às 19:30", []),
        ("CNPJ 12.345.678/0001-90, CEP 50030-230", []),
        ("Rua da Aurora, 1234, sala 3001", []),
        ("12345678901", []),
    ],
)
def test_personal_data_is_found_without_taking_prices_dates_or_addresses(text: str, kinds: list[str]) -> None:
    assert find_personal_data(text) == kinds


def test_dashes_and_the_mobile_without_area_code_are_replaced() -> None:
    assert redact_personal_data("Zap 99999\u20138888, fixo (081) 3333-4444.") == (
        f"Zap {CONTACT_REMOVED}, fixo {CONTACT_REMOVED}."
    )


def test_contacts_are_replaced() -> None:
    text = "Fale com fulano@gmail.com ou (81) 99999-8888. CPF 529.982.247-25. Entrada R$ 20."

    assert redact_personal_data(text) == (
        f"Fale com {CONTACT_REMOVED} ou {CONTACT_REMOVED}. CPF {CONTACT_REMOVED}. Entrada R$ 20."
    )


def test_reasons_are_in_portuguese() -> None:
    assert reasons(["sexual", "violence_graphic", "violence/graphic"]) == "conteúdo sexual, violência explícita"
    assert reasons(["something_new"]) == "conteúdo impróprio"


# --------------------------------------------------------- the moderation --


def _status_error(status: int) -> APIStatusError:
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.openai.com/v1/moderations"))
    return APIStatusError("bad image", response=response, body=None)


async def test_one_request_gives_one_verdict_per_text(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[Any]] = []

    async def moderate(inputs: list[Any]) -> list[tuple[bool, list[str]]]:
        calls.append(inputs)
        return [(False, ["hate"]) if "ruim" in t else (True, []) for t in inputs]

    monkeypatch.setattr(ai_service, "moderate_inputs", moderate)

    out = await moderation.text_verdicts(["bom", "", "ruim"])

    assert out == [Verdict(True), Verdict(True), Verdict(False, ("hate",))]
    # The empty text is not sent.
    assert calls == [["bom", "ruim"]]


async def test_a_moderation_that_fails_is_unavailable_and_recorded(
    monkeypatch: pytest.MonkeyPatch, recorded: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(ai_service, "moderate_inputs", AsyncMock(side_effect=RuntimeError("down")))

    with pytest.raises(moderation.ModerationUnavailable):
        await moderation.text_verdicts(["bom"])

    assert recorded[0]["code"] == "moderation_api_error"
    assert recorded[0]["details"]["stage"] == "text"


async def test_an_image_the_moderation_cannot_read_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "moderate_inputs", AsyncMock(side_effect=_status_error(400)))

    assert await moderation.image_verdict("https://x/a.jpg") == Verdict(False, unreadable=True)


async def test_a_server_error_on_an_image_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "moderate_inputs", AsyncMock(side_effect=_status_error(500)))

    with pytest.raises(moderation.ModerationUnavailable):
        await moderation.image_verdict("https://x/a.jpg")


async def test_the_sdk_call_carries_the_model_and_maps_the_categories() -> None:
    flagged = MagicMock(flagged=True)
    flagged.categories.model_dump.return_value = {"sexual": True, "violence": False}
    resp = MagicMock(results=[MagicMock(flagged=False), flagged])
    create = AsyncMock(return_value=resp)

    with patch.object(ai_service._client.moderations, "create", create):
        out = await AIService.moderate_inputs(ai_service, ["a", "b"])

    assert out == [(True, []), (False, ["sexual"])]
    assert create.await_args.kwargs["model"] == settings.MODERATION_MODEL
    assert create.await_args.kwargs["input"] == ["a", "b"]


async def test_private_photos_are_signed_for_two_minutes_only() -> None:
    db = MagicMock()
    db.sign_media_paths = AsyncMock(return_value={"user-1/a.jpg": "https://signed/a"})

    with patch.object(moderation, "image_verdicts", AsyncMock(return_value=[Verdict(True)])) as images:
        await real_path_verdicts(db, ["user-1/a.jpg"])

    assert db.sign_media_paths.await_args.kwargs == {"ttl": 120}
    images.assert_awaited_once_with(["https://signed/a"])


async def test_a_photo_that_no_longer_exists_is_not_let_through() -> None:
    # Storage leaves out what it cannot sign; retrying never helps.
    db = MagicMock()
    db.sign_media_paths = AsyncMock(return_value={})

    assert await real_path_verdicts(db, ["user-1/a.jpg"]) == [Verdict(False, unreadable=True, missing=True)]


async def test_a_storage_failure_while_signing_is_unavailable_and_recorded(recorded: list[dict[str, Any]]) -> None:
    db = MagicMock()
    db.sign_media_paths = AsyncMock(side_effect=httpx.ConnectError("storage down"))

    with pytest.raises(moderation.ModerationUnavailable):
        await real_path_verdicts(db, ["user-1/a.jpg"])

    assert [r["code"] for r in recorded] == ["storage_sign_error"]


@pytest.mark.parametrize(
    ("media", "icon", "message"),
    [
        ([{"media_url": "user-1/sumiu.jpg", "type": "image"}], None, "Uma das fotos não está mais disponível. Remova e envie de novo."),
        ([], "user-1/sumiu.png", "O ícone do troféu não está mais disponível. Escolha outro e tente de novo."),
    ],
)
async def test_a_file_that_no_longer_exists_is_refused_saying_so(
    monkeypatch: pytest.MonkeyPatch, media: list[dict[str, Any]], icon: str | None, message: str
) -> None:
    # Saving must be the way out for an event the pass hid for a missing photo.
    monkeypatch.setattr(moderation, "path_verdicts", real_path_verdicts)
    db = MagicMock()
    db.sign_media_paths = AsyncMock(return_value={})
    trophy = {"name": "Passista", "icon_url": icon} if icon else None

    with pytest.raises(ApiError) as caught:
        await check_event_content(db, _body(media=media, trophy=trophy))

    assert caught.value.status_code == 422
    assert caught.value.code == "content_blocked"
    assert caught.value.message == message


# --------------------------------------------------- created in the app --


def _body(**overrides: Any) -> EventCreate:
    data: dict[str, Any] = {
        "title": "Show de frevo",
        "description": "No Marco Zero",
        "lat": -8.06,
        "long": -34.87,
        "event_date": "2026-11-16T20:00:00",
        "ends_at": "2026-11-16T23:00:00",
    }
    data.update(overrides)
    return EventCreate(**data)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"description": "Dúvidas: (81) 99999-8888"}, "A descrição tem um telefone."),
        ({"title": "Show fulano@gmail.com"}, "O título tem um e-mail."),
        ({"trophy": {"name": "Troféu 529.982.247-25"}}, "O nome do troféu tem um CPF."),
        ({"location_name": "Casa, 81 99999-8888, a@b.com"}, "O local tem um e-mail e um telefone."),
    ],
)
async def test_personal_data_is_refused_saying_where(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ApiError) as caught:
        await check_event_content(MagicMock(), _body(**overrides))

    assert caught.value.status_code == 422
    assert caught.value.code == "personal_data"
    assert str(caught.value.message).startswith(message)


async def test_a_refused_text_says_which_and_why(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "moderate_inputs", _says({"frevo": ["violence"]}))

    with pytest.raises(ApiError) as caught:
        await check_event_content(MagicMock(), _body(title="Briga de frevo", description="ok"))

    assert caught.value.status_code == 422
    assert caught.value.code == "content_blocked"
    assert caught.value.message.startswith("O título foi recusado pela moderação (violência)")


async def test_every_text_the_creator_writes_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[Any]] = []

    async def moderate(inputs: list[Any]) -> list[tuple[bool, list[str]]]:
        seen.append(inputs)
        return [(True, [])] * len(inputs)

    monkeypatch.setattr(ai_service, "moderate_inputs", moderate)

    await check_event_content(
        MagicMock(),
        _body(location_name="Marco Zero", trophy={"name": "Frevista", "description": "Dançou"}),
    )

    assert seen == [["Show de frevo", "No Marco Zero", "Marco Zero", "Frevista", "Dançou"]]


async def test_without_the_moderation_nothing_is_published(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "moderate_inputs", AsyncMock(side_effect=RuntimeError("down")))

    with pytest.raises(ApiError) as caught:
        await check_event_content(MagicMock(), _body())

    assert (caught.value.status_code, caught.value.code) == (503, "moderation_unavailable")


async def test_photos_and_the_icon_are_checked_but_not_videos(monkeypatch: pytest.MonkeyPatch) -> None:
    checked = AsyncMock(return_value=[Verdict(True), Verdict(True)])
    monkeypatch.setattr(moderation, "path_verdicts", checked)
    body = _body(
        media=[{"media_url": "user-1/a.jpg", "type": "image"}, {"media_url": "user-1/b.mp4", "type": "video"}],
        trophy={"icon_url": "user-1/icon.png"},
    )

    await check_event_content(MagicMock(), body)

    assert checked.await_args.args[1] == ["user-1/a.jpg", "user-1/icon.png"]


@pytest.mark.parametrize(
    ("verdicts", "message"),
    [
        ([Verdict(False, ("sexual",))], "Uma das fotos foi recusada pela moderação (conteúdo sexual)"),
        ([Verdict(False, unreadable=True)], "Não conseguimos verificar uma das imagens"),
    ],
)
async def test_a_refused_photo_is_refused(
    monkeypatch: pytest.MonkeyPatch, verdicts: list[Verdict], message: str
) -> None:
    monkeypatch.setattr(moderation, "path_verdicts", AsyncMock(return_value=verdicts))

    with pytest.raises(ApiError) as caught:
        await check_event_content(MagicMock(), _body(media=[{"media_url": "user-1/a.jpg", "type": "image"}]))

    assert caught.value.code == "content_blocked"
    assert caught.value.message.startswith(message)


@pytest.fixture
def api() -> Iterator[MagicMock]:
    fake = MagicMock()
    fake.create_events_bulk = AsyncMock(
        return_value=[{"id": "evt-1", "creator_id": "user-1", "title": "Show de frevo", "lat": -8.06, "long": -34.87}]
    )
    fake.insert_event_media_rows = AsyncMock()
    fake.set_events_embedding = AsyncMock()
    fake.set_events_keywords = AsyncMock()
    fake.batch_fetch_event_media = AsyncMock(return_value={})
    limiter.reset()
    app.dependency_overrides[get_current_user_with_token] = lambda: ("user-1", "jwt")
    app.dependency_overrides[get_db_service] = lambda: fake
    with patch("app.api.events.ai_service.generate_embedding", AsyncMock(return_value=None)):
        yield fake
    app.dependency_overrides.clear()


def _create_body(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "title": "Show de frevo",
        "description": "No Marco Zero",
        "lat": -8.06,
        "long": -34.87,
        "event_date": "2026-11-16T20:00:00",
        "ends_at": "2026-11-16T23:00:00",
    }
    data.update(overrides)
    return data


def test_a_refused_event_writes_nothing(api: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "moderate_inputs", _says({"frevo": ["hate"]}))

    response = TestClient(app).post("/events/", json=_create_body())

    assert response.status_code == 422
    assert response.json()["code"] == "content_blocked"
    assert "discurso de ódio" in response.json()["detail"]
    api.create_events_bulk.assert_not_called()


def test_an_accepted_event_is_stored_as_checked(api: MagicMock) -> None:
    response = TestClient(app).post("/events/", json=_create_body())

    assert response.status_code == 201
    row = api.create_events_bulk.await_args.args[1][0]
    assert row["moderation_hash"] == event_moderation_hash("Show de frevo", "No Marco Zero", None)
    assert row["moderation_blocked"] is False
    assert row["moderated_at"]


def test_a_refused_edit_writes_nothing(api: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    api.get_event_by_id = AsyncMock(return_value={"id": "evt-1", "creator_id": "user-1", "kind": "event"})
    api.update_event = AsyncMock()
    monkeypatch.setattr(ai_service, "moderate_inputs", _says({"frevo": ["harassment"]}))

    response = TestClient(app).put("/events/evt-1", json=_create_body())

    assert response.status_code == 422
    api.update_event.assert_not_called()


def test_a_hidden_event_is_not_found(api: MagicMock) -> None:
    api.get_event_by_id = AsyncMock(return_value={"id": "evt-1", "moderation_blocked": True, "title": "x"})

    assert TestClient(app).get("/events/evt-1").status_code == 404


# ----------------------------------------------------------------- import --


def _imported(n: int, **overrides: Any) -> StandardizedEvent:
    data: dict[str, Any] = {
        "source": "sympla",
        "source_event_id": f"s{n}",
        "title": f"Show {n}",
        "description": "Noite animada",
        "category": "musica_show",
        "lat": -8.06,
        "long": -34.88,
        "cover_url": f"https://images.sympla.com.br/{n}.jpg",
    }
    data.update(overrides)
    return StandardizedEvent(**data)


def _etl_db(stored: dict[str, dict[str, Any]] | None = None) -> MagicMock:
    db = MagicMock()
    db.upsert_events = AsyncMock(side_effect=lambda rows: len(rows))
    db.list_source_state = AsyncMock(return_value=stored or {})
    db.block_imported_event = AsyncMock()
    return db


def _hash(ev: StandardizedEvent) -> str:
    return event_moderation_hash(ev.title, ev.description, ev.cover_url)


async def _load(db: MagicMock, events: list[StandardizedEvent]) -> Any:
    with patch("app.services.etl.load.ai_service.generate_embeddings", AsyncMock(side_effect=lambda t: [VEC] * len(t))):
        return await upsert_events(db, events)


def _rows(db: MagicMock) -> dict[str, dict[str, Any]]:
    return {r["source_event_id"]: r for c in db.upsert_events.await_args_list for r in c.args[0]}


async def test_refused_new_items_never_enter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "moderate_inputs", _says({"Show 2": ["sexual"]}))
    db = _etl_db()

    result = await _load(db, [_imported(1), _imported(2)])

    assert set(_rows(db)) == {"s1"}
    assert result.blocked == 1
    db.block_imported_event.assert_not_awaited()


async def test_an_existing_row_whose_new_text_is_refused_is_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "moderate_inputs", _says({"Show 1": ["hate"]}))
    db = _etl_db({"s1": {"moderation_hash": "old", "moderation_blocked": False}})

    await _load(db, [_imported(1)])

    assert _rows(db) == {}
    db.block_imported_event.assert_awaited_once_with("sympla", "s1", ["hate"])


async def test_unchanged_rows_are_not_checked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    moderate = AsyncMock()
    monkeypatch.setattr(ai_service, "moderate_inputs", moderate)
    ev = _imported(1)
    db = _etl_db({"s1": {"moderation_hash": _hash(ev)}})

    await _load(db, [ev])

    moderate.assert_not_awaited()
    # And the stored result is left alone.
    assert "moderation_hash" not in _rows(db)["s1"]


async def test_a_refused_or_unreadable_cover_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "moderate_inputs", _says({}, images=(False, ["violence"])))
    db = _etl_db()

    result = await _load(db, [_imported(1)])

    row = _rows(db)["s1"]
    assert row["cover_url"] is None
    assert row["moderation_hash"] == event_moderation_hash("Show 1", "Noite animada", None)
    assert result.covers_removed == 1


async def test_what_cannot_be_checked_waits_and_nothing_old_is_overwritten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "moderate_inputs", AsyncMock(side_effect=RuntimeError("down")))
    db = _etl_db({"s1": {"moderation_hash": "old"}})

    result = await _load(db, [_imported(1), _imported(2)])

    assert _rows(db) == {}
    assert result.pending == 2


async def test_moderated_and_unchanged_rows_go_in_separate_requests() -> None:
    same = _imported(1)
    db = _etl_db({"s1": {"moderation_hash": _hash(same)}})

    await _load(db, [same, _imported(2)])

    for call in db.upsert_events.await_args_list:
        assert len({frozenset(r) for r in call.args[0]}) == 1


async def test_checks_stop_at_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    async def slow_images(inputs: list[Any]) -> list[tuple[bool, list[str]]]:
        if isinstance(inputs[0], dict):
            await asyncio.sleep(5)
        return [(True, [])] * len(inputs)

    monkeypatch.setattr(ai_service, "moderate_inputs", slow_images)
    monkeypatch.setattr(moderation, "_MIN_CALL_SECONDS", 0.01)

    out = await check_imports([("texto", "https://x/a.jpg"), ("texto", None)], budget_seconds=0.2)

    assert out[0] is None
    assert out[1] == moderation.ImportCheck(True)


async def test_finished_text_checks_are_kept_when_time_runs_out(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    async def one_slow_chunk(inputs: list[Any]) -> list[tuple[bool, list[str]]]:
        if "lento" in inputs[0]:
            await asyncio.sleep(5)
        return [(True, [])] * len(inputs)

    monkeypatch.setattr(ai_service, "moderate_inputs", one_slow_chunk)
    items = [(f"rápido {i}", None) for i in range(32)] + [(f"lento {i}", None) for i in range(8)]

    out = await check_imports(items, budget_seconds=0.3)

    assert out[:32] == [moderation.ImportCheck(True)] * 32
    assert out[32:] == [None] * 8


async def test_a_moderation_that_times_out_on_its_own_is_recorded(
    monkeypatch: pytest.MonkeyPatch, recorded: list[dict[str, Any]]
) -> None:
    # ai_service gives up after MODERATION_TIMEOUT_SECONDS with a TimeoutError, well
    # before the import's own deadline: that is the moderation failing, not time running out.
    monkeypatch.setattr(ai_service, "moderate_inputs", AsyncMock(side_effect=TimeoutError()))

    out = await check_imports([(f"texto {i}", None) for i in range(40)], budget_seconds=10)

    assert out == [None] * 40
    assert [r["code"] for r in recorded] == ["moderation_api_error"]


async def test_an_unreadable_cover_is_dropped_and_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "moderate_inputs", _says({}, images=_status_error(400)))
    ev = _imported(1)
    db = _etl_db()

    await _load(db, [ev])

    row = _rows(db)["s1"]
    assert row["cover_url"] is None
    assert row["moderation_dropped_cover"] == ev.cover_url
    assert row["moderation_reasons"] == ["unreadable_cover"]


@pytest.mark.parametrize(("days_ago", "checked_again"), [(1, False), (8, True)])
async def test_an_unreadable_cover_is_tried_again_only_after_a_week(
    monkeypatch: pytest.MonkeyPatch, days_ago: int, checked_again: bool
) -> None:
    # OpenAI could not fetch it (a slow host, say): not every import, but not never.
    from datetime import datetime, timedelta, timezone

    moderate = AsyncMock(side_effect=_says({}))
    monkeypatch.setattr(ai_service, "moderate_inputs", moderate)
    ev = _imported(1)
    db = _etl_db(
        {
            "s1": {
                "moderation_hash": event_moderation_hash(ev.title, ev.description, None),
                "moderation_dropped_cover": ev.cover_url,
                "moderation_reasons": ["unreadable_cover"],
                "moderated_at": (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(),
            }
        }
    )

    await _load(db, [ev])

    assert moderate.await_count == (2 if checked_again else 0)
    assert _rows(db)["s1"]["cover_url"] == (ev.cover_url if checked_again else None)


async def test_a_refused_cover_keeps_its_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "moderate_inputs", _says({}, images=(False, ["violence"])))
    db = _etl_db()

    await _load(db, [_imported(1)])

    assert _rows(db)["s1"]["moderation_reasons"] == ["violence"]
    assert _rows(db)["s1"]["moderation_blocked"] is False


async def test_text_checks_run_side_by_side(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    async def slow(inputs: list[Any]) -> list[tuple[bool, list[str]]]:
        await asyncio.sleep(0.2)
        return [(True, [])] * len(inputs)

    monkeypatch.setattr(ai_service, "moderate_inputs", slow)

    # Four requests of 32 texts: one after another they would need 0.8 s.
    out = await check_imports([(f"texto {i}", None) for i in range(128)], budget_seconds=0.6)

    assert None not in out


async def test_a_dropped_cover_is_remembered_and_not_checked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "moderate_inputs", _says({}, images=(False, ["violence"])))
    ev = _imported(1)
    first = _etl_db()

    await _load(first, [ev])

    row = _rows(first)["s1"]
    assert row["cover_url"] is None
    assert row["moderation_dropped_cover"] == ev.cover_url

    moderate = AsyncMock()
    monkeypatch.setattr(ai_service, "moderate_inputs", moderate)
    again = _etl_db(
        {
            "s1": {
                "moderation_hash": row["moderation_hash"],
                "moderation_dropped_cover": ev.cover_url,
                "moderation_blocked": False,
            }
        }
    )

    result = await _load(again, [ev])

    moderate.assert_not_awaited()
    row = _rows(again)["s1"]
    assert row["cover_url"] is None
    assert "moderation_hash" not in row
    assert result.covers_removed == 0


async def test_a_new_cover_after_a_dropped_one_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    moderate = AsyncMock(side_effect=_says({}))
    monkeypatch.setattr(ai_service, "moderate_inputs", moderate)
    ev = _imported(1, cover_url="https://images.sympla.com.br/nova.jpg")
    db = _etl_db(
        {
            "s1": {
                "moderation_hash": event_moderation_hash(ev.title, ev.description, None),
                "moderation_dropped_cover": "https://images.sympla.com.br/1.jpg",
            }
        }
    )

    await _load(db, [ev])

    assert moderate.await_count == 2
    row = _rows(db)["s1"]
    assert row["cover_url"] == ev.cover_url
    assert row["moderation_dropped_cover"] is None


async def test_a_hidden_row_is_checked_again_and_comes_back_when_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # The source went back to the content that was allowed before the refused one.
    ev = _imported(1)
    db = _etl_db({"s1": {"moderation_hash": _hash(ev), "moderation_blocked": True}})

    await _load(db, [ev])

    assert _rows(db)["s1"]["moderation_blocked"] is False


async def test_a_hidden_row_refused_again_stays_as_it_was(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "moderate_inputs", _says({"Show 1": ["hate"]}))
    ev = _imported(1)
    db = _etl_db({"s1": {"moderation_hash": "refused", "moderation_blocked": True}})

    result = await _load(db, [ev])

    assert _rows(db) == {}
    db.block_imported_event.assert_not_awaited()
    assert result.blocked == 1


async def test_events_are_not_linked_to_hidden_places() -> None:
    client = MagicMock()
    client.get = AsyncMock(return_value=httpx.Response(200, json=[], request=httpx.Request("GET", "http://x")))

    await DBService(client).list_places_near(-8.1, -8.0, -34.9, -34.8)

    assert ("moderation_blocked", "is.false") in client.get.await_args.kwargs["params"]


async def test_the_import_reads_what_the_moderation_remembers() -> None:
    client = MagicMock()
    client.get = AsyncMock(return_value=httpx.Response(200, json=[], request=httpx.Request("GET", "http://x")))

    await DBService(client).list_source_state("sympla")

    select = client.get.await_args.kwargs["params"]["select"].split(",")
    assert {"moderation_hash", "moderation_blocked", "moderation_dropped_cover", "moderation_reasons", "moderated_at"} <= set(
        select
    )


async def test_the_import_removes_contacts_before_anything_is_stored() -> None:
    raw = [{"id": "e1"}]
    ev = _imported(1, description="Ingressos: (81) 99999-8888")
    source = MagicMock(name="sympla")
    source.name = "sympla"
    source.transform = MagicMock(return_value=ev)
    upsert = AsyncMock(return_value=MagicMock(upserted=1, blocked=0, pending=0, covers_removed=0))

    with (
        patch("app.services.etl.pipeline.fetch_dataset_items", AsyncMock(return_value=raw)),
        patch("app.services.etl.pipeline.upsert_events", upsert),
        patch("app.services.etl.pipeline.link_events_to_places", AsyncMock(return_value=0)),
    ):
        report = await pipeline.ingest_dataset(MagicMock(), MagicMock(), source, "ds-1", "tok")

    assert upsert.await_args.args[1][0].description == f"Ingressos: {CONTACT_REMOVED}"
    assert report.contacts_removed == 1


# ------------------------------------------------------- moderation pass --


async def test_the_pass_redacts_imports_and_checks_app_rows_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "moderate_inputs", _says({"Briga": ["violence"]}))
    db = MagicMock()
    db.list_stale_moderation_events = AsyncMock(
        return_value=[
            {"id": "a", "source": "sympla", "title": "Show", "description": "Zap 81 99999-8888", "cover_url": "https://c/1.jpg"},
            {"id": "b", "source": None, "title": "Festa", "description": "Zap 81 99999-8888", "cover_url": None},
            {"id": "c", "source": "sympla", "title": "Briga", "description": None, "cover_url": None},
        ]
    )
    db.set_event_moderation = AsyncMock()
    db.get_event_media_rows = AsyncMock(return_value=[])
    db.get_trophy_template_by_event = AsyncMock(return_value=None)

    result = await moderate_stale(db, limit=50, budget_seconds=5)

    written = {c.args[0]: c.args[1] for c in db.set_event_moderation.await_args_list}
    assert written["a"]["description"] == f"Zap {CONTACT_REMOVED}"
    assert written["a"]["moderation_hash"] == event_moderation_hash("Show", f"Zap {CONTACT_REMOVED}", "https://c/1.jpg")
    # The creator's own text is never rewritten.
    assert "description" not in written["b"]
    assert written["c"]["moderation_blocked"] is True
    assert written["c"]["moderation_reasons"] == ["violence"]
    assert result == PassResult(picked=3, checked=3, blocked=1, covers_removed=0, contacts_removed=1, not_checked=0)


def _app_rows_db(rows: list[dict[str, Any]], media: dict[str, list[dict[str, Any]]], trophies: dict[str, Any]) -> MagicMock:
    db = MagicMock()
    db.list_stale_moderation_events = AsyncMock(return_value=rows)
    db.get_event_media_rows = AsyncMock(side_effect=lambda event_id: media.get(event_id, []))
    db.get_trophy_template_by_event = AsyncMock(side_effect=lambda event_id: trophies.get(event_id))
    db.set_event_moderation = AsyncMock()
    return db


def _app_row(event_id: str, title: str, location: str = "Praça do Arsenal") -> dict[str, Any]:
    return {"id": event_id, "source": None, "title": title, "description": None, "location_name": location, "cover_url": None}


async def test_the_pass_checks_everything_an_app_event_shows(monkeypatch: pytest.MonkeyPatch) -> None:
    # Events created in the app before #22 A: their place, trophy and photos too,
    # as saving them today would.
    monkeypatch.setattr(ai_service, "moderate_inputs", _says({"Ódio FC": ["hate"], "Beco da briga": ["violence"]}))
    seen: list[list[str]] = []

    async def paths(_db: Any, items: list[str], **_: Any) -> list[Verdict]:
        seen.append(list(items))
        return [Verdict(False, ("sexual",)) if p == "user-1/foto.jpg" else Verdict(True) for p in items]

    monkeypatch.setattr(moderation, "path_verdicts", paths)
    db = _app_rows_db(
        [_app_row("trophy", "Festa"), _app_row("place", "Sarau", "Beco da briga"), _app_row("photo", "Feira"), _app_row("ok", "Aula")],
        media={"photo": [{"media_url": "user-1/foto.jpg", "type": "image"}, {"media_url": "user-1/v.mp4", "type": "video"}]},
        trophies={
            "trophy": {"name": "Ódio FC", "description": None, "icon_url": None},
            "ok": {"name": "Aluno", "description": None, "icon_url": "user-1/icone.png"},
        },
    )

    result = await moderate_stale(db, limit=50, budget_seconds=5)

    written = {c.args[0]: c.args[1] for c in db.set_event_moderation.await_args_list}
    assert written["trophy"]["moderation_reasons"] == ["hate"]
    assert written["place"]["moderation_reasons"] == ["violence"]
    assert written["photo"]["moderation_reasons"] == ["sexual"]
    assert written["ok"]["moderation_blocked"] is False
    assert all(written[k]["moderation_blocked"] for k in ("trophy", "place", "photo"))
    # Videos cannot be checked; the icon is.
    assert sorted(seen) == [["user-1/foto.jpg"], ["user-1/icone.png"]]
    assert result.blocked == 3 and result.checked == 4


async def test_an_app_event_whose_photos_cannot_be_checked_stays_unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(moderation, "path_verdicts", AsyncMock(side_effect=moderation.ModerationUnavailable("down")))
    db = _app_rows_db([_app_row("photo", "Feira")], media={"photo": [{"media_url": "user-1/foto.jpg", "type": "image"}]}, trophies={})

    result = await moderate_stale(db, limit=50, budget_seconds=5)

    db.set_event_moderation.assert_not_awaited()
    assert result.not_checked == 1 and result.checked == 0


async def test_app_rows_checked_in_time_are_kept_when_the_budget_runs_out(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    async def paths(_db: Any, items: list[str], **_: Any) -> list[Verdict]:
        if any("lenta" in p for p in items):
            await asyncio.sleep(5)
        return [Verdict(True)] * len(items)

    monkeypatch.setattr(moderation, "path_verdicts", paths)
    monkeypatch.setattr(moderation, "_MIN_CALL_SECONDS", 0.01)
    rows = [_app_row(f"r{i}", f"Evento {i}") for i in range(6)]
    media = {f"r{i}": [{"media_url": f"user-1/{'lenta' if i >= 4 else 'foto'}{i}.jpg", "type": "image"}] for i in range(6)}
    db = _app_rows_db(rows, media=media, trophies={})

    result = await moderate_stale(db, limit=50, budget_seconds=0.3)

    written = {c.args[0] for c in db.set_event_moderation.await_args_list}
    assert written == {"r0", "r1", "r2", "r3"}
    assert result.checked == 4 and result.not_checked == 2


async def test_an_app_photo_that_no_longer_exists_hides_the_event_instead_of_stalling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Storage signs what exists and leaves the rest out: such a row would otherwise
    # stay stale forever and stop the owner's script before the keywords.
    monkeypatch.setattr(moderation, "path_verdicts", real_path_verdicts)
    monkeypatch.setattr(moderation, "image_verdicts", AsyncMock(return_value=[Verdict(True)]))
    db = _app_rows_db(
        [_app_row("photo", "Feira")],
        media={"photo": [{"media_url": "user-1/existe.jpg", "type": "image"}, {"media_url": "string", "type": "image"}]},
        trophies={},
    )
    db.sign_media_paths = AsyncMock(return_value={"user-1/existe.jpg": "https://signed/existe"})

    await moderate_stale(db, limit=50, budget_seconds=5)

    fields = db.set_event_moderation.await_args.args[1]
    assert fields["moderation_blocked"] is True
    assert fields["moderation_reasons"] == ["unreadable_image"]


async def test_when_storage_cannot_sign_at_all_the_app_event_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(moderation, "path_verdicts", real_path_verdicts)
    db = _app_rows_db([_app_row("photo", "Feira")], media={"photo": [{"media_url": "user-1/a.jpg", "type": "image"}]}, trophies={})
    db.sign_media_paths = AsyncMock(side_effect=httpx.ConnectError("down"))

    result = await moderate_stale(db, limit=50, budget_seconds=5)

    db.set_event_moderation.assert_not_awaited()
    assert result.not_checked == 1


async def test_an_unreadable_photo_hides_an_app_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(moderation, "path_verdicts", AsyncMock(return_value=[Verdict(False, unreadable=True)]))
    db = _app_rows_db([_app_row("photo", "Feira")], media={"photo": [{"media_url": "https://velho/foto.jpg", "type": "image"}]}, trophies={})

    await moderate_stale(db, limit=50, budget_seconds=5)

    fields = db.set_event_moderation.await_args.args[1]
    assert fields["moderation_blocked"] is True
    assert fields["moderation_reasons"] == ["unreadable_image"]
    assert reasons(fields["moderation_reasons"]) == "imagem que não conseguimos verificar"


async def test_the_pass_reads_the_place_of_each_row() -> None:
    client = MagicMock()
    client.get = AsyncMock(return_value=httpx.Response(200, json=[], request=httpx.Request("GET", "http://x")))

    await DBService(client).list_stale_moderation_events(limit=10)

    assert "location_name" in client.get.await_args.kwargs["params"]["select"].split(",")


# ------------------------------------------------- hidden rows stay hidden --


async def test_my_events_leave_out_hidden_events_i_am_going_to() -> None:
    client = MagicMock()
    client.get = AsyncMock(return_value=httpx.Response(200, json=[], request=httpx.Request("GET", "http://x")))

    await DBService(client).get_events_going("user-1", "jwt")

    assert client.get.await_args.kwargs["params"]["events.moderation_blocked"] == "is.false"


def _hidden(creator: str | None = None) -> dict[str, Any]:
    return {
        "id": "evt-1",
        "kind": "event",
        "source": None if creator else "sympla",
        "creator_id": creator,
        "title": "Show",
        "lat": -8.06,
        "long": -34.87,
        "moderation_blocked": True,
    }


def test_its_creator_still_sees_a_hidden_event(api: MagicMock) -> None:
    api.get_event_by_id = AsyncMock(return_value=_hidden(creator="user-1"))
    api.sweep_expired_calls = AsyncMock()
    api.count_rsvps_by_status = AsyncMock(return_value=0)
    api.get_rsvp = AsyncMock(return_value=None)

    response = TestClient(app).get("/events/evt-1")

    assert response.status_code == 200
    assert response.json()["moderation_blocked"] is True


def test_someone_else_does_not_see_a_hidden_app_event(api: MagicMock) -> None:
    api.get_event_by_id = AsyncMock(return_value=_hidden(creator="user-2"))

    assert TestClient(app).get("/events/evt-1").status_code == 404


def test_nobody_joins_a_hidden_event(api: MagicMock) -> None:
    api.get_event_by_id = AsyncMock(return_value=_hidden(creator="user-2"))
    api.rsvp_join = AsyncMock()

    assert TestClient(app).post("/events/evt-1/rsvp").status_code == 404
    api.rsvp_join.assert_not_called()


def test_a_hidden_event_gives_no_ticket(api: MagicMock) -> None:
    api.get_event_by_id = AsyncMock(return_value=_hidden(creator="user-2"))
    api.get_rsvp = AsyncMock(return_value={"id": "r1", "status": "confirmed"})

    assert TestClient(app).post("/events/evt-1/rsvp/qr", json={}).status_code == 404


def test_a_hidden_place_gives_no_trophy(api: MagicMock) -> None:
    api.get_event_by_id = AsyncMock(return_value={**_hidden(), "kind": "place", "source": "google_maps"})
    api.insert_trophy = AsyncMock()

    response = TestClient(app).post("/trophies/checkin", json={"event_id": "evt-1", "lat": -8.06, "long": -34.87})

    assert response.status_code == 404
    api.insert_trophy.assert_not_called()
    assert "moderation_blocked" in api.get_event_by_id.await_args.kwargs["select"].split(",")


@pytest.fixture
def admin(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    monkeypatch.setattr(settings, "ADMIN_INGEST_TOKEN", ADMIN)
    limiter.reset()
    db = MagicMock()
    db.list_stale_moderation_events = AsyncMock(return_value=[{"id": "a"}, {"id": "b"}])
    app.dependency_overrides[get_db_service] = lambda: db
    yield db
    app.dependency_overrides.clear()


def test_the_pass_needs_the_admin_token(admin: MagicMock) -> None:
    assert TestClient(app).post("/admin/events/moderation", json={}).status_code == 401


def test_a_dry_run_only_counts(admin: MagicMock) -> None:
    with patch("app.api.admin.moderate_stale", AsyncMock()) as run:
        body = TestClient(app).post(
            "/admin/events/moderation", json={"dry_run": True}, headers={"X-Admin-Token": ADMIN}
        ).json()

    assert (body["pending"], body["remaining"]) == (2, 2)
    run.assert_not_awaited()


def test_a_run_checks_a_chunk(admin: MagicMock) -> None:
    done = AsyncMock(return_value=PassResult(picked=2, checked=2, blocked=1))

    with patch("app.api.admin.moderate_stale", done):
        body = TestClient(app).post("/admin/events/moderation", headers={"X-Admin-Token": ADMIN}).json()

    assert (body["checked"], body["blocked"], body["remaining"]) == (2, 1, 0)
    assert done.await_args.kwargs["limit"] == 100


def test_the_webhook_budget_still_fits() -> None:
    # Fetch, moderation of what changed, embeddings, upsert, link and keywords.
    assert settings.IMPORT_MODERATION_BUDGET_SECONDS < pipeline._WEBHOOK_SECONDS
    assert SOURCES["google_maps"].name == "google_maps"
