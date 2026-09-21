"""Capacity fields on the event payloads and the per-user capacity snapshot."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.events import _capacity_state, _row_to_event_read
from app.models.schemas import EventCreate, RsvpStatus


def _event_row(**extra: object) -> dict:
    row = {
        "id": "evt-1",
        "creator_id": "user-1",
        "title": "Jam session",
        "lat": 38.7,
        "long": -9.1,
    }
    row.update(extra)
    return row


def test_event_read_defaults_to_unlimited_for_pre_migration_rows() -> None:
    # Rows written before the capacity migration simply lack these keys; they must
    # read as "no limit" rather than blowing up the response model.
    out = _row_to_event_read(_event_row(), media=[])

    assert out.capacity is None
    assert out.waitlist_enabled is True
    assert out.call_ttl_minutes == 10
    assert out.capacity_state is None


def test_event_read_exposes_capacity_columns() -> None:
    out = _row_to_event_read(
        _event_row(capacity=50, waitlist_enabled=False, call_ttl_minutes=20),
        media=[],
    )

    assert out.capacity == 50
    assert out.waitlist_enabled is False
    assert out.call_ttl_minutes == 20


def test_event_create_is_unlimited_unless_a_capacity_is_given() -> None:
    body = EventCreate(title="Jam", lat=38.7, long=-9.1)

    assert body.capacity is None
    assert body.waitlist_enabled is True
    assert body.auto_call_next is True
    assert body.call_ttl_minutes == 10


def test_event_create_rejects_a_zero_capacity() -> None:
    # 0 would be an event nobody can attend; "no limit" is expressed by None.
    with pytest.raises(ValueError):
        EventCreate(title="Jam", lat=38.7, long=-9.1, capacity=0)


def test_event_create_rejects_an_absurd_call_window() -> None:
    with pytest.raises(ValueError):
        EventCreate(title="Jam", lat=38.7, long=-9.1, call_ttl_minutes=0)


@pytest.mark.asyncio
async def test_capacity_state_reports_the_callers_own_position() -> None:
    db = MagicMock()
    db.sweep_expired_calls = AsyncMock(return_value=0)
    db.count_rsvps_by_status = AsyncMock(side_effect=[50, 7])
    db.get_rsvp = AsyncMock(
        return_value={"status": "waitlisted", "waitlist_position": 4}
    )

    state = await _capacity_state(db, "evt-1", "user-2", "jwt", _event_row(capacity=50))

    assert state.capacity == 50
    assert state.taken == 50
    assert state.waitlist_count == 7
    assert state.my_status == RsvpStatus.waitlisted
    assert state.my_waitlist_position == 4


@pytest.mark.asyncio
async def test_capacity_state_for_an_unlimited_event() -> None:
    db = MagicMock()
    db.sweep_expired_calls = AsyncMock(return_value=0)
    db.count_rsvps_by_status = AsyncMock(side_effect=[12, 0])
    db.get_rsvp = AsyncMock(return_value=None)

    state = await _capacity_state(db, "evt-1", "user-2", "jwt", _event_row())

    assert state.capacity is None
    assert state.my_status is None
    assert state.my_waitlist_position is None


@pytest.mark.asyncio
async def test_capacity_state_sweeps_before_reading() -> None:
    # Reading a stale queue would show a call that has already run out of time.
    db = MagicMock()
    db.sweep_expired_calls = AsyncMock(return_value=1)
    db.count_rsvps_by_status = AsyncMock(side_effect=[1, 0])
    db.get_rsvp = AsyncMock(return_value=None)

    await _capacity_state(db, "evt-1", "user-2", "jwt", _event_row(capacity=5))

    db.sweep_expired_calls.assert_awaited_once_with("evt-1")
