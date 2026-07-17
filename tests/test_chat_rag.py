"""Chat RAG system-prompt assembly (mocked IO)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.chat import CHAT_SYSTEM_PROMPT, _build_system_prompt


async def _vec() -> list[float]:
    return [0.1] * 1536


@pytest.mark.asyncio
async def test_system_prompt_injects_retrieved_taste_docs() -> None:
    db = MagicMock()
    db.match_user_taste_documents = AsyncMock(
        return_value=[
            {"step_key": "favorite_media", "content": "Curte jazz e cinema", "similarity": 0.9},
            {"step_key": "one_food_forever", "content": "Adora comida japonesa", "similarity": 0.8},
        ]
    )
    db.get_profile = AsyncMock()

    prompt = await _build_system_prompt(db, "u1", "jwt", asyncio.create_task(_vec()))

    assert "Curte jazz e cinema" in prompt
    assert "Adora comida japonesa" in prompt
    assert "<<<USER_PREFERENCES>>>" in prompt
    # Fallback profile read is skipped when retrieval returns rows.
    db.get_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_system_prompt_falls_back_to_static_summary() -> None:
    db = MagicMock()
    db.match_user_taste_documents = AsyncMock(return_value=[])
    db.get_profile = AsyncMock(return_value={"onboarding_preferences_text": "Resumo estático do perfil"})

    prompt = await _build_system_prompt(db, "u1", "jwt", asyncio.create_task(_vec()))

    assert "Resumo estático do perfil" in prompt
    assert "<<<USER_PREFERENCES>>>" in prompt


@pytest.mark.asyncio
async def test_system_prompt_is_base_when_no_taste_and_no_profile() -> None:
    db = MagicMock()
    db.match_user_taste_documents = AsyncMock(return_value=[])
    db.get_profile = AsyncMock(return_value=None)

    prompt = await _build_system_prompt(db, "u1", "jwt", asyncio.create_task(_vec()))

    assert prompt == CHAT_SYSTEM_PROMPT
