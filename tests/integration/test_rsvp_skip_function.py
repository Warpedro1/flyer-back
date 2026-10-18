"""Pular a vez (migration 20261018010000, #5 part 1).

Only someone already called can be skipped, only by the event's creator. They become
`no_show` (so `rsvp_recall` still works) and, with `auto_call_next`, the next in line
is called at once. Clients cannot call the function at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("psycopg")

MIGRATIONS = (
    "20260921000000_event_capacity_waitlist.sql",
    "20260922000000_rsvp_revoke_execute_from_client_roles.sql",
    "20261018010000_rsvp_skip.sql",
)

# What the capacity migration expects from production (dashboard-made objects).
STUBS = """
create schema if not exists auth;
create or replace function auth.uid() returns uuid language sql stable as 'select null::uuid';
alter table public.events add column if not exists creator_id uuid;
alter table public.events add column if not exists attendee_count integer not null default 0;
create table public.profiles (id uuid primary key);
"""

CREATOR = str(uuid.uuid4())


@pytest.fixture
def db(empty_db: Any, migrate: Callable[..., None]) -> Any:
    empty_db.execute(STUBS)
    migrate(empty_db, MIGRATIONS)
    return empty_db


def _event(db: Any, *, auto: bool = True) -> str:
    event_id = str(uuid.uuid4())
    db.execute("insert into public.profiles (id) values (%s) on conflict do nothing", (CREATOR,))
    db.execute(
        "insert into public.events (id, lat, long, creator_id, capacity, auto_call_next) values (%s, -8, -34, %s, 1, %s)",
        (event_id, CREATOR, auto),
    )
    return event_id


def _rsvp(db: Any, event_id: str, status: str, position: int | None = None) -> str:
    user_id = str(uuid.uuid4())
    db.execute("insert into public.profiles (id) values (%s)", (user_id,))
    row = db.execute(
        """insert into public.event_rsvps (event_id, user_id, status, waitlist_position, called_at, call_expires_at)
           values (%s, %s, %s, %s, case when %s = 'called' then now() end,
                   case when %s = 'called' then now() + interval '10 minutes' end)
           returning id""",
        (event_id, user_id, status, position, status, status),
    ).fetchone()
    return str(row[0])


def _status(db: Any, rsvp_id: str) -> str:
    return db.execute("select status::text from public.event_rsvps where id = %s", (rsvp_id,)).fetchone()[0]


def _skip(db: Any, event_id: str, rsvp_id: str, creator: str = CREATOR) -> Any:
    return db.execute("select * from public.rsvp_skip(%s, %s, %s)", (event_id, rsvp_id, creator)).fetchone()


def test_skipping_calls_the_next_in_line(db: Any) -> None:
    event = _event(db)
    called = _rsvp(db, event, "called")
    first = _rsvp(db, event, "waitlisted", 1)
    second = _rsvp(db, event, "waitlisted", 2)

    _skip(db, event, called)

    assert _status(db, called) == "no_show"
    assert _status(db, first) == "called"
    assert _status(db, second) == "waitlisted"
    no_shows = db.execute("select no_show_count from public.event_rsvps where id = %s", (called,)).fetchone()[0]
    assert no_shows == 1


def test_without_auto_call_nobody_is_called(db: Any) -> None:
    event = _event(db, auto=False)
    called = _rsvp(db, event, "called")
    waiting = _rsvp(db, event, "waitlisted", 1)

    _skip(db, event, called)

    assert _status(db, called) == "no_show"
    assert _status(db, waiting) == "waitlisted"


def test_a_skipped_person_can_be_recalled(db: Any) -> None:
    event = _event(db, auto=False)
    called = _rsvp(db, event, "called")
    _skip(db, event, called)

    db.execute("select * from public.rsvp_recall(%s, %s, %s)", (event, called, CREATOR))

    assert _status(db, called) == "called"


@pytest.mark.parametrize("status", ["waitlisted", "confirmed", "admitted", "no_show", "cancelled"])
def test_only_someone_called_can_be_skipped(db: Any, status: str) -> None:
    event = _event(db)
    rsvp = _rsvp(db, event, status, 1 if status == "waitlisted" else None)

    with pytest.raises(Exception, match="rsvp_not_skippable"):
        _skip(db, event, rsvp)


def test_only_the_creator_can_skip(db: Any) -> None:
    event = _event(db)
    called = _rsvp(db, event, "called")

    with pytest.raises(Exception, match="not_event_creator"):
        _skip(db, event, called, creator=str(uuid.uuid4()))
    assert _status(db, called) == "called"


@pytest.mark.parametrize("role", ["anon", "authenticated", "public"])
def test_clients_cannot_execute_it(db: Any, role: str) -> None:
    allowed = db.execute(
        "select has_function_privilege(%s, 'public.rsvp_skip(uuid, uuid, uuid)', 'execute')",
        (role,),
    ).fetchone()[0]
    assert allowed is False


def test_the_backend_can_execute_it(db: Any) -> None:
    allowed = db.execute(
        "select has_function_privilege('service_role', 'public.rsvp_skip(uuid, uuid, uuid)', 'execute')"
    ).fetchone()[0]
    assert allowed is True
