"""DBService wrappers around the RSVP Postgres functions (mocked httpx client)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.db_service import DBService, RpcError, UniqueViolationError


def _ok(payload: object, headers: dict[str, str] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.is_success = True
    resp.json.return_value = payload
    resp.headers = headers or {}
    return resp


def _pg_error(message: str, code: str) -> MagicMock:
    resp = MagicMock()
    resp.is_success = False
    resp.json.return_value = {"code": code, "message": message}
    resp.headers = {}
    return resp


ROW = {
    "id": "rsvp-1",
    "event_id": "evt-1",
    "user_id": "user-1",
    "status": "waitlisted",
    "waitlist_position": 3,
}


@pytest.mark.asyncio
async def test_rsvp_join_calls_the_rpc_with_named_params() -> None:
    client = MagicMock()
    client.post = AsyncMock(return_value=_ok(ROW))
    db = DBService(client)

    row = await db.rsvp_join("evt-1", "user-1")

    assert row == ROW
    args, kwargs = client.post.call_args
    assert args[0].endswith("/rpc/rsvp_join")
    assert kwargs["json"] == {"p_event_id": "evt-1", "p_user_id": "user-1"}


@pytest.mark.asyncio
async def test_rsvp_rpc_accepts_object_list_or_null() -> None:
    client = MagicMock()
    db = DBService(client)

    client.post = AsyncMock(return_value=_ok(ROW))
    assert await db.rsvp_join("evt-1", "u") == ROW

    # PostgREST may wrap a single composite return in a list...
    client.post = AsyncMock(return_value=_ok([ROW]))
    assert await db.rsvp_join("evt-1", "u") == ROW

    # ...and an empty queue comes back as null, which is not an error.
    client.post = AsyncMock(return_value=_ok(None))
    assert await db.rsvp_call_next("evt-1", "creator") is None


@pytest.mark.asyncio
async def test_full_event_surfaces_as_rpc_error() -> None:
    client = MagicMock()
    client.post = AsyncMock(return_value=_pg_error("event_full", "P0001"))
    db = DBService(client)

    with pytest.raises(RpcError) as exc:
        await db.rsvp_join("evt-1", "user-1")

    assert exc.value.message == "event_full"
    assert exc.value.code == "P0001"


@pytest.mark.asyncio
async def test_replayed_qr_nonce_surfaces_as_unique_violation() -> None:
    # rsvp_admit inserts the jti; the 23505 IS the replay detection.
    client = MagicMock()
    client.post = AsyncMock(
        return_value=_pg_error("duplicate key value violates unique constraint", "23505")
    )
    db = DBService(client)

    with pytest.raises(UniqueViolationError):
        await db.rsvp_admit("rsvp-1", "evt-1", "jti-already-used")


@pytest.mark.asyncio
async def test_sweep_returns_the_number_of_expired_calls() -> None:
    client = MagicMock()
    client.post = AsyncMock(return_value=_ok(2))
    db = DBService(client)

    assert await db.sweep_expired_calls("evt-1") == 2
    args, kwargs = client.post.call_args
    assert args[0].endswith("/rpc/rsvp_sweep_expired_calls")


@pytest.mark.asyncio
async def test_count_by_status_reads_the_content_range_total() -> None:
    client = MagicMock()
    client.get = AsyncMock(return_value=_ok([], {"content-range": "0-0/7"}))
    db = DBService(client)

    total = await db.count_rsvps_by_status("evt-1", ["confirmed", "called"], "jwt")

    assert total == 7
    _args, kwargs = client.get.call_args
    assert kwargs["params"]["status"] == "in.(confirmed,called)"


@pytest.mark.asyncio
async def test_count_by_status_without_statuses_makes_no_request() -> None:
    client = MagicMock()
    client.get = AsyncMock()
    db = DBService(client)

    assert await db.count_rsvps_by_status("evt-1", [], "jwt") == 0
    client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_count_ahead_filters_on_position_and_waitlisted_only() -> None:
    client = MagicMock()
    client.get = AsyncMock(return_value=_ok([], {"content-range": "0-0/2"}))
    db = DBService(client)

    assert await db.count_waitlist_ahead("evt-1", 3, "jwt") == 2
    _args, kwargs = client.get.call_args
    assert kwargs["params"]["status"] == "eq.waitlisted"
    assert kwargs["params"]["waitlist_position"] == "lt.3"


@pytest.mark.asyncio
async def test_attendee_list_is_ordered_by_queue_position() -> None:
    client = MagicMock()
    client.get = AsyncMock(return_value=_ok([ROW]))
    db = DBService(client)

    rows = await db.get_event_rsvps("evt-1", "jwt")

    assert rows == [ROW]
    _args, kwargs = client.get.call_args
    assert kwargs["params"]["order"].startswith("waitlist_position.asc.nullslast")
    assert "profiles(id,name,email)" in kwargs["params"]["select"]


@pytest.mark.asyncio
async def test_rsvp_by_id_is_scoped_to_its_event() -> None:
    # An rsvp id read without its event would let one event's QR be checked
    # against another event's door.
    client = MagicMock()
    client.get = AsyncMock(return_value=_ok([ROW]))
    db = DBService(client)

    await db.get_rsvp_by_id("rsvp-1", "evt-1", "jwt")

    _args, kwargs = client.get.call_args
    assert kwargs["params"]["id"] == "eq.rsvp-1"
    assert kwargs["params"]["event_id"] == "eq.evt-1"
