"""ETL load: embeds each event and upserts rows carrying provenance keys."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.etl.load import upsert_events
from app.services.etl.schemas import StandardizedEvent


def _event() -> StandardizedEvent:
    return StandardizedEvent(
        source="apify",
        source_event_id="a1",
        title="Show de Frevo",
        description="Noite animada",
        category="music",
        location_name="Recife",
        lat=-8.0,
        long=-34.0,
        price=Decimal("40.00"),
    )


@pytest.mark.asyncio
async def test_upsert_events_embeds_and_writes_provenance() -> None:
    db = MagicMock()
    db.upsert_events = AsyncMock(return_value=1)
    vec = [0.1] * 1536

    with patch(
        "app.services.etl.load.ai_service.generate_embedding",
        new_callable=AsyncMock,
        return_value=vec,
    ):
        count = await upsert_events(db, [_event()])

    assert count == 1
    db.upsert_events.assert_awaited_once()
    rows = db.upsert_events.await_args.args[0]
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "apify"
    assert row["source_event_id"] == "a1"
    assert row["event_embedding"] == vec
    assert row["price"] == "40.00"


@pytest.mark.asyncio
async def test_upsert_events_empty_is_noop() -> None:
    db = MagicMock()
    db.upsert_events = AsyncMock()

    assert await upsert_events(db, []) == 0
    db.upsert_events.assert_not_awaited()
