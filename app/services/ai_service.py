"""Async OpenAI client for embeddings and chat (singleton instance at module load)."""

from __future__ import annotations

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

    async def generate_embeddings(self, texts: list[str]) -> list[list[float]]:
        """Batch embeddings in a single request (one OpenAI call for all inputs)."""
        if not texts:
            return []
        response = await self._client.embeddings.create(
            model=settings.EMBEDDING_MODEL,
            input=texts,
            dimensions=settings.EMBEDDING_DIMENSIONS,
        )
        ordered = sorted(response.data, key=lambda d: d.index)
        return [list(d.embedding) for d in ordered]

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

    async def json_completion(
        self,
        system_prompt: str,
        user_content: str,
        model: str,
    ) -> str:
        """Chat completion constrained to a JSON object (used by the ETL transform stage)."""
        completion = await self._client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        content = completion.choices[0].message.content
        return content or ""

    @staticmethod
    def _weighted_sum(
        a: list[float],
        b: list[float],
        weight_a: float,
        weight_b: float,
    ) -> list[float]:
        """Element-wise ``a * weight_a + b * weight_b``.

        Plain Python on purpose: this was numpy, which cost 73 MB of the
        serverless bundle for three weighted sums over 1536 floats. ``strict``
        keeps numpy's refusal to combine vectors of different lengths — silently
        truncating to the shorter one would corrupt an embedding unnoticed.
        """
        return [x * weight_a + y * weight_b for x, y in zip(a, b, strict=True)]

    @staticmethod
    def blend_vectors(
        personal: list[float],
        social: list[float],
        w_personal: float = 0.8,
        w_social: float = 0.2,
    ) -> list[float]:
        """Hybrid query vector for social mode (weights tunable)."""
        return AIService._weighted_sum(personal, social, w_personal, w_social)

    @staticmethod
    def update_interest_organically(current: list[float], new: list[float]) -> list[float]:
        """V_final = V_current * w_c + V_new * w_n (config-driven)."""
        return AIService._weighted_sum(
            current,
            new,
            settings.INTEREST_WEIGHT_CURRENT,
            settings.INTEREST_WEIGHT_NEW,
        )

    @staticmethod
    def blend_interest_weighted(
        current: list[float],
        new: list[float],
        weight_current: float,
        weight_new: float,
    ) -> list[float]:
        """Generic convex blend (used for anchor vs organic sync)."""
        return AIService._weighted_sum(current, new, weight_current, weight_new)

    async def moderate_text(self, text: str) -> tuple[bool, list[str]]:
        """Return (is_allowed, flagged_categories_or_reasons)."""
        if not (text or "").strip():
            return True, []
        try:
            resp = await self._client.moderations.create(
                model="text-moderation-latest",
                input=text[:8000],
            )
        except Exception:
            return False, ["moderation_api_error"]
        result = resp.results[0]
        if result.flagged:
            cats: list[str] = []
            cats_obj = result.categories
            raw = cats_obj.model_dump() if hasattr(cats_obj, "model_dump") else dict(cats_obj)
            for name, flagged in raw.items():
                if flagged:
                    cats.append(str(name))
            return False, cats or ["moderation_flagged"]
        return True, []


ai_service = AIService()
