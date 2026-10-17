"""How each kind of event checks in, and the trophy a check-in gives.

Decisions (2026-10-17):
  * events created in the app check in at the door: the creator reads the
    attendee's QR and the backend checks they are at the venue (`qr`);
  * places check in by GPS during their opening hours (`gps`);
  * imported events are only informative: no check-in, no trophy (`none`).

Every check-in gives the event's trophy (decision 2026-10-12): without a custom one,
the default "Presença: <título>" is created on the first check-in.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from app.models.schemas import TrophyRead
from app.services.db_service import DBService, UniqueViolationError
from app.services.trophies import default_trophy_name

logger = logging.getLogger(__name__)

CheckinMethod = Literal["qr", "gps", "none"]


def checkin_method(row: dict[str, Any]) -> CheckinMethod:
    """`qr` for events created in the app, `gps` for places, `none` for imported events."""
    if row.get("kind") == "place":
        return "gps"
    # Same rule as editing (`api/events.py:_editable`): an imported row has a source.
    return "none" if row.get("source") else "qr"


class TrophyUnavailable(Exception):
    """The event's trophy template could not be prepared."""


async def sign_icons(db: DBService, icons: list[str | None]) -> dict[str, str]:
    """Icons are private Storage paths: the app gets short-lived signed URLs.

    Best-effort: a Storage failure must not fail a check-in already saved nor the
    whole trophy shelf; the app falls back to the Flyer icon.
    """
    paths = sorted({i for i in icons if i})
    if not paths:
        return {}
    try:
        return await db.sign_media_paths(paths)
    except Exception:
        logger.exception("Trophy icon signing failed (%d paths)", len(paths))
        return {}


def parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


async def event_trophy_template(db: DBService, event: dict[str, Any]) -> dict[str, Any]:
    """The event's trophy template, creating the default one on the first check-in."""
    event_id = str(event["id"])
    template = await db.get_trophy_template_by_event(event_id)
    if not template:
        template = await db.ensure_default_trophy_template(
            event_id, default_trophy_name(str(event.get("title") or "evento"))
        )
    if not template:
        raise TrophyUnavailable(event_id)
    return template


async def trophy_read(db: DBService, template: dict[str, Any], trophy_row: dict[str, Any]) -> TrophyRead:
    icon = template.get("icon_url")
    # A path that could not be signed is never sent: it is private and useless to the app.
    icon_url = (await sign_icons(db, [icon])).get(icon) if icon else None
    return TrophyRead(
        id=str(trophy_row.get("id", "")),
        template_id=str(template["id"]),
        name=str(template.get("name") or ""),
        description=template.get("description"),
        icon_url=icon_url,
        acquired_at=parse_dt(trophy_row.get("acquired_at")),
    )


async def award_checkin_trophy(
    db: DBService, event: dict[str, Any], user_id: str, jwt: str
) -> TrophyRead:
    """Give the event's trophy to ``user_id``; a trophy they already have comes back as is.

    Used by the door scan, which must not fail an entry because of a trophy they
    already collected.
    """
    template = await event_trophy_template(db, event)
    template_id = str(template["id"])
    try:
        row = await db.insert_trophy(user_id, template_id, jwt)
    except UniqueViolationError:
        row = await db.get_user_trophy_for_template(user_id, template_id) or {}
    return await trophy_read(db, template, row)
