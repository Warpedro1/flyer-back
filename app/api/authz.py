"""Ownership checks for routes.

The API reaches Supabase with the service_role key (see `DBService._user_headers`),
so Row Level Security never runs on this path. Whatever a route does not check
here, nobody checks: every read or write of someone else's data must go through
one of these helpers, or filter by the `user_id` taken from the token.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException, status

from app.services.db_service import DBService

_NOT_YOURS = "Sem permissão para este recurso."


def require_owner(owner_id: Any, user_id: str, *, detail: str = _NOT_YOURS) -> None:
    """Reject unless the resource's owner is the authenticated user.

    A missing owner counts as someone else: rows without one (e.g. ingested
    events) belong to nobody, not to whoever happens to ask.
    """
    if not owner_id or str(owner_id) != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


async def require_event_creator(
    db: DBService, event_id: str, user_id: str, jwt: str
) -> dict[str, Any]:
    """Load the event and reject anyone who is not its creator."""
    event = await db.get_event_by_id(event_id, jwt)
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Evento não encontrado."
        )
    require_owner(
        event.get("creator_id"), user_id, detail="Só o criador do evento pode fazer isto."
    )
    return event


def _is_absolute_url(path: str) -> bool:
    return path.startswith("http://") or path.startswith("https://")


def require_media_paths_owned(user_id: str, paths: Iterable[str]) -> None:
    """Reject Storage paths outside the user's own `{user_id}/` folder.

    The bucket is private and the backend signs read URLs with service_role, so
    attaching someone else's path to an event would publish their file. Absolute
    URLs (legacy media) are never signed and pass through unchanged.
    """
    for path in paths:
        if _is_absolute_url(path):
            continue
        folder, _, rest = path.partition("/")
        segments = rest.split("/")
        if folder != user_id or not rest or any(s in {"", ".", ".."} for s in segments):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Só podes anexar ficheiros que tu próprio enviaste.",
            )
