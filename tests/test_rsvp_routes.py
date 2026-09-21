"""Route-level mapping for the RSVP domain: RPC outcomes, buckets, queue distance."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.rsvp import (
    _http_from_rpc,
    _not_admissible_error,
    _require_creator,
    _row_to_rsvp,
    _with_ahead_count,
    bucket_attendees,
)
from app.models.schemas import RsvpStatus
from app.services.db_service import RpcError


def _row(status: str, **extra: object) -> dict:
    row = {
        "id": f"rsvp-{status}",
        "event_id": "evt-1",
        "user_id": f"user-{status}",
        "status": status,
        "waitlist_position": None,
        "profiles": {"id": f"user-{status}", "name": status.title(), "email": None},
    }
    row.update(extra)
    return row


# ------------------------------------------------------------ RPC -> HTTP --


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("event_full", 409),
        ("event_not_found", 404),
        ("not_event_creator", 403),
        ("already_admitted", 409),
        ("rsvp_not_recallable", 409),
    ],
)
def test_known_rpc_failures_map_to_their_status(message: str, expected: int) -> None:
    assert _http_from_rpc(RpcError(message)).status_code == expected


def test_unknown_rpc_failure_falls_back_to_500() -> None:
    # An unmapped raise is a backend bug, not something the user did wrong.
    assert _http_from_rpc(RpcError("something_new")).status_code == 500


# --------------------------------------------------------------- buckets --


def test_bucket_attendees_splits_by_status() -> None:
    rows = [
        _row("confirmed"),
        _row("called"),
        _row("waitlisted", waitlist_position=1),
        _row("no_show"),
    ]

    out = bucket_attendees(rows, capacity=10)

    assert [r.status for r in out.confirmed] == [RsvpStatus.confirmed]
    assert [r.status for r in out.called] == [RsvpStatus.called]
    assert [r.status for r in out.waitlist] == [RsvpStatus.waitlisted]
    assert [r.status for r in out.no_show] == [RsvpStatus.no_show]
    assert out.capacity == 10


def test_taken_counts_only_seats_actually_held() -> None:
    rows = [
        _row("confirmed"),
        _row("called"),
        _row("admitted"),
        _row("waitlisted", waitlist_position=1),
        _row("no_show"),
        _row("cancelled"),
    ]

    out = bucket_attendees(rows, capacity=3)

    # confirmed + called + admitted; waitlisted / no_show / cancelled hold nothing,
    # which is what frees the queue to move past someone who never showed up.
    assert out.taken == 3


def test_unlimited_event_reports_no_capacity() -> None:
    out = bucket_attendees([_row("confirmed")], capacity=None)

    assert out.capacity is None
    assert out.taken == 1


def test_bucket_attendees_carries_the_attendee_profile() -> None:
    out = bucket_attendees([_row("waitlisted", waitlist_position=2)], capacity=1)

    assert out.waitlist[0].profile.name == "Waitlisted"
    assert out.waitlist[0].waitlist_position == 2


def test_bucket_attendees_tolerates_a_missing_profile_join() -> None:
    row = _row("confirmed")
    row["profiles"] = None

    out = bucket_attendees([row], capacity=None)

    assert out.confirmed[0].profile.id == "user-confirmed"
    assert out.confirmed[0].profile.name is None


# ------------------------------------------------------------ queue depth --


@pytest.mark.asyncio
async def test_waitlisted_rsvp_reports_how_many_are_ahead() -> None:
    db = MagicMock()
    db.count_waitlist_ahead = AsyncMock(return_value=4)

    out = await _with_ahead_count(db, _row("waitlisted", waitlist_position=5), "jwt")

    assert out.ahead_count == 4
    db.count_waitlist_ahead.assert_awaited_once_with("evt-1", 5, "jwt")


@pytest.mark.asyncio
async def test_confirmed_rsvp_does_not_query_the_queue() -> None:
    db = MagicMock()
    db.count_waitlist_ahead = AsyncMock()

    out = await _with_ahead_count(db, _row("confirmed"), "jwt")

    assert out.ahead_count is None
    db.count_waitlist_ahead.assert_not_awaited()


def test_row_to_rsvp_parses_timestamps() -> None:
    row = _row("called", called_at="2026-09-21T10:00:00Z", call_expires_at="2026-09-21T10:10:00Z")

    out = _row_to_rsvp(row)

    assert out.called_at is not None
    assert out.call_expires_at is not None
    assert out.call_expires_at > out.called_at


# ----------------------------------------------------------- creator gate --


@pytest.mark.asyncio
async def test_non_creator_is_refused() -> None:
    db = MagicMock()
    db.get_event_by_id = AsyncMock(return_value={"id": "evt-1", "creator_id": "someone-else"})

    with pytest.raises(HTTPException) as exc:
        await _require_creator(db, "evt-1", "user-1", "jwt")

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_missing_event_is_a_404_not_a_403() -> None:
    db = MagicMock()
    db.get_event_by_id = AsyncMock(return_value=None)

    with pytest.raises(HTTPException) as exc:
        await _require_creator(db, "evt-1", "user-1", "jwt")

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_creator_passes_and_gets_the_event_row() -> None:
    db = MagicMock()
    db.get_event_by_id = AsyncMock(return_value={"id": "evt-1", "creator_id": "user-1"})

    event = await _require_creator(db, "evt-1", "user-1", "jwt")

    assert event["id"] == "evt-1"


# ---------------------------------------------------- why the scan failed --


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_value", "fragment"),
    [
        ("waitlisted", "Ainda não é a vez"),
        ("no_show", "chamada desta pessoa expirou"),
        ("cancelled", "cancelou a inscrição"),
    ],
)
async def test_rejected_scan_explains_the_actual_reason(
    status_value: str, fragment: str
) -> None:
    # The doorkeeper needs to know WHY someone is being turned away, not just that
    # they are — the generic RPC raise is the same for all three.
    db = MagicMock()
    db.get_rsvp_by_id = AsyncMock(return_value={"status": status_value})

    exc = await _not_admissible_error(db, "rsvp-1", "evt-1", "jwt")

    assert exc.status_code == 409
    assert fragment in str(exc.detail)


@pytest.mark.asyncio
async def test_rejected_scan_with_a_vanished_rsvp_still_answers() -> None:
    db = MagicMock()
    db.get_rsvp_by_id = AsyncMock(return_value=None)

    exc = await _not_admissible_error(db, "rsvp-1", "evt-1", "jwt")

    assert exc.status_code == 409
