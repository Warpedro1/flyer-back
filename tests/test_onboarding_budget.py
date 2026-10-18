"""Finishing the onboarding again (#26 part 1).

* Moderation used ``text-moderation-latest``, which OpenAI shut down (2025-10-27):
  every onboarding ended in 503 ``moderation_unavailable``. The model is now a
  setting (``omni-moderation-latest``) and failures are recorded, not swallowed.
* Time: moderation, LLM judge, the taste agent's analysts and synthesizer (#31) and
  the embeddings must fit the 60 s of a Vercel function. Each has its own limit
  (retries included), and their sum stays under ``ONBOARDING_BUDGET_SECONDS``. A
  slow judge, analyst or synthesizer lets the onboarding go on with what it has
  instead of failing.
* Errors outside any ``try`` (the judge's constructor, the profile writes) became a
  500 without CORS; they now answer a stable code.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.core.errors import ApiError
from app.data.onboarding_guide import GUIDE_STEP_KEYS
from app.services import onboarding_guard, taste_agent
from app.services.ai_service import ai_service
from app.services.onboarding_guard import GuardResult
from app.services.onboarding_pipeline import run_complete_onboarding_pipeline
from app.services.taste_agent import (
    AnswerAnalysis,
    PersonTaste,
    StepQA,
    StepTaste,
    WeightedKeyword,
    analyze_answers,
    analyze_person,
    step_documents,
)


def test_the_default_limits_fit_the_budget() -> None:
    total = (
        settings.MODERATION_TIMEOUT_SECONDS
        + settings.GUARD_JUDGE_TIMEOUT_SECONDS
        + settings.TASTE_EXTRACT_TIMEOUT_SECONDS
        + settings.TASTE_SYNTH_TIMEOUT_SECONDS
        + settings.EMBEDDING_TIMEOUT_SECONDS
    )
    assert total <= settings.ONBOARDING_BUDGET_SECONDS < 60


# ------------------------------------------------------------- moderation --


class _Moderation:
    def __init__(self, flagged: bool = False) -> None:
        self.results = [MagicMock(flagged=flagged)]


async def test_moderation_uses_the_configured_model_and_timeout() -> None:
    create = AsyncMock(return_value=_Moderation())
    with patch.object(ai_service._client.moderations, "create", create):
        assert await ai_service.moderate_text("gosto de frevo") == (True, [])

    kwargs = create.await_args.kwargs
    assert kwargs["model"] == settings.MODERATION_MODEL == "omni-moderation-latest"
    assert kwargs["timeout"] == settings.MODERATION_TIMEOUT_SECONDS


async def test_a_moderation_failure_is_recorded(recorded: list[dict[str, Any]]) -> None:
    create = AsyncMock(side_effect=RuntimeError("model_not_found"))
    with patch.object(ai_service._client.moderations, "create", create):
        assert await ai_service.moderate_text("gosto de frevo") == (False, ["moderation_api_error"])

    assert recorded[0]["source"] == "ai"
    assert recorded[0]["code"] == "moderation_api_error"
    assert recorded[0]["error_type"] == "RuntimeError"


async def test_a_slow_moderation_counts_as_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MODERATION_TIMEOUT_SECONDS", 0.05)

    async def slow(**_: Any) -> _Moderation:
        await asyncio.sleep(1)
        return _Moderation()

    with patch.object(ai_service._client.moderations, "create", slow):
        assert await ai_service.moderate_text("gosto de frevo") == (False, ["moderation_api_error"])


# -------------------------------------------------------------- embeddings --


async def test_an_embedding_timeout_reaches_the_sdk() -> None:
    data = MagicMock(data=[MagicMock(embedding=[0.1], index=0)])
    create = AsyncMock(return_value=data)
    with patch.object(ai_service._client.embeddings, "create", create):
        await ai_service.generate_embedding("frevo", timeout=8.0)

    assert create.await_args.kwargs["timeout"] == 8.0


async def test_the_onboarding_embeddings_have_a_timeout() -> None:
    db = MagicMock()
    db.get_or_create_profile = AsyncMock(return_value={"id": "u1"})
    db.upsert_user_taste_documents = AsyncMock(return_value=6)
    db.update_profile = AsyncMock()
    one = AsyncMock(return_value=[0.1] * 3)
    many = AsyncMock(return_value=[[0.1] * 3] * 6)
    with (
        patch("app.services.onboarding_pipeline.run_onboarding_guard", AsyncMock(return_value=GuardResult(ok=True))),
        patch("app.services.onboarding_pipeline.analyze_person", AsyncMock(return_value=_person())),
        patch.object(ai_service, "generate_embedding", one),
        patch.object(ai_service, "generate_embeddings", many),
    ):
        await run_complete_onboarding_pipeline("u1", "jwt", db, _answers())

    assert one.await_args.kwargs["timeout"] == settings.EMBEDDING_TIMEOUT_SECONDS
    assert many.await_args.kwargs["timeout"] == settings.EMBEDDING_TIMEOUT_SECONDS


# ------------------------------------------------------------------ judge --


def _state() -> dict[str, Any]:
    return {"normalized": "Gosto de frevo e de praia.", "blocked": False}


async def test_a_slow_judge_lets_the_onboarding_go_on(
    monkeypatch: pytest.MonkeyPatch, recorded: list[dict[str, Any]]
) -> None:
    # Moderation already passed; the judge is an extra check, not a reason to fail.
    monkeypatch.setattr(settings, "GUARD_JUDGE_TIMEOUT_SECONDS", 0.05)

    async def slow(_: Any) -> Any:
        await asyncio.sleep(1)

    llm = MagicMock()
    llm.with_structured_output.return_value.ainvoke = slow
    with patch.object(onboarding_guard, "ChatOpenAI", return_value=llm):
        state = await onboarding_guard._judge_llm(_state())

    assert state["blocked"] is False
    assert [r["code"] for r in recorded] == ["llm_judge_timeout"]


async def test_a_judge_that_cannot_be_built_is_unavailable_not_a_crash() -> None:
    with patch.object(onboarding_guard, "ChatOpenAI", side_effect=ValueError("bad model")):
        state = await onboarding_guard._judge_llm(_state())

    assert state["blocked"] is True
    assert state["block_code"] == "moderation_unavailable"
    assert state["detail_codes"] == ["llm_judge_error"]


# -------------------------------------------------------------- extraction --


class _Slow:
    """A runnable that answers after ``seconds`` (the limit is the caller's)."""

    def __init__(self, seconds: float, out: Any = None) -> None:
        self.seconds, self.out = seconds, out

    async def ainvoke(self, *_: Any) -> Any:
        await asyncio.sleep(self.seconds)
        return self.out


async def test_a_slow_analyst_leaves_the_answer_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TASTE_EXTRACT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(taste_agent, "_structured", lambda *_: _Slow(1))
    answer = "Gosto muito de frevo, de maracatu e de passar o domingo na praia de Boa Viagem."

    steps = await analyze_answers([StepQA("music", "Que música?", answer)])

    assert steps[0].analysis is None
    assert [d.metadata["extraction"] for d in step_documents(steps)] == ["short"]


async def test_all_answers_are_analysed_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # Six answers that each take the whole limit must not take six times the limit.
    monkeypatch.setattr(settings, "TASTE_EXTRACT_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(taste_agent, "_structured", lambda *_: _Slow(1))

    loop = asyncio.get_running_loop()
    start = loop.time()
    await analyze_answers([StepQA(f"s{i}", "P?", "frevo") for i in range(6)])

    assert loop.time() - start < 0.9


async def test_a_slow_synthesizer_falls_back_to_merging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TASTE_SYNTH_TIMEOUT_SECONDS", 0.05)
    analysis = AnswerAnalysis(keywords=[WeightedKeyword(term="Frevo", weight=3)], summary="Gosta de frevo.")

    def runnable(schema: Any) -> _Slow:
        return _Slow(0, analysis) if schema is AnswerAnalysis else _Slow(1)

    monkeypatch.setattr(taste_agent, "_structured", runnable)

    person = await analyze_person([StepQA("music", "Que música?", "frevo")])

    assert person.mode == "merged"
    assert person.taste is not None and person.taste.keywords == ("frevo",)


# ------------------------------------------------------------ profile writes --


def _answers() -> dict[str, str]:
    return {k: f"Resposta elaborada para {k} com mais de quarenta caracteres mínimos." for k in GUIDE_STEP_KEYS}


def _person() -> PersonTaste:
    steps = [StepTaste(StepQA(k, "P?", f"resposta {k}"), None) for k in GUIDE_STEP_KEYS]
    return PersonTaste(steps, None, "raw")


async def test_a_profile_that_cannot_be_read_is_a_clear_error() -> None:
    db = MagicMock()
    db.get_or_create_profile = AsyncMock(side_effect=RuntimeError("supabase down"))

    with pytest.raises(ApiError) as caught:
        await run_complete_onboarding_pipeline("u1", "jwt", db, _answers())

    assert caught.value.status_code == 500
    assert caught.value.code == "profile_write_failed"


async def test_a_profile_that_cannot_be_saved_is_a_clear_error() -> None:
    db = MagicMock()
    db.get_or_create_profile = AsyncMock(return_value={"id": "u1"})
    db.upsert_user_taste_documents = AsyncMock(return_value=6)
    db.update_profile = AsyncMock(side_effect=RuntimeError("supabase down"))
    with (
        patch("app.services.onboarding_pipeline.run_onboarding_guard", AsyncMock(return_value=GuardResult(ok=True))),
        patch("app.services.onboarding_pipeline.analyze_person", AsyncMock(return_value=_person())),
        patch.object(ai_service, "generate_embedding", AsyncMock(return_value=[0.1] * 3)),
        patch.object(ai_service, "generate_embeddings", AsyncMock(return_value=[[0.1] * 3] * 6)),
        pytest.raises(ApiError) as caught,
    ):
        await run_complete_onboarding_pipeline("u1", "jwt", db, _answers())

    assert caught.value.code == "profile_write_failed"
