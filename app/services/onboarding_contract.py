"""Strict validation for POST /chat/complete-onboarding `answers` (stable 422 codes)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status

from app.core.config import settings
from app.data.onboarding_guide import GUIDE_STEP_KEYS, GUIDE_STEPS


@dataclass(frozen=True, slots=True)
class ValidatedOnboardingAnswers:
    """Ordered answers aligned with `GUIDE_STEPS`."""

    answers: dict[str, str]


def _strip_collapse(s: str) -> str:
    return " ".join(s.split()).strip()


def validate_onboarding_answers(raw: Any) -> ValidatedOnboardingAnswers:
    """Raise HTTPException 422 with stable `code` and optional `invalid_steps`."""
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_answers_type", "invalid_steps": []},
        )

    unknown = [k for k in raw if k not in GUIDE_STEP_KEYS]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "unknown_step_key", "invalid_steps": sorted(unknown)},
        )

    missing = [k for k in GUIDE_STEP_KEYS if k not in raw]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "incomplete_answers", "invalid_steps": list(missing)},
        )

    cleaned: dict[str, str] = {}
    empty_keys: list[str] = []
    too_long_keys: list[str] = []
    bad_type_keys: list[str] = []

    for key in GUIDE_STEP_KEYS:
        val = raw[key]
        if not isinstance(val, str):
            bad_type_keys.append(key)
            continue
        text = _strip_collapse(val)
        if not text:
            empty_keys.append(key)
            continue
        if len(text) > settings.ONBOARDING_ANSWER_MAX_CHARS:
            too_long_keys.append(key)
            continue
        cleaned[key] = text

    if bad_type_keys:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_answer_type", "invalid_steps": sorted(bad_type_keys)},
        )
    if empty_keys:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "empty_answer", "invalid_steps": sorted(empty_keys)},
        )
    if too_long_keys:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "answer_too_long", "invalid_steps": sorted(too_long_keys)},
        )

    total_len = sum(len(cleaned[k]) for k in GUIDE_STEP_KEYS)
    if total_len > settings.ONBOARDING_ANSWERS_TOTAL_MAX_CHARS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "total_answer_length_exceeded", "invalid_steps": list(GUIDE_STEP_KEYS)},
        )

    return ValidatedOnboardingAnswers(answers=cleaned)


def ordered_qa_tuples(answers: dict[str, str]) -> list[tuple[str, str, str]]:
    """`(step_key, prompt, answer)` in official guide order."""
    out: list[tuple[str, str, str]] = []
    for step in GUIDE_STEPS:
        out.append((step.key, step.prompt, answers[step.key]))
    return out
