"""Onboarding pipeline orchestration (mocked IO)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from langchain_core.documents import Document

from app.data.onboarding_guide import GUIDE_STEP_KEYS
from app.services.onboarding_guard import GuardResult
from app.services.onboarding_pipeline import run_complete_onboarding_pipeline


def _answers() -> dict[str, str]:
    return {k: f"Resposta elaborada para {k} com mais de quarenta caracteres mínimos." for k in GUIDE_STEP_KEYS}


def _docs() -> list[Document]:
    return [
        Document(page_content=f"doc {k}", metadata={"step_key": k, "source": "onboarding", "extraction": "llm"})
        for k in GUIDE_STEP_KEYS
    ]


@pytest.mark.asyncio
async def test_complete_onboarding_persists_taste_docs_then_profile() -> None:
    docs = _docs()
    db = MagicMock()
    db.get_or_create_profile = AsyncMock(return_value={"id": "u1"})
    db.upsert_user_taste_documents = AsyncMock(return_value=len(docs))
    db.update_profile = AsyncMock()
    canonical_vec = [0.01] * 1536
    doc_vecs = [[0.5] * 1536 for _ in docs]
    with (
        patch("app.services.onboarding_pipeline.run_onboarding_guard", new_callable=AsyncMock) as g,
        patch(
            "app.services.onboarding_pipeline.extract_tastes_per_step",
            new_callable=AsyncMock,
            return_value=docs,
        ),
        patch(
            "app.services.onboarding_pipeline.ai_service.generate_embedding",
            new_callable=AsyncMock,
            return_value=canonical_vec,
        ),
        patch(
            "app.services.onboarding_pipeline.ai_service.generate_embeddings",
            new_callable=AsyncMock,
            return_value=doc_vecs,
        ),
    ):
        g.return_value = GuardResult(ok=True)
        out = await run_complete_onboarding_pipeline("u1", "jwt", db, _answers())

    assert out.ok is True
    assert out.steps_indexed == len(GUIDE_STEP_KEYS)
    # Taste docs upserted (one per step, each with an embedding).
    db.upsert_user_taste_documents.assert_awaited_once()
    items = db.upsert_user_taste_documents.await_args.args[1]
    assert len(items) == len(GUIDE_STEP_KEYS)
    assert all("embedding" in it and it["embedding"] for it in items)
    assert {it["step_key"] for it in items} == set(GUIDE_STEP_KEYS)
    # Embedding + prefs committed atomically via a single update_profile call.
    db.update_profile.assert_awaited_once()
    payload = db.update_profile.await_args.args[1]
    assert payload["interest_embedding"] == canonical_vec
    assert "onboarding_preferences_text" in payload


@pytest.mark.asyncio
async def test_taste_upsert_failure_returns_500_before_any_profile_write() -> None:
    docs = _docs()
    db = MagicMock()
    db.get_or_create_profile = AsyncMock(return_value={"id": "u1"})
    db.upsert_user_taste_documents = AsyncMock(side_effect=RuntimeError("supabase down"))
    db.update_profile = AsyncMock()
    with (
        patch("app.services.onboarding_pipeline.run_onboarding_guard", new_callable=AsyncMock) as g,
        patch(
            "app.services.onboarding_pipeline.extract_tastes_per_step",
            new_callable=AsyncMock,
            return_value=docs,
        ),
        patch(
            "app.services.onboarding_pipeline.ai_service.generate_embedding",
            new_callable=AsyncMock,
            return_value=[0.02] * 1536,
        ),
        patch(
            "app.services.onboarding_pipeline.ai_service.generate_embeddings",
            new_callable=AsyncMock,
            return_value=[[0.3] * 1536 for _ in docs],
        ),
    ):
        g.return_value = GuardResult(ok=True)
        with pytest.raises(HTTPException) as ei:
            await run_complete_onboarding_pipeline("u1", "jwt", db, _answers())
    assert ei.value.status_code == 500
    # Taste vectors are written before the profile, so a failure leaves NO partial DB state.
    db.update_profile.assert_not_awaited()


@pytest.mark.asyncio
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
