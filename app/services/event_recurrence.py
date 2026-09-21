"""Expand an EventRecurrence spec into concrete occurrence datetimes.

Pure/synchronous and easily unit-tested. Datetimes are naive to match how the app
stores/serializes `event_date` (the frontend sends `datetime-local`, i.e. no timezone).
Every mode is capped at ``MAX_OCCURRENCES`` as an anti-abuse guard.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from app.models.schemas import EventRecurrence, RecurrenceMode

MAX_OCCURRENCES = 60


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(hour=int(hh), minute=int(mm))


def expand_recurrence(base_dt: datetime | None, rec: EventRecurrence) -> list[datetime]:
    """Return the ordered list of occurrence datetimes for ``rec`` (validated upstream)."""
    if rec.mode == RecurrenceMode.count:
        base = base_dt or datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
        step = timedelta(days=1) if rec.every == "day" else timedelta(weeks=1)
        n = min(rec.occurrences or 0, MAX_OCCURRENCES)
        return [base + step * k for k in range(n)]

    if rec.mode == RecurrenceMode.weekly:
        # Validators guarantee weekdays/time_of_day/until are present in this mode.
        tod = _parse_hhmm(rec.time_of_day or "00:00")
        weekdays = set(rec.weekdays or [])
        cursor = base_dt.date() if base_dt else date.today()
        end = rec.until or cursor
        out: list[datetime] = []
        while cursor <= end and len(out) < MAX_OCCURRENCES:
            if cursor.weekday() in weekdays:
                out.append(datetime.combine(cursor, tod))
            cursor += timedelta(days=1)
        return out

    if rec.mode == RecurrenceMode.range:
        tod = _parse_hhmm(rec.time_of_day or "00:00")
        cursor = rec.start or date.today()
        end = rec.end or cursor
        out = []
        while cursor <= end and len(out) < MAX_OCCURRENCES:
            out.append(datetime.combine(cursor, tod))
            cursor += timedelta(days=1)
        return out

    return []
