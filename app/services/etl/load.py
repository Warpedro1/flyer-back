"""Load stage: embed in batches, upsert idempotently, link events to their places.

The vector of a row whose text did not change since its keywords were made is
rebuilt from those keywords (#22 B), without the LLM; any other row gets the raw
text, as before, and stays stale (`events.keywords_stale`) until the reprocessing
gives it keywords. A row whose keywords are fresh must never get any other vector:
the reprocessing would not see it. So the load stops when the stored keywords
cannot be read, and a batch whose embeddings failed keeps the vectors it had.

Moderation (#22 A): what is new or changed since it was last checked goes through
the moderation first (``IMPORT_MODERATION_BUDGET_SECONDS``). Refused text never
enters (a row that was already here is hidden); a refused or unreadable cover is
dropped; what could not be checked in time waits for the next import (an existing
row keeps its checked content). Unchanged rows are never checked again.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.services.ai_service import ai_service
from app.services.db_service import DBService
from app.services.etl.schemas import StandardizedEvent
from app.services.etl.transform import normalize
from app.services.event_keywords import raw_embedding_text, stored_keywords
from app.services.moderation import check_imports, event_moderation_hash, import_text
from app.services.taste_agent import event_content_hash, event_embedding_text

logger = logging.getLogger(__name__)

EMBED_BATCH = 100
UPSERT_BATCH = 200
# An event links to a place only when both are this close and the names agree.
LINK_MAX_METRES = 200.0
# Words that say nothing about which place it is.
_STOPWORDS = frozenset(
    {"a", "o", "as", "os", "de", "da", "do", "das", "dos", "e", "em", "no", "na", "recife", "the"}
)


async def _stored_state(db: DBService, events: list[StandardizedEvent]) -> dict[tuple[str, str], dict[str, Any]]:
    """The rows already here, by (source, source_event_id).

    A failed read raises (the load stops): guessing "nothing stored" would give
    fresh rows a raw-text vector the reprocessing never sees, and would let a
    hidden row's content back in unchecked.
    """
    stored: dict[tuple[str, str], dict[str, Any]] = {}
    for source in sorted({ev.source for ev in events}):
        rows = await db.list_source_state(source)
        stored.update({(source, sid): row for sid, row in rows.items()})
    return stored


def _embedding_text(ev: StandardizedEvent, row: dict[str, Any] | None) -> str:
    """The row's vector text: its stored keywords when they are still fresh, else the raw text."""
    fresh = row is not None and row.get("keywords_hash") == event_content_hash(ev.title, ev.description, ev.category)
    taste = stored_keywords(row) if fresh else None
    if taste is not None:
        return event_embedding_text(ev.title, ev.category, taste)
    return raw_embedding_text(ev.title, ev.description, ev.category)


def _row(ev: StandardizedEvent, embedding: list[float] | None) -> dict[str, Any]:
    # Every row of a request has the same keys: PostgREST takes a bulk insert's
    # columns from the rows. `place_id` is left out on purpose so a re-import never
    # undoes a link, and `event_embedding` when there is no new vector (see below).
    row: dict[str, Any] = {
        "source": ev.source,
        "source_event_id": ev.source_event_id,
        "kind": ev.kind,
        "title": ev.title,
        "description": ev.description,
        "category": ev.category,
        "location_name": ev.location_name,
        "lat": ev.lat,
        "long": ev.long,
        "event_date": ev.event_date.isoformat() if ev.event_date else None,
        # `checkin_closed_at` is left out: nobody owns an imported event.
        "ends_at": ev.ends_at.isoformat() if ev.ends_at else None,
        "checkin_opens_at": ev.checkin_opens_at.isoformat() if ev.checkin_opens_at else None,
        "price": str(ev.price) if ev.price is not None else None,
        "price_level": ev.price_level,
        "rating": str(ev.rating) if ev.rating is not None else None,
        "opening_hours": [h.model_dump() for h in ev.opening_hours] if ev.opening_hours else None,
        "source_url": ev.source_url,
        "cover_url": ev.cover_url,
    }
    if embedding is not None:
        row["event_embedding"] = embedding
    return row


async def _embed(texts: list[str]) -> list[list[float] | None]:
    """One OpenAI request per batch; a failed batch imports without vectors."""
    out: list[list[float] | None] = []
    for start in range(0, len(texts), EMBED_BATCH):
        batch = texts[start : start + EMBED_BATCH]
        try:
            vectors = await ai_service.generate_embeddings(batch)
            if len(vectors) != len(batch):
                raise ValueError(f"expected {len(batch)} vectors, got {len(vectors)}")
            out.extend(vectors)
        except Exception:
            # The items still go in, keeping the vector they had (new ones have none
            # and discovery ranks them last until they get one).
            logger.exception("ETL embeddings failed for %d items", len(batch))
            out.extend([None] * len(batch))
    return out


