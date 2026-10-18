"""Sampling options per model (onboarding failing in production, 2026-10-17).

`LLM_MODEL` is `gpt-5-mini` in production. Reasoning models (gpt-5, o1, o3, o4) only
accept the default temperature: the judge's `temperature=0` answered 400, which the
guard turned into 503 `moderation_unavailable`, and the extraction's 0.2 silently
fell back to the short document. Seen in `app_errors` (`llm_judge_error`,
"Unsupported value: 'temperature' does not support 0.0 with this model").
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from langchain_openai import ChatOpenAI

from app.core.config import settings
from app.services import onboarding_guard, taste_agent
from app.services.llm_options import chat_model_kwargs

# Taken at import, before the autouse fixture swaps it for a runnable that always fails.
from app.services.taste_agent import _structured as real_structured


@pytest.mark.parametrize("model", ["gpt-5-mini", "gpt-5", "gpt-5-nano", "o3-mini", "o4-mini", "o1"])
def test_reasoning_models_get_only_the_default_temperature(model: str) -> None:
    kwargs = chat_model_kwargs(model, temperature=0)

    assert kwargs["temperature"] == 1
    assert kwargs["reasoning_effort"] in {"minimal", "low"}


@pytest.mark.parametrize(("model", "temperature"), [("gpt-5-mini", 1), ("gpt-4o", 0)])
def test_the_request_sent_to_openai(model: str, temperature: float) -> None:
    # The library has its own default (0.7): check what actually goes out.
    llm = ChatOpenAI(api_key="sk-test", model=model, timeout=5, **chat_model_kwargs(model, temperature=0))

    payload = llm._get_request_payload([("user", "oi")])

    assert payload["temperature"] == temperature


def test_gpt5_uses_minimal_reasoning_to_fit_the_time_limits() -> None:
    assert chat_model_kwargs("gpt-5-mini", temperature=0)["reasoning_effort"] == "minimal"


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4o-mini", "gpt-4.1-mini", "gpt-5-chat-latest"])
def test_other_models_keep_the_temperature(model: str) -> None:
    assert chat_model_kwargs(model, temperature=0.2) == {"temperature": 0.2}


def test_the_judge_with_gpt5_sends_no_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GUARD_JUDGE_MODEL", "gpt-5-mini")
    built: dict[str, Any] = {}

    def fake_chat(**kwargs: Any) -> MagicMock:
        built.update(kwargs)
        return MagicMock()

    with patch.object(onboarding_guard, "ChatOpenAI", side_effect=fake_chat):
        asyncio.run(onboarding_guard._judge_llm({"normalized": "Gosto de frevo.", "blocked": False}))

    assert built["model"] == "gpt-5-mini"
    assert built["temperature"] == 1
    assert built["reasoning_effort"] == "minimal"


def test_the_taste_agent_with_gpt5_sends_no_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TASTE_EXTRACTOR_MODEL", "gpt-5-mini")
    monkeypatch.setattr(taste_agent, "_runnables", {})
    built: dict[str, Any] = {}

    def fake_chat(**kwargs: Any) -> MagicMock:
        built.update(kwargs)
        return MagicMock()

    with patch.object(taste_agent, "ChatOpenAI", side_effect=fake_chat):
        real_structured(taste_agent.AnswerAnalysis)

    assert built["temperature"] == 1
    assert built["reasoning_effort"] == "minimal"
