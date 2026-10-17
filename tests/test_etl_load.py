"""ETL load: one embedding request per batch, idempotent upsert, event→place links."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.etl.load import link_events_to_places, same_place, upsert_events
from app.services.etl.schemas import StandardizedEvent

VEC = [0.1] * 1536


def _event(n: int, **overrides: Any) -> StandardizedEvent:
    data: dict[str, Any] = {
        "source": "sympla",
        "source_event_id": f"s{n}",
        "title": f"Show {n}",
        "description": "Noite animada",
        "category": "musica_show",
        "location_name": "Teatro do Parque",
        "lat": -8.0631,
        "long": -34.8811,
        "price": Decimal("40.00"),
    }
    data.update(overrides)
    return StandardizedEvent(**data)


def _place(**overrides: Any) -> StandardizedEvent:
    data: dict[str, Any] = {
        "source": "google_maps",
        "source_event_id": "g1",
        "kind": "place",
        "title": "Parraxaxá",
        "category": "comida_bebida",
        "lat": -8.11,
        "long": -34.89,
        "price_level": 2,
        "opening_hours": [{"day": "segunda-feira", "hours": "11:30 a 22:00"}],
    }
    data.update(overrides)
    return StandardizedEvent(**data)


def _db() -> MagicMock:
    db = MagicMock()
    db.upsert_events = AsyncMock(side_effect=lambda rows: len(rows))
    return db


async def test_embeddings_are_requested_in_batches_not_one_per_item() -> None:
    db = _db()
    embed = AsyncMock(side_effect=lambda texts: [VEC] * len(texts))

    with patch("app.services.etl.load.ai_service.generate_embeddings", embed):
        count = await upsert_events(db, [_event(n) for n in range(250)])

    assert count == 250
    assert [len(c.args[0]) for c in embed.await_args_list] == [100, 100, 50]


async def test_rows_carry_provenance_and_the_place_fields() -> None:
    db = _db()

    with patch("app.services.etl.load.ai_service.generate_embeddings", AsyncMock(return_value=[VEC])):
        await upsert_events(db, [_place()])

    row = db.upsert_events.await_args.args[0][0]
    assert row["source"] == "google_maps"
    assert row["source_event_id"] == "g1"
    assert row["kind"] == "place"
    assert row["price_level"] == 2
    assert row["price"] is None
    assert row["opening_hours"] == [{"day": "segunda-feira", "hours": "11:30 a 22:00"}]
    assert row["event_embedding"] == VEC


async def test_every_row_has_the_same_keys_and_never_touches_place_id() -> None:
    # PostgREST takes the columns of a bulk insert from the rows; and a re-import
    # must not undo a link the creator made by hand.
    db = _db()

    with patch("app.services.etl.load.ai_service.generate_embeddings", AsyncMock(return_value=[VEC, VEC])):
        await upsert_events(db, [_event(1, price=None), _place()])

    rows = db.upsert_events.await_args.args[0]
    assert rows[0].keys() == rows[1].keys()
    assert "place_id" not in rows[0]


async def test_a_failed_embedding_still_imports_the_items(caplog: pytest.LogCaptureFixture) -> None:
    db = _db()
    failing = AsyncMock(side_effect=RuntimeError("openai down"))

    with (
        patch("app.services.etl.load.ai_service.generate_embeddings", failing),
        caplog.at_level(logging.ERROR, logger="app.services.etl.load"),
    ):
        count = await upsert_events(db, [_event(1)])

    assert count == 1
    assert db.upsert_events.await_args.args[0][0]["event_embedding"] is None
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


async def test_upserts_go_in_chunks() -> None:
    db = _db()

    with patch(
        "app.services.etl.load.ai_service.generate_embeddings",
        AsyncMock(side_effect=lambda texts: [VEC] * len(texts)),
    ):
        await upsert_events(db, [_event(n) for n in range(450)])

    assert [len(c.args[0]) for c in db.upsert_events.await_args_list] == [200, 200, 50]


# ------------------------------------------------------------------ links --


@pytest.mark.parametrize(
    ("venue", "place", "metres", "expected"),
    [
        ("Teatro do Parque", "Teatro do Parque", 30, True),
        ("Recife Expo Center", "Centro de Convenções Recife Expo Center", 120, True),
        ("Parraxaxá Boa Viagem", "Parraxaxá", 80, True),
        ("Teatro do Parque", "Teatro do Parque", 400, False),
        ("Colégio Santa Maria", "Padaria Santa Maria", 50, False),
        ("R. Buenos Aires, 80", "Bar", 10, False),
    ],
)
def test_same_place(venue: str, place: str, metres: float, expected: bool) -> None:
    assert same_place(venue, place, metres) is expected


async def test_events_are_linked_to_the_matching_place_nearby() -> None:
    db = MagicMock()
    db.list_places_near = AsyncMock(
        return_value=[
            {"id": "place-1", "title": "Teatro do Parque", "lat": -8.0632, "long": -34.8812},
            {"id": "place-2", "title": "Bar do Parque", "lat": -8.0632, "long": -34.8812},
        ]
    )
    db.link_event_to_place = AsyncMock(return_value=True)

    linked = await link_events_to_places(db, [_event(1), _event(2, location_name="Outro lugar"), _place()])

    assert linked == 1
    db.link_event_to_place.assert_awaited_once_with("sympla", "s1", "place-1")


async def test_without_events_there_is_nothing_to_link() -> None:
    db = MagicMock()
    db.list_places_near = AsyncMock()

    assert await link_events_to_places(db, [_place()]) == 0
    db.list_places_near.assert_not_called()


async def test_rows_carry_the_end_and_the_check_in_opening() -> None:
    from datetime import datetime, timezone

    db = _db()
    start = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 18, 2, 0, tzinfo=timezone.utc)

    with patch("app.services.etl.load.ai_service.generate_embeddings", AsyncMock(return_value=[VEC, VEC])):
        await upsert_events(db, [_event(1, event_date=start, ends_at=end, checkin_opens_at=start), _place()])

    event_row, place_row = db.upsert_events.await_args.args[0]
    assert event_row["ends_at"] == end.isoformat()
    assert event_row["checkin_opens_at"] == start.isoformat()
    assert place_row["ends_at"] is None
    # Nobody owns an imported event: a re-import never touches a manual close.
    assert "checkin_closed_at" not in event_row
