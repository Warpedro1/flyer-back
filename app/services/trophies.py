"""Trophy defaults shared by event creation and check-in."""

from __future__ import annotations

TROPHY_NAME_MAX = 80


def default_trophy_name(event_title: str) -> str:
    """The name of the trophy an event gives when its creator did not customise one."""
    return f"Presença: {event_title}"[:TROPHY_NAME_MAX]
