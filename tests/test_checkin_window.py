"""When check-in is open (decisions 2026-10-12).

* Events: from the time the creator scheduled (or the moment they pressed "open")
  until the event ends, unless the creator closes it earlier.
* Places: during opening hours, Recife time. No hours = always open.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.tz import LOCAL_TZ
from app.services.checkin_window import checkin_state

UTC = timezone.utc


def _local(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=LOCAL_TZ)


# 16/10/2026 is a Friday.
START = _local(2026, 10, 16, 20)
END = _local(2026, 10, 16, 23)
EVENT = {
    "kind": "event",
    "event_date": START.isoformat(),
    "ends_at": END.isoformat(),
    "checkin_opens_at": START.isoformat(),
    "checkin_closed_at": None,
}


# ------------------------------------------------------------------ events --


def test_before_the_scheduled_opening_it_is_not_open_yet() -> None:
    state = checkin_state(EVENT, START - timedelta(minutes=1))

    assert state.status == "not_yet"
    assert state.opens_at == START


def test_between_opening_and_end_it_is_open_until_the_end() -> None:
    state = checkin_state(EVENT, START + timedelta(hours=1))

    assert state.status == "open"
    assert state.closes_at == END


def test_it_closes_by_itself_when_the_event_ends() -> None:
    assert checkin_state(EVENT, END).status == "closed"


def test_the_creator_can_open_it_earlier() -> None:
    opened = {**EVENT, "checkin_opens_at": (START - timedelta(hours=2)).isoformat()}

    assert checkin_state(opened, START - timedelta(hours=1)).status == "open"


def test_the_creator_can_close_it_before_the_end() -> None:
    closed = {**EVENT, "checkin_closed_at": (START + timedelta(minutes=30)).isoformat()}

    assert checkin_state(closed, START + timedelta(minutes=10)).status == "open"
    assert checkin_state(closed, START + timedelta(hours=1)).status == "closed"


def test_without_a_scheduled_opening_it_opens_at_the_start() -> None:
    row = {**EVENT, "checkin_opens_at": None}

    assert checkin_state(row, START - timedelta(minutes=1)).status == "not_yet"
    assert checkin_state(row, START).status == "open"


def test_an_event_without_dates_has_no_check_in() -> None:
    row = {"kind": "event", "event_date": None, "ends_at": None}

    assert checkin_state(row, START).status == "no_schedule"


def test_utc_timestamps_from_the_database_are_compared_correctly() -> None:
    # PostgREST returns timestamptz in UTC: 23:00Z is 20:00 in Recife.
    row = {**EVENT, "event_date": "2026-10-16T23:00:00+00:00", "checkin_opens_at": "2026-10-16T23:00:00+00:00",
           "ends_at": "2026-10-17T02:00:00+00:00"}

    assert checkin_state(row, datetime(2026, 10, 16, 22, 59, tzinfo=UTC)).status == "not_yet"
    assert checkin_state(row, datetime(2026, 10, 16, 23, 0, tzinfo=UTC)).status == "open"


# ------------------------------------------------------------------ places --


def _place(hours: list[tuple[str, str]] | None) -> dict[str, object]:
    return {
        "kind": "place",
        "opening_hours": None if hours is None else [{"day": d, "hours": h} for d, h in hours],
    }


WEEK = ["domingo", "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado"]


def _every_day(hours: str) -> list[tuple[str, str]]:
    return [(d, hours) for d in WEEK]


def test_a_place_is_open_during_its_opening_hours() -> None:
    place = _place(_every_day("11:30 to 15:00"))

    state = checkin_state(place, _local(2026, 10, 16, 12))
    assert state.status == "open"
    assert state.closes_at == _local(2026, 10, 16, 15)
    assert checkin_state(place, _local(2026, 10, 16, 15)).status == "closed"
    assert checkin_state(place, _local(2026, 10, 16, 11)).status == "closed"


def test_a_place_with_two_shifts() -> None:
    place = _place(_every_day("11:30 to 15:00, 17:00 to 23:00"))

    assert checkin_state(place, _local(2026, 10, 16, 16)).status == "closed"
    assert checkin_state(place, _local(2026, 10, 16, 18)).status == "open"


def test_a_place_closed_on_that_day() -> None:
    hours = _every_day("11:00 to 15:00")
    hours[5] = ("sexta-feira", "Fechado")

    assert checkin_state(_place(hours), _local(2026, 10, 16, 12)).status == "closed"


def test_a_place_open_24_hours() -> None:
    place = _place(_every_day("Atendimento 24 horas"))

    assert checkin_state(place, _local(2026, 10, 16, 3)).status == "open"


def test_closing_at_midnight_counts_until_the_end_of_the_day() -> None:
    place = _place(_every_day("11:30 to 00:00"))

    assert checkin_state(place, _local(2026, 10, 16, 23, 59)).status == "open"


def test_hours_past_midnight_continue_into_the_next_day() -> None:
    hours = _every_day("Fechado")
    hours[5] = ("sexta-feira", "18:00 to 02:00")

    # Saturday 01:00 belongs to Friday's shift.
    assert checkin_state(_place(hours), _local(2026, 10, 17, 1)).status == "open"
    assert checkin_state(_place(hours), _local(2026, 10, 17, 3)).status == "closed"


def test_hours_are_read_in_recife_time() -> None:
    place = _place(_every_day("11:30 to 15:00"))

    # 15:30 UTC is 12:30 in Recife.
    assert checkin_state(place, datetime(2026, 10, 16, 15, 30, tzinfo=UTC)).status == "open"


@pytest.mark.parametrize("hours", [None, [], [("segunda-feira", "consultar")]])
def test_a_place_without_readable_hours_is_always_open(hours: list[tuple[str, str]] | None) -> None:
    assert checkin_state(_place(hours), _local(2026, 10, 16, 4)).status == "open"
