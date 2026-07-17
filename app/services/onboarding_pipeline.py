"""Onboarding validate/complete orchestration (guard → taste → embedding → profile → Chroma)."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from fastapi import HTTPException, status

from app.core.config import settings
from app.data.onboarding_guide import (
    GUIDE_SCHEMA_VERSION,
    build_guard_corpus,
    build_preferences_markdown,
)
from app.models.schemas import CompleteOnboardingOut
from app.services.ai_service import ai_service
from app.services.db_service import DBService
from app.services.onboarding_contract import ordered_qa_tuples, validate_onboarding_answers
from app.services.onboarding_guard import GuardResult, run_onboarding_guard
from app.services.taste_extraction import StepQA, build_canonical_taste_corpus, extract_tastes_per_step

logger = logging.getLogger(__name__)


def _map_guard_failure_to_http(gr: GuardResult) -> HTTPException:
    if gr.block_code == "moderation_unavailable":
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "moderation_unavailable", "reason_codes": gr.detail_codes or []},
        )
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "code": "onboarding_guard_rejected",
            "block_code": gr.block_code,
            "reason_codes": gr.detail_codes or [],
        },
    )


async def run_onboarding_guard_only(answers_raw: Any) -> None:
    """Same guard as complete; raises HTTPException on contract or guard failure."""
    validated = validate_onboarding_answers(answers_raw)
    corpus = build_guard_corpus(validated.answers)
    gr = await run_onboarding_guard(corpus)
    if not gr.ok:
        raise _map_guard_failure_to_http(gr)


async def run_complete_onboarding_pipeline(
    user_id: str,
    jwt: str,
    db: DBService,
    answers_raw: Any,
) -> CompleteOnboardingOut:
    validated = validate_onboarding_answers(answers_raw)
    answers = validated.answers

    await db.get_or_create_profile(user_id, jwt)

    corpus = build_guard_corpus(answers)
    gr = await run_onboarding_guard(corpus)
    if not gr.ok:
        raise _map_guard_failure_to_http(gr)

    qas = [StepQA(k, p, a) for k, p, a in ordered_qa_tuples(answers)]
    documents = await extract_tastes_per_step(qas)
    canonical = build_canonical_taste_corpus(documents)
    prefs_md = build_preferences_markdown(answers)
    store_text = prefs_md if prefs_md.strip() else canonical[: settings.ONBOARDING_ANSWERS_TOTAL_MAX_CHARS]
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    embedding_vec = await ai_service.generate_embedding(canonical)
    doc_vecs = await ai_service.generate_embeddings([d.page_content for d in documents])
    taste_items = [
        {
            "user_id": user_id,
            "step_key": str(d.metadata.get("step_key", f"idx_{i}")),
            "content": d.page_content,
            "extraction": str(d.metadata.get("extraction", "llm")),
            "guide_schema_version": GUIDE_SCHEMA_VERSION,
            "embedding": vec,
        }
        for i, (d, vec) in enumerate(zip(documents, doc_vecs))
    ]

    # Write the taste vectors BEFORE any profile mutation, so a vector-store failure
    # aborts with no partial DB state to reconcile on retry.
    try:
        await db.upsert_user_taste_documents(user_id, taste_items)
    except Exception:
        logger.exception("User taste documents upsert failed (user_id=%s)", user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "vector_store_write_failed"},
        ) from None

    # Single row update: embedding + preferences text/hash/version are committed
    # together, so we never persist an embedding without its matching prefs text.
    await db.update_profile(
        user_id,
        {
            "interest_embedding": embedding_vec,
            "onboarding_preferences_text": store_text,
            "onboarding_preferences_hash": digest,
            "guide_schema_version": GUIDE_SCHEMA_VERSION,
        },
        jwt,
    )

    return CompleteOnboardingOut(
        ok=True,
        steps_indexed=len(taste_items),
        vectorstore_skipped=False,
    )
