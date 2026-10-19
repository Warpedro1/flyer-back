"""Event moderation (migration 20261018030000, #22 part A).

`moderation_stale` is a generated column: the md5 the app computes in
`moderation.event_moderation_hash` must be the one Postgres computes, or every row
looks unchecked forever. And discovery must leave out what the moderation refused.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.services.moderation import event_moderation_hash

pytest.importorskip("psycopg")

MIGRATION = ("20261018030000_event_moderation.sql",)
LAT, LNG = -8.0631, -34.8711


@pytest.fixture
def mdb(db: Any, migrate: Any) -> Any:
    db.execute("alter table public.events add column if not exists description text")
    migrate(db, MIGRATION)
    return db


def _event(db: Any, title: str, description: str | None, cover: str | None) -> str:
    event_id = str(uuid.uuid4())
    db.execute(
        "insert into public.events (id, title, description, cover_url, lat, long, event_date) "
        "values (%s, %s, %s, %s, %s, %s, %s)",
        (event_id, title, description, cover, LAT, LNG, datetime.now(timezone.utc) + timedelta(days=1)),
    )
    return event_id


def _stale(db: Any, event_id: str) -> bool:
    return db.execute("select moderation_stale from public.events where id = %s", (event_id,)).fetchone()[0]


def test_an_unchecked_row_is_stale(mdb: Any) -> None:
    assert _stale(mdb, _event(mdb, "Show", "No Recife", None)) is True


@pytest.mark.parametrize(
    ("title", "description", "cover"),
    [
        ("Forró de Lais Senna", None, "https://images.sympla.com.br/1.jpg"),
        ("Conferência “Da Visão ao Valor”", "Linha 1\n\nLinha 2… çãõ [contato removido]", None),
        ("Praia", "", ""),
    ],
)
def test_the_apps_hash_is_the_databases(mdb: Any, title: str, description: str | None, cover: str | None) -> None:
    event_id = _event(mdb, title, description, cover)

    mdb.execute(
        "update public.events set moderation_hash = %s where id = %s",
        (event_moderation_hash(title, description, cover), event_id),
    )

    assert _stale(mdb, event_id) is False


def test_a_new_cover_makes_it_stale_again(mdb: Any) -> None:
    event_id = _event(mdb, "Show", "No Recife", "https://c/1.jpg")
    mdb.execute(
        "update public.events set moderation_hash = %s where id = %s",
        (event_moderation_hash("Show", "No Recife", "https://c/1.jpg"), event_id),
    )

    mdb.execute("update public.events set cover_url = 'https://c/2.jpg' where id = %s", (event_id,))

    assert _stale(mdb, event_id) is True


def test_discovery_leaves_out_what_the_moderation_refused(mdb: Any) -> None:
    shown = _event(mdb, "Show", None, None)
    hidden = _event(mdb, "Outro", None, None)
    mdb.execute("update public.events set moderation_blocked = true where id = %s", (hidden,))

    rows = mdb.execute(
        "select id from public.match_events(null::extensions.vector, %s, %s, %s)", (LAT, LNG, 10.0)
    ).fetchall()

    assert {str(r[0]) for r in rows} == {shown}


def test_clients_still_cannot_call_discovery(mdb: Any) -> None:
    rows = mdb.execute(
        "select grantee from information_schema.routine_privileges "
        "where routine_schema = 'public' and routine_name = 'match_events' and privilege_type = 'EXECUTE'"
    ).fetchall()

    assert {r[0] for r in rows} & {"anon", "authenticated", "PUBLIC"} == set()


def test_the_cover_the_moderation_dropped_is_remembered_apart(mdb: Any) -> None:
    event_id = _event(mdb, "Show", "No Recife", None)

    mdb.execute(
        "update public.events set moderation_hash = %s, moderation_dropped_cover = 'https://c/1.jpg' where id = %s",
        (event_moderation_hash("Show", "No Recife", None), event_id),
    )

    # The row as stored (without that cover) is what the moderation saw.
    assert _stale(mdb, event_id) is False
