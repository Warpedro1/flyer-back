"""Recurrence expansion + schema validation for recurring events."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from app.models.schemas import EventRecurrence
from app.services.event_recurrence import MAX_OCCURRENCES, expand_recurrence


def test_count_daily_expands_n_times() -> None:
    base = datetime(2026, 7, 20, 20, 0)  # noqa: DTZ001 (naive matches app storage)
    rec = EventRecurrence(mode="count", every="day", occurrences=3)

    out = expand_recurrence(base, rec)

    assert out == [
        datetime(2026, 7, 20, 20, 0),
        datetime(2026, 7, 21, 20, 0),
        datetime(2026, 7, 22, 20, 0),
    ]


def test_count_weekly_steps_by_seven_days() -> None:
    base = datetime(2026, 7, 20, 18, 30)
    rec = EventRecurrence(mode="count", every="week", occurrences=4)

    out = expand_recurrence(base, rec)

    assert [d.date() for d in out] == [
        date(2026, 7, 20),
        date(2026, 7, 27),
        date(2026, 8, 3),
        date(2026, 8, 10),
    ]
    assert all(d.hour == 18 and d.minute == 30 for d in out)


def test_weekly_emits_selected_weekdays_at_time() -> None:
    base = datetime(2026, 7, 20, 12, 0)  # Monday 2026-07-20
    # Monday=0, Wednesday=2, Friday=4; run for two weeks.
    rec = EventRecurrence(
        mode="weekly",
        weekdays=[0, 2, 4],
        time_of_day="20:00",
        until=date(2026, 8, 2),
    )

    out = expand_recurrence(base, rec)

    assert [d.date() for d in out] == [
        date(2026, 7, 20),  # Mon
        date(2026, 7, 22),  # Wed
        date(2026, 7, 24),  # Fri
        date(2026, 7, 27),  # Mon
        date(2026, 7, 29),  # Wed
        date(2026, 7, 31),  # Fri
    ]
    assert all(d.hour == 20 and d.minute == 0 for d in out)


def test_range_emits_daily_between_dates() -> None:
    rec = EventRecurrence(
        mode="range",
        start=date(2026, 9, 1),
        end=date(2026, 9, 5),
        time_of_day="09:15",
    )

    out = expand_recurrence(None, rec)

    assert len(out) == 5
    assert out[0] == datetime(2026, 9, 1, 9, 15)
    assert out[-1] == datetime(2026, 9, 5, 9, 15)


def test_range_is_capped_at_max_occurrences() -> None:
    rec = EventRecurrence(
        mode="range",
        start=date(2026, 1, 1),
        end=date(2026, 12, 31),
        time_of_day="10:00",
    )

    out = expand_recurrence(None, rec)

    assert len(out) == MAX_OCCURRENCES


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "count", "occurrences": 3},  # missing 'every'
        {"mode": "count", "every": "day"},  # missing 'occurrences'
        {"mode": "weekly", "weekdays": [0], "time_of_day": "20:00"},  # missing 'until'
        {"mode": "weekly", "time_of_day": "20:00", "until": "2026-08-01"},  # missing weekdays
        {"mode": "range", "start": "2026-09-05", "end": "2026-09-01", "time_of_day": "09:00"},  # end<start
        {"mode": "range", "start": "2026-09-01", "end": "2026-09-05", "time_of_day": "25:00"},  # bad time
        {"mode": "weekly", "weekdays": [9], "time_of_day": "20:00", "until": "2026-08-01"},  # bad weekday
    ],
)
def test_invalid_recurrence_specs_are_rejected(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        EventRecurrence(**kwargs)