@dataclass(slots=True)
class LoadResult:
    upserted: int = 0
    # Refused by the moderation: never entered, or (already here) hidden.
    blocked: int = 0
    # Could not be checked in time: left for the next import.
    pending: int = 0
    covers_removed: int = 0


def _moderated(ev: StandardizedEvent, row: dict[str, Any] | None) -> bool:
    return row is not None and row.get("moderation_hash") == event_moderation_hash(
        ev.title, ev.description, ev.cover_url
    )


async def upsert_events(
    db: DBService,
    events: list[StandardizedEvent],
    *,
    moderation_budget_seconds: float | None = None,
) -> LoadResult:
    """Moderate what changed, embed and upsert (conflict on source + source_event_id)."""
    result = LoadResult()
    if not events:
        return result
    stored = await _stored_state(db, events)
    to_check = [ev for ev in events if not _moderated(ev, stored.get((ev.source, ev.source_event_id)))]
    budget = settings.IMPORT_MODERATION_BUDGET_SECONDS if moderation_budget_seconds is None else moderation_budget_seconds
    checks = await check_imports(
        [(import_text(ev.title, ev.description), ev.cover_url) for ev in to_check], budget_seconds=budget
    )
    verdict = {(ev.source, ev.source_event_id): check for ev, check in zip(to_check, checks, strict=True)}

    now = datetime.now(timezone.utc).isoformat()
    kept: list[tuple[StandardizedEvent, dict[str, Any]]] = []
    to_block: list[tuple[StandardizedEvent, list[str]]] = []
    for ev in events:
        key = (ev.source, ev.source_event_id)
        if key not in verdict:
            kept.append((ev, {}))
            continue
        check = verdict[key]
        if check is None:
            result.pending += 1
            continue
        if not check.allowed:
            result.blocked += 1
            if key in stored and not stored[key].get("moderation_blocked"):
                to_block.append((ev, list(check.categories)))
            continue
        if check.drop_cover and ev.cover_url:
            ev = ev.model_copy(update={"cover_url": None})
            result.covers_removed += 1
        kept.append(
            (
                ev,
                {
                    "moderation_hash": event_moderation_hash(ev.title, ev.description, ev.cover_url),
                    "moderated_at": now,
                    "moderation_blocked": False,
                    "moderation_reasons": None,
                },
            )
        )

    vectors = await _embed([_embedding_text(ev, stored.get((ev.source, ev.source_event_id))) for ev, _ in kept])
    rows = [{**_row(ev, vec), **extra} for (ev, extra), vec in zip(kept, vectors, strict=True)]
    # PostgREST takes a bulk request's columns from its rows: rows with and without a
    # new vector, or a new moderation result, go in separate requests.
    groups: dict[frozenset[str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(frozenset(row), []).append(row)
    for group in groups.values():
        for start in range(0, len(group), UPSERT_BATCH):
            result.upserted += await db.upsert_events(group[start : start + UPSERT_BATCH])
    for ev, categories in to_block:
        await db.block_imported_event(ev.source, ev.source_event_id, categories)
    return result


# ------------------------------------------------------------------ links --


def _metres(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371000.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def _words(name: str) -> list[str]:
    return [w for w in normalize(name).replace(",", " ").replace("-", " ").split() if w not in _STOPWORDS]


def same_place(venue: str, place: str, metres: float) -> bool:
    """Close enough, and one name contained in the other (as whole words)."""
    if metres > LINK_MAX_METRES:
        return False
    a, b = _words(venue), _words(place)
    if not a or not b:
        return False
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(" ".join(short)) < 4:
        return False
    n = len(short)
    return any(long_[i : i + n] == short for i in range(len(long_) - n + 1))


async def link_events_to_places(db: DBService, events: list[StandardizedEvent]) -> int:
    """Point imported events at the place they happen in, when it is in the catalogue."""
    candidates = [e for e in events if e.kind == "event" and e.location_name]
    if not candidates:
        return 0
    margin = 0.01  # ~1 km, so places just outside the events' box are still seen
    places = await db.list_places_near(
        min(e.lat for e in candidates) - margin,
        max(e.lat for e in candidates) + margin,
        min(e.long for e in candidates) - margin,
        max(e.long for e in candidates) + margin,
    )
    linked = 0
    for ev in candidates:
        best: tuple[float, str] | None = None
        for place in places:
            metres = _metres(ev.lat, ev.long, float(place["lat"]), float(place["long"]))
            if same_place(ev.location_name or "", str(place.get("title") or ""), metres) and (
                best is None or metres < best[0]
            ):
                best = (metres, str(place["id"]))
        if best and await db.link_event_to_place(ev.source, ev.source_event_id, best[1]):
            linked += 1
    return linked
