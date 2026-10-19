"""Async OpenAI client for embeddings and chat (singleton instance at module load)."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable
from typing import Any, TypeVar

from openai import AsyncOpenAI

from app.core.config import settings
from app.services import error_log

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _build_client() -> AsyncOpenAI:
    """With LangSmith tracing on, every OpenAI call is traced (#25 phase 1): model,
    duration, tokens, errors and, by decision 2026-10-16, the text."""
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    if not (settings.LANGSMITH_TRACING and settings.LANGSMITH_API_KEY):
        return client
    try:
        from langsmith import traceable
        from langsmith.wrappers import wrap_openai

        client = wrap_openai(client)
        # wrap_openai covers chat only; embeddings and moderation are traced here.
        client.embeddings.create = traceable(  # type: ignore[method-assign]
            name="openai.embeddings", run_type="embedding"
        )(client.embeddings.create)
        client.moderations.create = traceable(  # type: ignore[method-assign]
            name="openai.moderations", run_type="tool"
        )(client.moderations.create)
    except Exception as exc:  # noqa: BLE001 — tracing is optional
        logger.warning("LangSmith tracing unavailable (%s); calls are not traced", type(exc).__name__)
    return client


async def _limited(call: Awaitable[T], timeout: float | None) -> T:
    """`timeout` covers the whole call, the SDK's retries included."""
    if timeout is None:
        return await call
    return await asyncio.wait_for(call, timeout=timeout)


class AIService:
    """Uses AsyncOpenAI; system prompt kept separate from user messages."""

    def __init__(self) -> None:
        self._client = _build_client()

    @staticmethod
    def _timeout_kwargs(timeout: float | None) -> dict[str, Any]:
        return {"timeout": timeout} if timeout is not None else {}

    async def generate_embedding(self, text: str, *, timeout: float | None = None) -> list[float]:
        response = await _limited(
            self._client.embeddings.create(
                model=settings.EMBEDDING_MODEL,
                input=text,
                dimensions=settings.EMBEDDING_DIMENSIONS,
                **self._timeout_kwargs(timeout),
            ),
            timeout,
        )
        return list(response.data[0].embedding)

    async def generate_embeddings(self, texts: list[str], *, timeout: float | None = None) -> list[list[float]]:
        """Batch embeddings in a single request (one OpenAI call for all inputs)."""
        if not texts:
            return []
        response = await _limited(
            self._client.embeddings.create(
                model=settings.EMBEDDING_MODEL,
                input=texts,
                dimensions=settings.EMBEDDING_DIMENSIONS,
                **self._timeout_kwargs(timeout),
            ),
            timeout,
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

    async def complete(
        self,
        system_prompt: str,
        user_content: str,
        *,
        model: str,
        max_tokens: int,
        timeout: float,
    ) -> str:
        """One plain-text answer (e.g. "Melhorar descrição", #22 C), limited in time."""
        completion = await _limited(
            self._client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                timeout=timeout,
            ),
            timeout,
        )
        return completion.choices[0].message.content or ""

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
        """Element-wise ``a * weight_a + b * weight_b``, rescaled to unit length.

        Plain Python on purpose: this was numpy, which cost 73 MB of the
        serverless bundle for three weighted sums over 1536 floats. ``strict``
        keeps numpy's refusal to combine vectors of different lengths — silently
        truncating to the shorter one would corrupt an embedding unnoticed.

        The rescale matters because embeddings are unit vectors and a weighted sum
        of two different ones is shorter: without it the stored interest vector
        shrank on every sync. A zero result is returned as is (nothing to scale).
        """
        out = [x * weight_a + y * weight_b for x, y in zip(a, b, strict=True)]
        norm = math.sqrt(sum(x * x for x in out))
        if norm == 0.0:
            return out
        return [x / norm for x in out]

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

    async def moderate_inputs(self, inputs: list[Any]) -> list[tuple[bool, list[str]]]:
        """One moderation request; one ``(allowed, categories)`` per input, in order.

        ``inputs`` are strings (each its own result) or a single multimodal list
        (``{"type": "image_url", ...}``). Raises on any failure: the caller decides
        what "unavailable" means (#22 A).
        """
        timeout = settings.MODERATION_TIMEOUT_SECONDS
        resp = await _limited(
            self._client.moderations.create(model=settings.MODERATION_MODEL, input=inputs, timeout=timeout),
            timeout,
        )
        return [(not r.flagged, _flagged_categories(r) if r.flagged else []) for r in resp.results]

    async def moderate_text(self, text: str) -> tuple[bool, list[str]]:
        """Return (is_allowed, flagged_categories_or_reasons).

        A failure (model gone, timeout, key) is ``["moderation_api_error"]``, logged
        and recorded in `app_errors`: it used to be swallowed, and a model OpenAI
        shut down blocked every onboarding unnoticed (#26).
        """
        if not (text or "").strip():
            return True, []
        timeout = settings.MODERATION_TIMEOUT_SECONDS
        try:
            resp = await _limited(
                self._client.moderations.create(
                    model=settings.MODERATION_MODEL,
                    input=text[:8000],
                    timeout=timeout,
                ),
                timeout,
            )
        except Exception as exc:  # noqa: BLE001 — every failure means "unavailable"
            logger.warning("Moderation failed (%s)", type(exc).__name__)
            await error_log.record_error(
                source="ai",
                code="moderation_api_error",
                error_type=type(exc).__name__,
                message=str(exc),
                details={"model": settings.MODERATION_MODEL},
            )
            return False, ["moderation_api_error"]
        result = resp.results[0]
        if result.flagged:
            return False, _flagged_categories(result)
        return True, []


def _flagged_categories(result: Any) -> list[str]:
    cats_obj = result.categories
    raw = cats_obj.model_dump() if hasattr(cats_obj, "model_dump") else dict(cats_obj)
    return [str(name) for name, flagged in raw.items() if flagged] or ["moderation_flagged"]


ai_service = AIService()
