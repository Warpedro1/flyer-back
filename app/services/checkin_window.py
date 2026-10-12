"""When check-in is open (decisions 2026-10-12).

* Events: from `checkin_opens_at` (the start, unless the creator scheduled another
  time or pressed "open") until `ends_at`, unless the creator closed it earlier
  (`checkin_closed_at`). Without a start or an end there is no check-in.
* Places: during opening hours, in Recife time. Places whose hours are missing
  or unreadable are always open.

Pure and synchronous so it can be tested without a database.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from app.core.tz import LOCAL_TZ, parse_aware
from app.models.schemas import CheckinStatus


@dataclass(frozen=True)
class CheckinState:
    status: CheckinStatus
    opens_at: datetime | None = None
    closes_at: datetime | None = None


# ------------------------------------------------------------------ events --


def _event_state(row: dict[str, Any], now: datetime) -> CheckinState:
    start = parse_aware(row.get("event_date"))
    end = parse_aware(row.get("ends_at"))
    if start is None or end is None:
        return CheckinState("no_schedule")
    opens = parse_aware(row.get("checkin_opens_at")) or start
    closed = parse_aware(row.get("checkin_closed_at"))
    closes = min(end, closed) if closed is not None else end
    if closed is not None and now >= closed:
        # Closed by hand (even before the scheduled opening): "open now" clears it.
        return CheckinState("closed", opens_at=opens, closes_at=closes)
    if now < opens:
        return CheckinState("not_yet", opens_at=opens, closes_at=end)
    if now >= closes:
        return CheckinState("closed", opens_at=opens, closes_at=closes)
    return CheckinState("open", opens_at=opens, closes_at=closes)


# ------------------------------------------------------------------ places --

# Monday = 0, as `date.weekday()`.
_WEEKDAYS = {
    "segunda": 0,
    "terca": 1,
    "quarta": 2,
    "quinta": 3,
    "sexta": 4,
    "sabado": 5,
    "domingo": 6,
}
_RANGE_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*(?:to|a|-|–)\s*(\d{1,2}):(\d{2})\s*$")


def _fold(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return stripped.lower().strip()


def _weekday(day: str) -> int | None:
    folded = _fold(day)
    for prefix, number in _WEEKDAYS.items():
        if folded.startswith(prefix):
            return number
    return None


Interval = tuple[time, time | None]  # end None = until midnight; end <= start = past midnight


def _parse_hours(hours: str) -> list[Interval] | None:
    """Intervals for one day; [] = closed; None = unreadable."""
    folded = _fold(hours)
    if folded == "fechado":
        return []
    if "24 horas" in folded or "24 hours" in folded:
        return [(time(0, 0), None)]
    out: list[Interval] = []
    for part in hours.split(","):
        match = _RANGE_RE.match(part)
        if not match:
            return None
        h1, m1, h2, m2 = (int(g) for g in match.groups())
        if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59:
            return None
        start = time(h1, m1)
        end = None if (h2, m2) in {(0, 0), (24, 0)} else time(h2, m2)
        out.append((start, end))
    return out


def _schedule(row: dict[str, Any]) -> dict[int, list[Interval]] | None:
    """Opening intervals per weekday, or None when the place has no readable hours."""
    entries = row.get("opening_hours")
    if not isinstance(entries, list) or not entries:
        return None
    week: dict[int, list[Interval]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        day = _weekday(str(entry.get("day") or ""))
        intervals = _parse_hours(str(entry.get("hours") or ""))
        if day is None or intervals is None:
            return None
        week.setdefault(day, []).extend(intervals)
    return week


def _windows(week: dict[int, list[Interval]], day: date) -> list[tuple[datetime, datetime]]:
    """Concrete opening windows that start on ``day``, in Recife time."""
    out: list[tuple[datetime, datetime]] = []
    for start, end in week.get(day.weekday(), []):
        opens = datetime.combine(day, start, tzinfo=LOCAL_TZ)
        if end is None:
            closes = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=LOCAL_TZ)
        else:
            closes = datetime.combine(day, end, tzinfo=LOCAL_TZ)
            if closes <= opens:  # e.g. 18:00 to 02:00
                closes += timedelta(days=1)
        out.append((opens, closes))
    return out


def _place_state(row: dict[str, Any], now: datetime) -> CheckinState:
    week = _schedule(row)
    if week is None:
        return CheckinState("open")
    today = now.astimezone(LOCAL_TZ).date()
    # Yesterday's late shift may still be running after midnight.
    for day in (today - timedelta(days=1), today):
        for opens, closes in _windows(week, day):
            if opens <= now < closes:
                return CheckinState("open", opens_at=opens, closes_at=closes)
    return CheckinState("closed")


# ------------------------------------------------------------------- public --


def checkin_state(row: dict[str, Any], now: datetime) -> CheckinState:
    """Whether a user standing at ``row`` can check in at ``now`` (aware datetime)."""
    if row.get("kind") == "place":
        return _place_state(row, now)
    return _event_state(row, now)


def refusal_detail(state: CheckinState, *, is_place: bool) -> str:
    """User-facing reason (pt) for refusing a check-in outside the window."""
    if is_place:
        return "Este lugar está fechado agora: o check-in funciona no horário de funcionamento."
    if state.status == "no_schedule":
        return "Este evento ainda não tem início e fim: o check-in não está disponível."
    if state.status == "not_yet" and state.opens_at is not None:
        local = state.opens_at.astimezone(LOCAL_TZ)
        return f"O check-in abre às {local:%H:%M} de {local:%d/%m}."
    return "O check-in deste evento já fechou."
