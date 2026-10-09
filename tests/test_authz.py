"""Central ownership checks (app/api/authz.py).

The API talks to Supabase with the service_role key, so RLS never runs on this
path: these helpers are the only thing standing between a user and someone
else's rows.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.api.authz import require_event_creator, require_media_paths_owned, require_owner

# ---------------------------------------------------------------- owner --


def test_owner_passes() -> None:
    require_owner("user-1", "user-1")


@pytest.mark.parametrize("owner_id", ["someone-else", None, ""])
def test_anyone_else_is_refused(owner_id: str | None) -> None:
    # A row without an owner belongs to nobody, not to whoever asks.
    with pytest.raises(HTTPException) as exc:
        require_owner(owner_id, "user-1")

    assert exc.value.status_code == 403


def test_refusal_detail_can_be_specific() -> None:
    with pytest.raises(HTTPException) as exc:
        require_owner("someone-else", "user-1", detail="Não é teu.")

    assert exc.value.detail == "Não é teu."


# --------------------------------------------------------- event creator --


async def test_non_creator_is_refused() -> None:
    db = MagicMock()
    db.get_event_by_id = AsyncMock(return_value={"id": "evt-1", "creator_id": "someone-else"})

    with pytest.raises(HTTPException) as exc:
        await require_event_creator(db, "evt-1", "user-1", "jwt")

    assert exc.value.status_code == 403
    assert exc.value.detail == "Só o criador do evento pode fazer isto."


async def test_missing_event_is_a_404_not_a_403() -> None:
    db = MagicMock()
    db.get_event_by_id = AsyncMock(return_value=None)

    with pytest.raises(HTTPException) as exc:
        await require_event_creator(db, "evt-1", "user-1", "jwt")

    assert exc.value.status_code == 404


async def test_creator_passes_and_gets_the_event_row() -> None:
    db = MagicMock()
    db.get_event_by_id = AsyncMock(return_value={"id": "evt-1", "creator_id": "user-1"})

    event = await require_event_creator(db, "evt-1", "user-1", "jwt")

    assert event["id"] == "evt-1"


async def test_event_without_creator_is_nobodys() -> None:
    # Ingested events (ETL) have no creator; nobody may manage their queue.
    db = MagicMock()
    db.get_event_by_id = AsyncMock(return_value={"id": "evt-1", "creator_id": None})

    with pytest.raises(HTTPException) as exc:
        await require_event_creator(db, "evt-1", "user-1", "jwt")

    assert exc.value.status_code == 403


# ----------------------------------------------------------------- media --


def test_own_storage_paths_pass() -> None:
    require_media_paths_owned("user-1", ["user-1/a.jpg", "user-1/b.mp4"])


def test_absolute_urls_pass_through() -> None:
    # Legacy media are full URLs: they are never signed, so they expose nothing.
    require_media_paths_owned("user-1", ["https://cdn.example.com/a.jpg"])


@pytest.mark.parametrize(
    "path",
    [
        "user-2/a.jpg",  # someone else's folder
        "a.jpg",  # bucket root
        "user-1",  # the folder itself, no file
        "user-10/a.jpg",  # prefix of another id is not the same folder
        "user-1/../user-2/a.jpg",  # climbs out of the own folder
        "/user-1/a.jpg",  # leading slash
        "user-1//a.jpg",  # empty segment
    ],
)
def test_any_other_path_is_refused(path: str) -> None:
    # The backend signs these with service_role, so accepting someone else's path
    # would hand out a readable URL for a file in a private bucket.
    with pytest.raises(HTTPException) as exc:
        require_media_paths_owned("user-1", ["user-1/ok.jpg", path])

    assert exc.value.status_code == 403
