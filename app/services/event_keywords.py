"""Event and place keywords (#22 part B, decisions 2026-10-18).

The taste agent turns an event's title, description and category into keywords and
a summary; the vector is made from those, so it sits in the same space as a
person's. Three places run it:

* creating and editing an event, inside the request (``embed_app_events``);
* the import, which reuses the keywords of rows whose text did not change and
  leaves the rest stale (``etl/load.py``);
* the reprocessing (``reprocess_stale``), which works through the stale rows
  (``events.keywords_stale``) a chunk at a time, inside one function's 60 s.

Nothing here fails a request. Keywords are only written together with the vector
made from them: when either step fails nothing is written, so the row stays stale
and the next reprocessing picks it up.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import ROUND_UP, Decimal
from typing import Any

from app.core.config import settings
from app.services.ai_service import ai_service
from app.services.db_service import DBService
from app.services.taste_agent import (
    EventText,
    TasteKeywords,
    analyze_event,
    analyze_events,
    event_content_hash,
    event_embedding_text,
)

logger = logging.getLogger(__name__)

EMBED_BATCH = 100
# The stale rows read per page when estimating.
_PAGE = 1000
_WRITE_CONCURRENCY = 8

# Cost estimate (dry run). List prices per 1M tokens in 2026-10 — check them before
# trusting the total: gpt-4o-mini $0.15 in / $0.60 out, text-embedding-3-small $0.02.
_PRICE_IN = Decimal("0.15")
_PRICE_OUT = Decimal("0.60")
_PRICE_EMBED = Decimal("0.02")
# About 4 characters per token in Portuguese; the system prompt and the output
# schema go with every call, and an answer is about 15 keywords and two sentences.
_CHARS_PER_TOKEN = 4
_FIXED_INPUT_TOKENS = 450
_OUTPUT_TOKENS = 180
_EMBED_TOKENS = 90


def raw_embedding_text(title: str | None, description: str | None, category: str | None) -> str:
    """The vector's text before the agent, still used when the agent fails."""
    return " ".join(part for part in (title, description or "", category or "") if part).strip()


def event_text(row: dict[str, Any]) -> EventText:
    return EventText(
        title=str(row.get("title") or ""),
        description=str(row.get("description") or ""),
        category=row.get("category"),
        kind=row.get("kind"),
    )


def stored_keywords(row: dict[str, Any] | None) -> TasteKeywords | None:
    """The keywords a row already has, if they were made from its current text."""
    if not row:
        return None
    keywords = [str(k) for k in (row.get("keywords") or []) if str(k).strip()]
    if not keywords:
        return None
    return TasteKeywords(tuple(keywords), str(row.get("keywords_summary") or ""))


def _hash(row: dict[str, Any]) -> str:
    return event_content_hash(row.get("title"), row.get("description"), row.get("category"))


