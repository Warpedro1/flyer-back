"""Load stage: embed each standardized event and upsert it (idempotent by source id)."""

from __future__ import annotations

from app.services.ai_service import ai_service
from app.services.db_service import DBService
from app.services.etl.schemas import StandardizedEvent


async def upsert_events(db: DBService, events: list[StandardizedEvent]) -> int:
    """Generate an embedding per event and upsert; returns the number of rows written."""
    if not events:
        return 0
    rows: list[dict[str, object]] = []
    for ev in events:
        text = " ".join(
            part for part in (ev.title, ev.description or "", ev.category or "") if part
        ).strip()
        embedding = await ai_service.generate_embedding(text) if text else None
        row: dict[str, object] = {
            "source": ev.source,
            "source_event_id": ev.source_event_id,
            "title": ev.title,
            "description": ev.description,
            "category": ev.category,
            "location_name": ev.location_name,
            "lat": ev.lat,
            "long": ev.long,
        }
        if embedding:
            row["event_embedding"] = embedding
        if ev.event_date is not None:
            row["event_date"] = ev.event_date.isoformat()
        if ev.price is not None:
            row["price"] = str(ev.price)
        rows.append(row)
    return await db.upsert_events(rows)
