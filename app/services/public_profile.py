"""What one person sees of another (#32, #24; decisions 2026-11-01).

`ProfileBrief` is the public identity: @username, nickname and photo. Never the name,
e-mail, CPF or phone (those stay with their owner). The photo lives in the private
avatars bucket and is read through a URL signed here, one Storage call per response;
a photo that cannot be signed is left out, never sent as its raw path (it carries the
user's id). Deleting photos is best effort too: a failure never fails the request.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

from app.core.config import settings
from app.models.schemas import ProfileBrief
from app.services import error_log

logger = logging.getLogger(__name__)


async def sign_avatars(db: Any, paths: Iterable[str | None]) -> dict[str, str]:
    """Signed URLs for the photos that exist; {} when Storage fails (recorded)."""
    wanted = sorted({p for p in paths if p})
    if not wanted:
        return {}
    try:
        return await db.sign_media_paths(wanted, bucket=settings.AVATAR_BUCKET)
    except Exception as exc:  # noqa: BLE001 — a list without photos still works
        logger.warning("Signing profile photos failed (%s)", type(exc).__name__)
        # No message: Storage URLs carry the user's folder.
        await error_log.record_error(
            source="api", code="storage_sign_error", error_type=type(exc).__name__, details={"stage": "avatars"}
        )
        return {}


def _brief(row: dict[str, Any], fallback_id: str | None, avatars: dict[str, str]) -> ProfileBrief:
    path = row.get("avatar_path")
    return ProfileBrief(
        id=str(row.get("id") or fallback_id or ""),
        username=row.get("username"),
        nickname=row.get("nickname"),
        avatar_url=avatars.get(path) if path else None,
    )


def brief_from_row(row: dict[str, Any] | None, avatars: dict[str, str], *, fallback_id: str | None = None) -> ProfileBrief:
    """A `profiles` row (id, username, nickname, avatar_path) with its photo already signed."""
    return _brief(row or {}, fallback_id, avatars)


async def public_profiles(db: Any, rows: Sequence[dict[str, Any]]) -> list[ProfileBrief]:
    """Many people at once, with one signing call for all their photos."""
    avatars = await sign_avatars(db, (row.get("avatar_path") for row in rows))
    return [_brief(row, None, avatars) for row in rows]


async def public_profile(db: Any, row: dict[str, Any] | None, *, fallback_id: str) -> ProfileBrief:
    avatars = await sign_avatars(db, [(row or {}).get("avatar_path")])
    return _brief(row or {}, fallback_id, avatars)


def display_label(row: dict[str, Any] | None, *, default: str = "Participante") -> str:
    """How messages name someone: "vulgo (@usuário)", "@usuário", or ``default``."""
    row = row or {}
    username = row.get("username")
    nickname = row.get("nickname")
    if username and nickname:
        return f"{nickname} (@{username})"
    if username:
        return f"@{username}"
    return nickname or default


async def discard_avatars(db: Any, paths: Iterable[str | None]) -> None:
    """Delete photos nobody uses any more (replaced, removed or refused)."""
    doomed = sorted({p for p in paths if p})
    if not doomed:
        return
    try:
        await db.delete_storage_objects(settings.AVATAR_BUCKET, doomed)
    except Exception as exc:  # noqa: BLE001 — a leftover file is not worth an error page
        logger.warning("Deleting profile photos failed (%s)", type(exc).__name__)
        # No message: the paths carry the user's id.
        await error_log.record_error(
            source="api", code="storage_delete_error", error_type=type(exc).__name__, details={"stage": "avatars"}
        )
