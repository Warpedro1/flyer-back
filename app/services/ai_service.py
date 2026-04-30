"""Async OpenAI client for embeddings and chat (singleton instance at module load)."""

from __future__ import annotations

import numpy as np
from openai import AsyncOpenAI

from app.core.config import settings


class AIService:
    """Uses AsyncOpenAI; system prompt kept separate from user messages."""

    def __init__(self) -> None:
        self._client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

    async def generate_embedding(self, text: str) -> list[float]:
        response = await self._client.embeddings.create(
            model=settings.EMBEDDING_MODEL,
            input=text,
            dimensions=settings.EMBEDDING_DIMENSIONS,
        )
        return list(response.data[0].embedding)

    async def chat_completion(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> str:
        completion = await self._client.chat.completions.create(
            model=settings.CHAT_MODEL,
            max_tokens=settings.CHAT_MAX_TOKENS,
            messages=[{"role": "system", "content": system_prompt}, *messages],
        )
        content = completion.choices[0].message.content
        return content or ""

    @staticmethod
    def blend_vectors(
        personal: list[float],
        social: list[float],
        w_personal: float = 0.8,
        w_social: float = 0.2,
    ) -> list[float]:
        """Hybrid query vector for social mode (weights tunable)."""
        a = np.asarray(personal, dtype=np.float64)
        b = np.asarray(social, dtype=np.float64)
        return (a * w_personal + b * w_social).tolist()

    @staticmethod
    def update_interest_organically(current: list[float], new: list[float]) -> list[float]:
        """V_final = V_current * w_c + V_new * w_n (config-driven)."""
        wc = settings.INTEREST_WEIGHT_CURRENT
        wn = settings.INTEREST_WEIGHT_NEW
        a = np.asarray(current, dtype=np.float64)
        b = np.asarray(new, dtype=np.float64)
        return (a * wc + b * wn).tolist()


ai_service = AIService()
