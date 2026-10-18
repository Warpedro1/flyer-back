"""Load stage: embed in batches, upsert idempotently, link events to their places.

The vector of a row whose text did not change since its keywords were made is
rebuilt from those keywords (#22 B), without the LLM; any other row gets the raw
text, as before, and stays stale (`events.keywords_stale`) until the reprocessing
gives it keywords.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from app.services.ai_service import ai_service
from app.services.db_service import DBService
from app.services.etl.schemas import StandardizedEvent
from app.services.etl.transform import normalize
from app.services.event_keywords import raw_embedding_text, stored_keywords
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


async def _embedding_texts(db: DBService, events: list[StandardizedEvent]) -> list[str]:
    """Each row's vector text: its stored keywords when they are still fresh, else the raw text."""
    stored: dict[tuple[str, str], dict[str, Any]] = {}
    for source in sorted({ev.source for ev in events}):
        try:
            rows = await db.list_source_keywords(source)
        except Exception:
            # Not fatal: those rows get the raw text and stay stale until reprocessed.
            logger.exception("ETL could not read the stored keywords (source=%s)", source)
            continue
        stored.update({(source, sid): row for sid, row in rows.items()})
    texts: list[str] = []
    for ev in events:
        row = stored.get((ev.source, ev.source_event_id))
        fresh = row is not None and row.get("keywords_hash") == event_content_hash(ev.title, ev.description, ev.category)
        taste = stored_keywords(row) if fresh else None
        if taste is not None:
            texts.append(event_embedding_text(ev.title, ev.category, taste))
        else:
            texts.append(raw_embedding_text(ev.title, ev.description, ev.category))
    return texts


def _row(ev: StandardizedEvent, embedding: list[float] | None) -> dict[str, Any]:
    # Every row has the same keys: PostgREST takes a bulk insert's columns from the
    # rows. `place_id` is left out on purpose so a re-import never undoes a link.
    return {
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
        "event_embedding": embedding,
    }


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
            # The items still go in: discovery ranks them last until they get a vector.
            logger.exception("ETL embeddings failed for %d items", len(batch))
            out.extend([None] * len(batch))
    return out


async def upsert_events(db: DBService, events: list[StandardizedEvent]) -> int:
    """Embed and upsert (conflict on source + source_event_id); returns rows sent."""
    if not events:
        return 0
    vectors = await _embed(await _embedding_texts(db, events))
    rows = [_row(ev, vec) for ev, vec in zip(events, vectors, strict=True)]
    written = 0
    for start in range(0, len(rows), UPSERT_BATCH):
        written += await db.upsert_events(rows[start : start + UPSERT_BATCH])
    return written


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