async def embed_app_events(
    db: DBService,
    event_ids: list[str],
    row: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> None:
    """Keywords and one vector for an event created or edited in the app, on every
    occurrence of its series (they share the text).

    ``row`` is the stored row (what the hash must match); ``previous`` the row before
    a PUT. When the text did not change and the keywords are fresh, nothing is redone.
    Without keywords the vector is made from the raw text, as before, and the event
    stays stale for the reprocessing.
    """
    if not event_ids:
        return
    digest = _hash(row)
    fresh = stored_keywords(previous) if previous and previous.get("keywords_hash") == digest else None
    if fresh is not None and previous and previous.get("event_embedding") is not None:
        return
    taste = fresh or await analyze_event(event_text(row))
    try:
        if taste is not None:
            vec = await ai_service.generate_embedding(
                event_embedding_text(str(row.get("title") or ""), row.get("category"), taste),
                timeout=settings.EMBEDDING_TIMEOUT_SECONDS,
            )
            if vec:
                await db.set_events_keywords(
                    event_ids,
                    keywords=list(taste.keywords),
                    summary=taste.summary,
                    keywords_hash=digest,
                    vector=vec,
                )
            return
        text = raw_embedding_text(row.get("title"), row.get("description"), row.get("category"))
        if not text:
            return
        vec = await ai_service.generate_embedding(text, timeout=settings.EMBEDDING_TIMEOUT_SECONDS)
        if vec:
            await db.set_events_embedding(event_ids, vec)
    except Exception:
        # Error, not warning: the event is created but personalised discovery ranks
        # it last until it gets a vector. This used to fail silently for every event.
        logger.exception("Event embedding failed (ids=%s)", event_ids)


# ------------------------------------------------------------- reprocessing --


@dataclass(frozen=True, slots=True)
class CostEstimate:
    items: int
    input_tokens: int
    output_tokens: int
    embedding_tokens: int
    usd: Decimal


def estimate_cost(rows: list[dict[str, Any]]) -> CostEstimate:
    """What reprocessing these rows would cost, roughly (no call is made)."""
    chars = sum(len(str(r.get("title") or "")) + len(str(r.get("description") or "")[:2000]) for r in rows)
    input_tokens = len(rows) * _FIXED_INPUT_TOKENS + chars // _CHARS_PER_TOKEN
    output_tokens = len(rows) * _OUTPUT_TOKENS
    embedding_tokens = len(rows) * _EMBED_TOKENS
    million = Decimal(1_000_000)
    usd = (
        Decimal(input_tokens) * _PRICE_IN + Decimal(output_tokens) * _PRICE_OUT + Decimal(embedding_tokens) * _PRICE_EMBED
    ) / million
    return CostEstimate(
        items=len(rows),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        embedding_tokens=embedding_tokens,
        usd=usd.quantize(Decimal("0.01"), rounding=ROUND_UP),
    )


async def all_stale(db: DBService) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while True:
        page = await db.list_stale_keyword_events(limit=_PAGE, offset=len(out))
        out.extend(page)
        if len(page) < _PAGE:
            return out


@dataclass(frozen=True, slots=True)
class ReprocessResult:
    picked: int
    processed: int
    failed: int


async def reprocess_stale(db: DBService, *, limit: int, budget_seconds: float) -> ReprocessResult:
    """Give keywords and a new vector to up to ``limit`` stale rows.

    The agent runs ``TASTE_AGENT_CONCURRENCY`` at a time within ``budget_seconds``;
    what it could not do stays stale. Each row is written on its own (its keywords
    differ), keywords and vector together.
    """
    rows = await db.list_stale_keyword_events(limit=limit)
    if not rows:
        return ReprocessResult(0, 0, 0)
    tastes = await analyze_events([event_text(r) for r in rows], budget_seconds=budget_seconds)
    done = [(r, t) for r, t in zip(rows, tastes, strict=True) if t is not None]
    vectors: list[list[float] | None] = []
    for start in range(0, len(done), EMBED_BATCH):
        batch = done[start : start + EMBED_BATCH]
        texts = [event_embedding_text(str(r.get("title") or ""), r.get("category"), t) for r, t in batch]
        try:
            got = await ai_service.generate_embeddings(texts, timeout=settings.EMBEDDING_TIMEOUT_SECONDS)
            if len(got) != len(batch):
                raise ValueError(f"expected {len(batch)} vectors, got {len(got)}")
            vectors.extend(got)
        except Exception:
            logger.exception("Keyword embeddings failed for %d items; they stay stale.", len(batch))
            vectors.extend([None] * len(batch))

    gate = asyncio.Semaphore(_WRITE_CONCURRENCY)

    async def write(row: dict[str, Any], taste: TasteKeywords, vec: list[float]) -> bool:
        async with gate:
            try:
                await db.set_events_keywords(
                    [str(row["id"])],
                    keywords=list(taste.keywords),
                    summary=taste.summary,
                    keywords_hash=_hash(row),
                    vector=vec,
                )
                return True
            except Exception:
                logger.exception("Keyword write failed for one event; it stays stale.")
                return False

    written = await asyncio.gather(
        *(write(r, t, v) for (r, t), v in zip(done, vectors, strict=True) if v is not None)
    )
    processed = sum(1 for ok in written if ok)
    return ReprocessResult(picked=len(rows), processed=processed, failed=len(rows) - processed)
