"""Onboarding pipeline orchestration (mocked IO).

The person's vector is made from the taste agent's keywords and summary (#26 part 2,
decision 2026-10-18); with no answer analysed it falls back to the raw answers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.data.onboarding_guide import GUIDE_STEP_KEYS
from app.services.onboarding_guard import GuardResult
from app.services.onboarding_pipeline import run_complete_onboarding_pipeline
from app.services.taste_agent import (
    AnswerAnalysis,
    PersonTaste,
    StepQA,
    StepTaste,
    TasteKeywords,
    WeightedKeyword,
)


def _answers() -> dict[str, str]:
    return {k: f"Resposta elaborada para {k} com mais de quarenta caracteres mínimos." for k in GUIDE_STEP_KEYS}


def _raw_person() -> PersonTaste:
    """No answer analysed: the agent failed for all of them."""
    steps = [StepTaste(StepQA(k, "P?", f"resposta {k}"), None) for k in GUIDE_STEP_KEYS]
    return PersonTaste(steps, None, "raw")


def _analysed_person() -> PersonTaste:
    steps = [
        StepTaste(
            StepQA(k, "P?", f"resposta {k}"),
            AnswerAnalysis(keywords=[WeightedKeyword(term=f"gosto {k}", weight=2)], summary=f"Resumo {k}."),
        )
        for k in GUIDE_STEP_KEYS[:-1]
    ]
    # One answer the analyst could not read: still indexed, as the raw answer.
    steps.append(StepTaste(StepQA(GUIDE_STEP_KEYS[-1], "P?", "cedo"), None))
    taste = TasteKeywords(("literatura clássica", "música brasileira", "debates"), "Gosta de clássicos.")
    return PersonTaste(steps, taste, "synthesized")


def _db() -> MagicMock:
    db = MagicMock()
    db.get_or_create_profile = AsyncMock(return_value={"id": "u1"})
    db.upsert_user_taste_documents = AsyncMock(return_value=len(GUIDE_STEP_KEYS))
    db.update_profile = AsyncMock()
    return db


async def _run(db: MagicMock, person: PersonTaste, one: AsyncMock, many: AsyncMock) -> None:
    with (
        patch("app.services.onboarding_pipeline.run_onboarding_guard", AsyncMock(return_value=GuardResult(ok=True))),
        patch("app.services.onboarding_pipeline.analyze_person", AsyncMock(return_value=person)),
        patch("app.services.onboarding_pipeline.ai_service.generate_embedding", one),
        patch("app.services.onboarding_pipeline.ai_service.generate_embeddings", many),
    ):
        await run_complete_onboarding_pipeline("u1", "jwt", db, _answers())


async def test_the_vector_is_made_from_the_keywords_and_the_summary() -> None:
    db = _db()
    one = AsyncMock(return_value=[0.01] * 1536)
    many = AsyncMock(return_value=[[0.5] * 1536] * len(GUIDE_STEP_KEYS))

    await _run(db, _analysed_person(), one, many)

    text = one.await_args.args[0]
    assert text.startswith("Palavras-chave: literatura clássica, música brasileira, debates.")
    assert "Resumo: Gosta de clássicos." in text
    # Decision 2026-10-18: none of the raw answers go into the person's vector.
    assert "resposta" not in text
    payload = db.update_profile.await_args.args[1]
    assert payload["interest_embedding"] == [0.01] * 1536
    assert payload["interest_keywords"] == ["literatura clássica", "música brasileira", "debates"]
    assert payload["interest_summary"] == "Gosta de clássicos."
    assert payload["onboarding_preferences_hash"]


async def test_every_answer_is_indexed_with_its_analysis() -> None:
    db = _db()
    many = AsyncMock(return_value=[[0.5] * 1536] * len(GUIDE_STEP_KEYS))

    await _run(db, _analysed_person(), AsyncMock(return_value=[0.01] * 1536), many)

    items = db.upsert_user_taste_documents.await_args.args[1]
    assert {it["step_key"] for it in items} == set(GUIDE_STEP_KEYS)
    # A PostgREST bulk upsert takes its columns from the rows: all the same keys.
    assert len({tuple(sorted(it)) for it in items}) == 1
    by_key = {it["step_key"]: it for it in items}
    assert by_key[GUIDE_STEP_KEYS[0]]["extraction"] == "llm"
    assert by_key[GUIDE_STEP_KEYS[0]]["analysis"]["summary"] == f"Resumo {GUIDE_STEP_KEYS[0]}."
    assert by_key[GUIDE_STEP_KEYS[-1]]["extraction"] == "short"
    assert by_key[GUIDE_STEP_KEYS[-1]]["analysis"] is None
    # The chat's documents keep what the person said.
    assert "Resposta: cedo" in by_key[GUIDE_STEP_KEYS[-1]]["content"]
    assert all(it["embedding"] for it in items)


async def test_with_nothing_analysed_the_raw_answers_make_the_vector() -> None:
    db = _db()
    one = AsyncMock(return_value=[0.02] * 1536)

    await _run(db, _raw_person(), one, AsyncMock(return_value=[[0.5] * 1536] * len(GUIDE_STEP_KEYS)))

    assert f"resposta {GUIDE_STEP_KEYS[0]}" in one.await_args.args[0]
    payload = db.update_profile.await_args.args[1]
    # Cleared, so keywords from an earlier onboarding never sit next to this vector.
    assert payload["interest_keywords"] is None
    assert payload["interest_summary"] is None
    assert "onboarding_preferences_text" in payload


async def test_taste_upsert_failure_returns_500_before_any_profile_write() -> None:
    db = _db()
    db.upsert_user_taste_documents = AsyncMock(side_effect=RuntimeError("supabase down"))

    with pytest.raises(HTTPException) as ei:
        await _run(
            db,
            _analysed_person(),
            AsyncMock(return_value=[0.02] * 1536),
            AsyncMock(return_value=[[0.3] * 1536] * len(GUIDE_STEP_KEYS)),
        )

    assert ei.value.status_code == 500
    # Taste vectors are written before the profile, so a failure leaves NO partial DB state.
    db.update_profile.assert_not_awaited()


async def test_guard_moderation_unavailable_is_503() -> None:
    db = MagicMock()
    db.get_or_create_profile = AsyncMock(return_value={"id": "u1"})
    with patch(
        "app.services.onboarding_pipeline.run_onboarding_guard",
        new_callable=AsyncMock,
        return_value=GuardResult(ok=False, block_code="moderation_unavailable", detail_codes=["x"]),
    ):
        with pytest.raises(HTTPException) as ei:
            await run_complete_onboarding_pipeline("u1", "jwt", db, _answers())
    assert ei.value.status_code == 503
