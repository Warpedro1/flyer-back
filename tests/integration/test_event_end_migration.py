"""Event end and check-in window columns (migration 20261013000000)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

pytest.importorskip("psycopg")

BEFORE = (
    "20260618000000_events_ingestion.sql",
    "20261009010000_match_events.sql",
    "20261011000000_places_catalog.sql",
    "20261012000000_trophy_templates_one_per_event.sql",
)
THIS = ("20261013000000_event_end_and_checkin_window.sql",)


def test_existing_dated_events_get_four_hours_and_open_at_the_start(
    empty_db: Any, migrate: Callable[..., None]
) -> None:
    # Rows imported before the end was stored: fim = início + 4 h (decision 2026-10-12).
    migrate(empty_db, BEFORE)
    empty_db.execute(
        "insert into public.events (lat, long, event_date) values (-8.06, -34.87, '2026-10-16 20:00-03')"
    )
    empty_db.execute("insert into public.events (lat, long, kind) values (-8.06, -34.87, 'place')")

    migrate(empty_db, THIS)

    rows = empty_db.execute(
        "select kind, ends_at - event_date, checkin_opens_at = event_date, checkin_closed_at "
        "from public.events order by kind"
    ).fetchall()
    event, place = rows
    assert str(event[1]) == "4:00:00"
    assert event[2] is True
    assert event[3] is None
    assert place[1:] == (None, None, None)


def test_an_event_cannot_end_before_it_starts(db: Any) -> None:
    with pytest.raises(Exception, match="events_ends_after_start"):
        db.execute(
            "insert into public.events (lat, long, event_date, ends_at) "
            "values (-8.06, -34.87, '2026-10-16 20:00-03', '2026-10-16 19:00-03')"
        )


def test_an_event_in_progress_is_still_discovered(db: Any) -> None:
    # Check-in opens at the start: an event that started an hour ago and ends in two
    # must still be found, or nobody can reach it to check in.
    now = datetime.now(timezone.utc)
    running = db.execute(
        "insert into public.events (lat, long, event_date, ends_at) values (%s, %s, %s, %s) returning id",
        (38.72, -9.14, now - timedelta(hours=1), now + timedelta(hours=2)),
    ).fetchone()[0]
    db.execute(
        "insert into public.events (lat, long, event_date, ends_at) values (%s, %s, %s, %s)",
        (38.72, -9.14, now - timedelta(hours=5), now - timedelta(hours=1)),
    )

    ids = [r[0] for r in db.execute("select id from public.match_events(null, 38.72, -9.14, 10)").fetchall()]

    assert ids == [running]
