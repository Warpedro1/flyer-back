"""Onboarding validate/complete orchestration (guard → taste agent → embeddings → taste docs → profile).

Each step has its own time limit (#26); together they fit a 60 s Vercel function
(``ONBOARDING_BUDGET_SECONDS``). The person's vector is made from the keywords and
the summary the taste agent writes (#26 part 2, #31); only when no answer could be
analysed does it fall back to the raw answers.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from fastapi import HTTPException, status

from app.core.config import settings
from app.core.errors import ApiError
from app.data.onboarding_guide import (
    GUIDE_SCHEMA_VERSION,
    build_guard_corpus,
    build_preferences_markdown,
)
from app.models.schemas import CompleteOnboardingOut
from app.services.ai_service import ai_service
from app.services.db_service import DBService
from app.services.onboarding_contract import (
    ordered_qa_tuples,
    validate_onboarding_answers,
)
from app.services.onboarding_guard import GuardResult, run_onboarding_guard
from app.services.taste_agent import (
    StepQA,
    TasteKeywords,
    analyze_person,
    build_canonical_taste_corpus,
    person_embedding_text,
    step_documents,
)

logger = logging.getLogger(__name__)

PROFILE_WRITE_FAILED = "profile_write_failed"
PROFILE_WRITE_FAILED_MESSAGE = "Não foi possível salvar seu perfil. Tente de novo em instantes."


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

    try:
        await db.get_or_create_profile(user_id, jwt)
    except Exception:
        logger.exception("Onboarding profile read failed")
        raise ApiError(500, PROFILE_WRITE_FAILED, PROFILE_WRITE_FAILED_MESSAGE) from None

    corpus = build_guard_corpus(answers)
    gr = await run_onboarding_guard(corpus)
    if not gr.ok:
        raise _map_guard_failure_to_http(gr)

    qas = [StepQA(k, p, a) for k, p, a in ordered_qa_tuples(answers)]
    person = await analyze_person(qas)
    documents = step_documents(person.steps)
    canonical = build_canonical_taste_corpus(documents)
    prefs_md = build_preferences_markdown(answers)
    store_text = prefs_md if prefs_md.strip() else canonical[: settings.ONBOARDING_ANSWERS_TOTAL_MAX_CHARS]
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    # Decision 2026-10-18: keywords and summary only; the raw answers are the fallback.
    vector_text = person_embedding_text(person.taste) if person.taste else canonical
    logger.info(
        "Onboarding taste agent: %s (%d of %d answers analysed)",
        person.mode,
        sum(1 for s in person.steps if s.analysis is not None),
        len(person.steps),
    )

    # Embedding generation is the one step that depends on an external provider
    # (OpenAI). Surface its failure with a stable code instead of a generic 500,
    # so a missing/invalid OPENAI key doesn't silently leave the profile vector-less.
    try:
        timeout = settings.EMBEDDING_TIMEOUT_SECONDS
        embedding_vec, doc_vecs = await asyncio.gather(
            ai_service.generate_embedding(vector_text, timeout=timeout),
            ai_service.generate_embeddings([d.page_content for d in documents], timeout=timeout),
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Onboarding embedding generation failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "embedding_generation_failed"},
        ) from None
    taste_items = [
        {
            "user_id": user_id,
            "step_key": str(d.metadata.get("step_key", f"idx_{i}")),
            "content": d.page_content,
            "extraction": str(d.metadata.get("extraction", "llm")),
            # Every item carries the same keys: a PostgREST bulk upsert takes its
            # columns from the rows.
            "analysis": d.metadata.get("analysis"),
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
        logger.exception("User taste documents upsert failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "vector_store_write_failed"},
        ) from None

    # Single row update: embedding + preferences text/hash/version are committed
    # together, so we never persist an embedding without its matching prefs text.
    try:
        await db.update_profile(
            user_id,
            {
                "interest_embedding": embedding_vec,
                # Cleared when nothing was analysed, so old keywords never sit next
                # to a vector they did not make.
                "interest_keywords": list(person.taste.keywords) if person.taste else None,
                "interest_summary": person.taste.summary if person.taste else None,
                "onboarding_preferences_text": store_text,
                "onboarding_preferences_hash": digest,
                "guide_schema_version": GUIDE_SCHEMA_VERSION,
            },
            jwt,
        )
    except Exception:
        logger.exception("Onboarding profile write failed")
        raise ApiError(500, PROFILE_WRITE_FAILED, PROFILE_WRITE_FAILED_MESSAGE) from None

    return CompleteOnboardingOut(
        ok=True,
        steps_indexed=len(taste_items),
        vectorstore_skipped=False,
    )


async def ensure_interest_embedding(
    db: DBService,
    user_id: str,
    jwt: str,
    profile: dict[str, Any],
) -> list[float] | None:
    """Lazy backfill: rebuild `interest_embedding` when it is missing.

    Users who onboarded before the pipeline existed (or whose embedding write failed)
    keep their keywords or a valid `onboarding_preferences_text` but no vector, which
    made discovery hard-fail with 400. The keywords and summary come first, as in the
    onboarding (#31: they are stored so a vector can be rebuilt without the LLM); the
    raw preferences text is the fallback. The vector is persisted (service_role) and
    returned; None when there is nothing to rebuild from.
    """
    keywords = [str(k) for k in (profile.get("interest_keywords") or []) if str(k).strip()]
    if keywords:
        summary = str(profile.get("interest_summary") or "").strip()
        text = person_embedding_text(TasteKeywords(tuple(keywords), summary))
    else:
        prefs_raw = profile.get("onboarding_preferences_text")
        text = str(prefs_raw).strip() if prefs_raw else ""
    if not text:
        return None
    try:
        vec = await ai_service.generate_embedding(text)
    except Exception:
        logger.exception("Lazy interest-embedding rebuild failed")
        return None
    if not vec:
        return None
    await db.update_profile_embedding(user_id, vec)
    return vec
