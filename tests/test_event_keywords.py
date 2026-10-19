"""Event and place keywords (#22 part B, decisions 2026-10-18).

* Creating and editing: the taste agent's keywords make the vector; on failure the
  raw text does, as before, and the event stays stale.
* Import: rows whose text did not change keep a vector made from their keywords,
  without the LLM; the rest stay stale.
* Reprocessing: ``POST /admin/events/keywords`` estimates by default (the owner
  approves the cost first) and otherwise works through a chunk of stale rows.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_db_service
from app.core.config import settings
from app.core.limiter import limiter
from app.main import app
from app.services import event_keywords, taste_agent
from app.services.db_service import DBService
from app.services.etl import pipeline
from app.services.etl.load import LoadResult, upsert_events
from app.services.etl.schemas import StandardizedEvent
from app.services.etl.sources import SOURCES
from app.services.event_keywords import (
    ReprocessResult,
    embed_app_events,
    estimate_cost,
    reprocess_stale,
)
from app.services.taste_agent import TasteKeywords, TasteSynthesis, WeightedKeyword, event_content_hash

VEC = [0.1] * 1536
OLD = [0.9] * 1536
ADMIN = "admin-token-for-tests"


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "evt-1",
        "kind": "event",
        "title": "Forró de Lais Senna",
        "description": "Noite de forró pé de serra.",
        "category": "musica_show",
    }
    row.update(overrides)
    return row


def _db() -> MagicMock:
    db = MagicMock()
    db.set_events_keywords = AsyncMock()
    db.set_events_embedding = AsyncMock()
    return db


def _agent(monkeypatch: pytest.MonkeyPatch, *terms: str) -> list[Any]:
    """The event call answers these keywords; returns the calls it got."""
    calls: list[Any] = []

    class _R:
        async def ainvoke(self, messages: Any) -> TasteSynthesis:
            calls.append(messages)
            return TasteSynthesis(keywords=[WeightedKeyword(term=t) for t in terms], summary="Noite de forró.")

    monkeypatch.setattr(taste_agent, "_structured", lambda *_: _R())
    return calls


# ------------------------------------------------------ created in the app --


async def test_a_new_event_gets_keywords_and_a_vector_made_from_them(monkeypatch: pytest.MonkeyPatch) -> None:
    _agent(monkeypatch, "forró", "dança")
    db = _db()
    embed = AsyncMock(return_value=VEC)

    with patch.object(event_keywords.ai_service, "generate_embedding", embed):
        await embed_app_events(db, ["evt-1", "evt-2"], _row())

    text = embed.await_args.args[0]
    assert text == "Forró de Lais Senna\nCategoria: Música/Show\nPalavras-chave: forró, dança.\nResumo: Noite de forró."
    assert embed.await_args.kwargs["timeout"] == settings.EMBEDDING_TIMEOUT_SECONDS
    # One call per series: every occurrence shares the text.
    db.set_events_keywords.assert_awaited_once_with(
        ["evt-1", "evt-2"],
        keywords=["forró", "dança"],
        summary="Noite de forró.",
        keywords_hash=event_content_hash("Forró de Lais Senna", "Noite de forró pé de serra.", "musica_show"),
        vector=VEC,
    )
    db.set_events_embedding.assert_not_awaited()


async def test_without_the_agent_the_raw_text_still_makes_a_vector() -> None:
    # The autouse fixture keeps the LLM off: the agent fails.
    db = _db()
    embed = AsyncMock(return_value=VEC)

    with patch.object(event_keywords.ai_service, "generate_embedding", embed):
        await embed_app_events(db, ["evt-1"], _row())

    assert embed.await_args.args[0] == "Forró de Lais Senna Noite de forró pé de serra. musica_show"
    db.set_events_embedding.assert_awaited_once_with(["evt-1"], VEC)
    # No keywords written: the event stays stale for the reprocessing.
    db.set_events_keywords.assert_not_awaited()


async def test_an_edit_that_kept_the_text_redoes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _agent(monkeypatch, "forró")
    row = _row()
    previous = {**row, "keywords": ["forró"], "keywords_summary": "x", "keywords_hash": event_content_hash(
        row["title"], row["description"], row["category"]), "event_embedding": OLD}
    db = _db()
    embed = AsyncMock(return_value=VEC)

    with patch.object(event_keywords.ai_service, "generate_embedding", embed):
        await embed_app_events(db, ["evt-1"], row, previous=previous)

    assert calls == []
    embed.assert_not_awaited()
    db.set_events_keywords.assert_not_awaited()


async def test_fresh_keywords_without_a_vector_rebuild_it_without_the_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _agent(monkeypatch, "outra")
    row = _row()
    digest = event_content_hash(row["title"], row["description"], row["category"])
    previous = {**row, "keywords": ["forró", "dança"], "keywords_summary": "Noite.", "keywords_hash": digest, "event_embedding": None}
    db = _db()

    with patch.object(event_keywords.ai_service, "generate_embedding", AsyncMock(return_value=VEC)):
        await embed_app_events(db, ["evt-1"], row, previous=previous)

    assert calls == []
    assert db.set_events_keywords.await_args.kwargs["keywords"] == ["forró", "dança"]


async def test_reverting_a_failed_edit_rebuilds_the_vector_from_the_old_keywords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Edit 1 changed the text while the agent was down: the raw vector of that text
    # was written and the keywords (of the original text) stayed. Edit 2 reverts.
    calls = _agent(monkeypatch, "outra")
    original = _row()
    digest = event_content_hash(original["title"], original["description"], original["category"])
    previous = {**original, "description": "Texto da edição 1", "keywords": ["forró"], "keywords_summary": "",
                "keywords_hash": digest, "event_embedding": OLD}
    db = _db()
    embed = AsyncMock(return_value=VEC)

    with patch.object(event_keywords.ai_service, "generate_embedding", embed):
        await embed_app_events(db, ["evt-1"], original, previous=previous)

    assert calls == []
    assert "Palavras-chave: forró." in embed.await_args.args[0]
    assert db.set_events_keywords.await_args.kwargs["vector"] == VEC


async def test_an_edit_that_changed_the_text_asks_the_agent_again(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _agent(monkeypatch, "frevo")
    previous = {**_row(), "keywords": ["forró"], "keywords_hash": "old", "event_embedding": OLD}
    db = _db()

    with patch.object(event_keywords.ai_service, "generate_embedding", AsyncMock(return_value=VEC)):
        await embed_app_events(db, ["evt-1"], _row(description="Agora é frevo."), previous=previous)

    assert len(calls) == 1
    assert db.set_events_keywords.await_args.kwargs["keywords"] == ["frevo"]


async def test_keywords_are_never_written_without_their_vector(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _agent(monkeypatch, "forró")
    db = _db()

    with patch.object(event_keywords.ai_service, "generate_embedding", AsyncMock(side_effect=RuntimeError("down"))):
        await embed_app_events(db, ["evt-1"], _row())

    db.set_events_keywords.assert_not_awaited()
    assert any("evt-1" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")


# ------------------------------------------------------------ reprocessing --


def test_the_estimate_counts_every_row() -> None:
    rows = [_row(description="x" * 2000), _row(description=None)] * 50

    cost = estimate_cost(rows)

    assert cost.items == 100
    assert cost.input_tokens > cost.output_tokens > 0
    assert Decimal("0") < cost.usd < Decimal("1")


async def test_reprocessing_writes_each_row_with_its_own_keywords(monkeypatch: pytest.MonkeyPatch) -> None:
    async def tastes(items: Any, *, budget_seconds: float) -> list[TasteKeywords | None]:
        assert budget_seconds == 12.0
        return [TasteKeywords((f"k{i}",), "") if i != 1 else None for i in range(len(items))]

    monkeypatch.setattr(event_keywords, "analyze_events", tastes)
    rows = [_row(id=f"evt-{i}", title=f"Show {i}") for i in range(3)]
    db = _db()
    db.list_stale_keyword_events = AsyncMock(return_value=rows)
    embed = AsyncMock(side_effect=lambda texts, **_: [VEC] * len(texts))

    with patch.object(event_keywords.ai_service, "generate_embeddings", embed):
        result = await reprocess_stale(db, limit=50, budget_seconds=12.0)

    assert result == ReprocessResult(picked=3, processed=2, failed=1)
    assert db.list_stale_keyword_events.await_args.kwargs == {"limit": 50}
    written = {c.args[0][0]: c.kwargs for c in db.set_events_keywords.await_args_list}
    assert set(written) == {"evt-0", "evt-2"}
    assert written["evt-2"]["keywords"] == ["k2"]
    assert written["evt-2"]["keywords_hash"] == event_content_hash("Show 2", rows[2]["description"], "musica_show")


async def test_a_failed_embedding_batch_leaves_the_rows_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    async def tastes(items: Any, **_: Any) -> list[TasteKeywords]:
        return [TasteKeywords(("k",), "")] * len(items)

    monkeypatch.setattr(event_keywords, "analyze_events", tastes)
    db = _db()
    db.list_stale_keyword_events = AsyncMock(return_value=[_row()])

    with patch.object(event_keywords.ai_service, "generate_embeddings", AsyncMock(side_effect=RuntimeError("down"))):
        result = await reprocess_stale(db, limit=10, budget_seconds=5)

    assert result.processed == 0
    db.set_events_keywords.assert_not_awaited()


async def test_nothing_stale_means_nothing_to_do() -> None:
    db = _db()
    db.list_stale_keyword_events = AsyncMock(return_value=[])

    assert await reprocess_stale(db, limit=10, budget_seconds=5) == ReprocessResult(0, 0, 0)


@pytest.fixture
def admin(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    monkeypatch.setattr(settings, "ADMIN_INGEST_TOKEN", ADMIN)
    limiter.reset()
    db = _db()
    db.list_stale_keyword_events = AsyncMock(return_value=[_row(), _row(id="evt-2")])
    app.dependency_overrides[get_db_service] = lambda: db
    yield db
    app.dependency_overrides.clear()


def test_the_reprocessing_needs_the_admin_token(admin: MagicMock) -> None:
    assert TestClient(app).post("/admin/events/keywords", json={}).status_code == 401


def test_by_default_it_only_estimates(admin: MagicMock) -> None:
    with patch("app.api.admin.reprocess_stale", AsyncMock()) as run:
        response = TestClient(app).post("/admin/events/keywords", headers={"X-Admin-Token": ADMIN})

    assert response.status_code == 200
    body = response.json()
    assert (body["dry_run"], body["pending"], body["remaining"], body["processed"]) == (True, 2, 2, 0)
    assert body["estimate"]["items"] == 2
    run.assert_not_awaited()


def test_a_real_run_processes_a_chunk(admin: MagicMock) -> None:
    done = AsyncMock(return_value=ReprocessResult(picked=2, processed=1, failed=1))

    with patch("app.api.admin.reprocess_stale", done):
        response = TestClient(app).post(
            "/admin/events/keywords", json={"dry_run": False, "limit": 40}, headers={"X-Admin-Token": ADMIN}
        )

    body = response.json()
    assert (body["processed"], body["failed"], body["remaining"]) == (1, 1, 1)
    assert done.await_args.kwargs == {"limit": 40, "budget_seconds": settings.KEYWORDS_BUDGET_SECONDS}


# ------------------------------------------------------------------ import --


def _imported(n: int, **overrides: Any) -> StandardizedEvent:
    data: dict[str, Any] = {
        "source": "sympla",
        "source_event_id": f"s{n}",
        "title": f"Show {n}",
        "description": "Noite animada",
        "category": "musica_show",
        "lat": -8.06,
        "long": -34.88,
    }
    data.update(overrides)
    return StandardizedEvent(**data)


async def test_a_reimport_reuses_fresh_keywords_and_leaves_the_rest_raw() -> None:
    same, changed = _imported(1), _imported(2, description="Texto novo")
    stored = {
        "s1": {"keywords": ["samba"], "keywords_summary": "Roda.", "keywords_hash": event_content_hash("Show 1", "Noite animada", "musica_show")},
        "s2": {"keywords": ["forró"], "keywords_summary": "", "keywords_hash": event_content_hash("Show 2", "Noite animada", "musica_show")},
    }
    db = MagicMock()
    db.upsert_events = AsyncMock(side_effect=lambda rows: len(rows))
    db.list_source_state = AsyncMock(return_value=stored)
    embed = AsyncMock(side_effect=lambda texts: [VEC] * len(texts))

    with patch("app.services.etl.load.ai_service.generate_embeddings", embed):
        await upsert_events(db, [same, changed])

    texts = embed.await_args.args[0]
    assert texts[0] == "Show 1\nCategoria: Música/Show\nPalavras-chave: samba.\nResumo: Roda."
    assert texts[1] == "Show 2 Texto novo musica_show"
    db.list_source_state.assert_awaited_once_with("sympla")
    # The upsert never carries the keyword columns: a re-import cannot wipe them.
    assert not any(k.startswith("keywords") for k in db.upsert_events.await_args.args[0][0])


async def test_without_the_stored_keywords_the_load_stops_before_overwriting() -> None:
    # Guessing "no keywords" would give fresh rows a raw vector the reprocessing
    # never sees (they are not stale).
    db = MagicMock()
    db.upsert_events = AsyncMock(side_effect=lambda rows: len(rows))
    db.list_source_state = AsyncMock(side_effect=RuntimeError("supabase down"))

    with (
        patch("app.services.etl.load.ai_service.generate_embeddings", AsyncMock(return_value=[VEC])),
        pytest.raises(RuntimeError),
    ):
        await upsert_events(db, [_imported(1)])

    db.upsert_events.assert_not_awaited()


async def test_the_webhook_gives_keywords_with_the_time_left(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "KEYWORDS_ON_IMPORT", True)
    raw = [{"title": "Parraxaxá", "placeId": "p1", "location": {"lat": -8.1, "lng": -34.9}}]
    done = AsyncMock(return_value=ReprocessResult(picked=5, processed=4, failed=1))

    with (
        patch("app.services.etl.pipeline.fetch_dataset_items", AsyncMock(return_value=raw)),
        patch("app.services.etl.pipeline.upsert_events", AsyncMock(return_value=LoadResult(upserted=1))),
        patch("app.services.etl.pipeline.link_events_to_places", AsyncMock(return_value=0)),
        patch("app.services.etl.pipeline.reprocess_stale", done),
    ):
        report = await pipeline.ingest_dataset(MagicMock(), MagicMock(), SOURCES["google_maps"], "ds-1", "tok")

    assert report.keywords == 4
    assert report.errors == []
    assert 0 < done.await_args.kwargs["budget_seconds"] <= settings.KEYWORDS_BUDGET_SECONDS
    assert done.await_args.kwargs["limit"] <= event_keywords.EMBED_BATCH


async def test_the_webhook_spends_nothing_on_keywords_until_the_owner_approves() -> None:
    # Decision 2026-10-18: the backlog's cost is approved first (KEYWORDS_ON_IMPORT).
    assert settings.KEYWORDS_ON_IMPORT is False
    done = AsyncMock()

    with (
        patch("app.services.etl.pipeline.fetch_dataset_items", AsyncMock(return_value=[])),
        patch("app.services.etl.pipeline.upsert_events", AsyncMock(return_value=LoadResult())),
        patch("app.services.etl.pipeline.link_events_to_places", AsyncMock(return_value=0)),
        patch("app.services.etl.pipeline.reprocess_stale", done),
    ):
        await pipeline.ingest_dataset(MagicMock(), MagicMock(), SOURCES["sympla"], "ds-1", "tok")

    done.assert_not_awaited()


async def test_a_slow_load_leaves_the_keywords_for_later(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "KEYWORDS_ON_IMPORT", True)
    monkeypatch.setattr(pipeline, "_WEBHOOK_SECONDS", 0.0)
    done = AsyncMock()

    with (
        patch("app.services.etl.pipeline.fetch_dataset_items", AsyncMock(return_value=[])),
        patch("app.services.etl.pipeline.upsert_events", AsyncMock(return_value=LoadResult())),
        patch("app.services.etl.pipeline.link_events_to_places", AsyncMock(return_value=0)),
        patch("app.services.etl.pipeline.reprocess_stale", done),
    ):
        report = await pipeline.ingest_dataset(MagicMock(), MagicMock(), SOURCES["sympla"], "ds-1", "tok")

    done.assert_not_awaited()
    assert report.keywords == 0


# --------------------------------------------------------------- database --


def _client(json_body: Any = None) -> MagicMock:
    client = MagicMock()
    response = httpx.Response(200, json=json_body if json_body is not None else [], request=httpx.Request("GET", "http://x"))
    client.patch = AsyncMock(return_value=response)
    client.get = AsyncMock(return_value=response)
    return client


async def test_keywords_and_their_vector_go_in_one_patch() -> None:
    client = _client()

    await DBService(client).set_events_keywords(
        ["evt-1", "evt-2"], keywords=["forró"], summary="Noite.", keywords_hash="h", vector=VEC
    )

    kwargs = client.patch.await_args.kwargs
    assert kwargs["params"] == {"id": "in.(evt-1,evt-2)"}
    assert kwargs["json"] == {"keywords": ["forró"], "keywords_summary": "Noite.", "keywords_hash": "h", "event_embedding": VEC}


async def test_the_stale_rows_are_read_without_their_vectors() -> None:
    client = _client([{"id": "evt-1"}])

    rows = await DBService(client).list_stale_keyword_events(limit=80)

    params = client.get.await_args.kwargs["params"]
    assert rows == [{"id": "evt-1"}]
    assert params["keywords_stale"] == "is.true"
    assert "embedding" not in params["select"]
    assert params["limit"] == "80"
    # Events first (few, and they pass), then the most reviewed places (decision
    # 2026-10-19); the id keeps the pages stable.
    assert params["order"] == "kind.asc,reviews_count.desc.nullslast,id.asc"


async def test_the_stored_keywords_of_a_source_are_read_page_by_page() -> None:
    client = MagicMock()
    page = [{"source_event_id": f"s{i}", "keywords_hash": "h"} for i in range(1000)]
    client.get = AsyncMock(
        side_effect=[
            httpx.Response(200, json=page, request=httpx.Request("GET", "http://x")),
            httpx.Response(200, json=[{"source_event_id": "last", "keywords_hash": "h"}], request=httpx.Request("GET", "http://x")),
        ]
    )

    stored = await DBService(client).list_source_state("google_maps")

    assert len(stored) == 1001
    assert [c.kwargs["params"]["offset"] for c in client.get.await_args_list] == ["0", "1000"]


def test_one_call_fits_a_function() -> None:
    # One embedding batch at most after the LLM stops, plus the error record (2 s)
    # and the writes: the webhook and the admin route end inside Vercel's 60 s.
    from app.api.admin import KeywordsRequest

    assert pipeline._KEYWORDS_PER_RUN <= event_keywords.EMBED_BATCH
    assert KeywordsRequest.model_fields["limit"].metadata[-1].le <= event_keywords.EMBED_BATCH
    assert pipeline._WEBHOOK_SECONDS + settings.EMBEDDING_TIMEOUT_SECONDS + 2 + 5 < 60
    assert settings.KEYWORDS_BUDGET_SECONDS + settings.EMBEDDING_TIMEOUT_SECONDS + 2 + 5 < 60
    assert asyncio.iscoroutinefunction(reprocess_stale)
