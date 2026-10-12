"""Flyer's local time: Recife (UTC−03:00, no daylight saving time).

A fixed offset rather than `zoneinfo`: Recife has not had DST since 2019, and the
serverless runtime is not guaranteed to ship the tz database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

LOCAL_TZ = timezone(timedelta(hours=-3), "America/Recife")


def to_local_naive(value: datetime) -> datetime:
    """Recife wall-clock time without tzinfo. A naive value is already Recife time."""
    if value.tzinfo is None:
        return value
    return value.astimezone(LOCAL_TZ).replace(tzinfo=None)


def local_iso(value: datetime) -> str:
    """ISO string with the Recife offset, for a naive Recife wall-clock time."""
    return value.replace(tzinfo=LOCAL_TZ).isoformat()


def parse_aware(value: object) -> datetime | None:
    """A timestamp from the database (ISO string or datetime) as an aware datetime.

    Naive values are treated as UTC, which is how timestamptz columns come back.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
