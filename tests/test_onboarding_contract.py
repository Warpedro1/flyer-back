"""Contract validation for onboarding `answers`."""

import pytest
from fastapi import HTTPException

from app.data.onboarding_guide import GUIDE_STEP_KEYS
from app.services.onboarding_contract import validate_onboarding_answers


def _valid() -> dict[str, str]:
    return {k: f"Resposta adequada para {k} com comprimento suficiente aqui." for k in GUIDE_STEP_KEYS}


def test_validate_ok() -> None:
    bundle = validate_onboarding_answers(_valid())
    assert len(bundle.answers) == len(GUIDE_STEP_KEYS)


def test_unknown_key() -> None:
    body = {**_valid(), "extra": "x"}
    with pytest.raises(HTTPException) as ei:
        validate_onboarding_answers(body)
    assert ei.value.status_code == 422
    assert ei.value.detail["code"] == "unknown_step_key"


def test_incomplete() -> None:
    body = {k: _valid()[k] for k in GUIDE_STEP_KEYS[:-1]}
    with pytest.raises(HTTPException) as ei:
        validate_onboarding_answers(body)
    assert ei.value.detail["code"] == "incomplete_answers"


def test_empty_answer() -> None:
    body = _valid()
    body["free_time"] = "   "
    with pytest.raises(HTTPException) as ei:
        validate_onboarding_answers(body)
    assert ei.value.detail["code"] == "empty_answer"
    assert "free_time" in ei.value.detail["invalid_steps"]


def test_invalid_type() -> None:
    body = _valid()
    body["free_time"] = 123  # type: ignore[assignment]
    with pytest.raises(HTTPException) as ei:
        validate_onboarding_answers(body)
    assert ei.value.detail["code"] == "invalid_answer_type"
