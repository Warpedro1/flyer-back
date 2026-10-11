"""Places, event↔place links and discovery filters (migration 20261011000000).

Run against a real Postgres: the upsert conflict target and the filters live in SQL,
and the unit tests only see a mocked PostgREST.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

pytest.importorskip("psycopg")

# Recife, Marco Zero. ~0.009 degrees of latitude is ~1 km.
LAT, LNG = -8.0631, -34.8711


def _add(
    conn: Any,
    *,
    kind: str = "event",
    category: str | None = "musica_show",
    days: float | None = 1.0,
    price: str | None = "0",
    source: str | None = None,
    source_event_id: str | None = None,
    place_id: str | None = None,
) -> str:
    when = None if days is None else datetime.now(timezone.utc) + timedelta(days=days)
    row = conn.execute(
        "insert into public.events (kind, category, lat, long, event_date, price, source, source_event_id, place_id) "
        "values (%s, %s, %s, %s, %s, %s::numeric, %s, %s, %s) returning id",
        (kind, category, LAT, LNG, when, price, source, source_event_id, place_id),
    ).fetchone()
    return str(row[0])


def _place(conn: Any, **kwargs: Any) -> str:
    return _add(conn, kind="place", days=None, price=None, **kwargs)


def _ids(conn: Any, **filters: Any) -> set[str]:
    rows = conn.execute(
        "select id from public.match_events("
        "null::extensions.vector, %(lat)s, %(lng)s, 10, "
        "kinds => %(kinds)s, categories => %(categories)s, "
        "date_from => %(date_from)s, date_to => %(date_to)s, max_price => %(max_price)s)",
        {
            "lat": LAT,
            "lng": LNG,
            "kinds": filters.get("kinds"),
            "categories": filters.get("categories"),
            "date_from": filters.get("date_from"),
            "date_to": filters.get("date_to"),
            "max_price": filters.get("max_price"),
        },
    ).fetchall()
    return {str(r[0]) for r in rows}


def _in(days: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


# ----------------------------------------------------------------- filters --


def test_without_filters_places_and_events_come_together(db: Any) -> None:
    event = _add(db)
    place = _place(db)

    assert _ids(db) == {event, place}


@pytest.mark.parametrize(("kinds", "expected"), [(["event"], "event"), (["place"], "place")])
def test_filter_by_kind(db: Any, kinds: list[str], expected: str) -> None:
    ids = {"event": _add(db), "place": _place(db)}

    assert _ids(db, kinds=kinds) == {ids[expected]}


def test_filter_by_category_accepts_several(db: Any) -> None:
    show = _add(db, category="musica_show")
    beach = _place(db, category="praia_natureza")
    _place(db, category="academia_exercicio")

    assert _ids(db, categories=["musica_show", "praia_natureza"]) == {show, beach}


def test_date_filter_keeps_places_and_drops_undated_events(db: Any) -> None:
    # Decision 2026-10-11: places have no date and always pass the date filter.
    inside = _add(db, days=2)
    _add(db, days=10)
    _add(db, days=None)
    place = _place(db)

    assert _ids(db, date_from=_in(1), date_to=_in(3)) == {inside, place}


def test_price_filter_keeps_places_and_drops_unknown_prices(db: Any) -> None:
    # price = 0 is free; null is "the source did not say" and cannot be promised.
    free = _add(db, price="0")
    cheap = _add(db, price="40")
    _add(db, price="150")
    _add(db, price=None)
    place = _place(db)

    assert _ids(db, max_price=50) == {free, cheap, place}
    assert _ids(db, max_price=0) == {free, place}


# ------------------------------------------------------------------ places --


def test_an_event_can_point_at_a_place(db: Any) -> None:
    place = _place(db)
    event = _add(db, place_id=place)

    linked = db.execute("select place_id from public.events where id = %s", (event,)).fetchone()[0]

    assert str(linked) == place


def test_a_place_cannot_sit_inside_another_place(db: Any) -> None:
    outer = _place(db)

    with pytest.raises(Exception, match="events_place_id_only_on_events"):
        _place(db, place_id=outer)


def test_deleting_a_place_keeps_its_events(db: Any) -> None:
    place = _place(db)
    event = _add(db, place_id=place)

    db.execute("delete from public.events where id = %s", (place,))

    assert db.execute("select place_id from public.events where id = %s", (event,)).fetchone()[0] is None


# ------------------------------------------------------------------ upsert --


def test_the_upsert_conflict_target_postgrest_sends_works(db: Any) -> None:
    # PostgREST turns on_conflict=source,source_event_id into exactly this statement.
    # With the old partial unique index it failed with 42P10 on the first ingestion.
    upsert = (
        "insert into public.events (source, source_event_id, title, lat, long) "
        "values ('sympla', '123', %s, %s, %s) "
        "on conflict (source, source_event_id) do update set title = excluded.title"
    )
    db.execute(upsert, ("Primeira versão", LAT, LNG))
    db.execute(upsert, ("Segunda versão", LAT, LNG))

    rows = db.execute("select title from public.events where source = 'sympla'").fetchall()

    assert rows == [("Segunda versão",)]


def test_events_created_in_the_app_never_collide(db: Any) -> None:
    _add(db, source=None, source_event_id=None)
    _add(db, source=None, source_event_id=None)

    assert db.execute("select count(*) from public.events").fetchone()[0] == 2


# -------------------------------------------------------------- taxonomy --


def test_legacy_categories_move_to_the_new_taxonomy(
    empty_db: Any, migrate: Callable[..., None]
) -> None:
    migrate(empty_db, ("20260618000000_events_ingestion.sql", "20261009010000_match_events.sql"))
    for category in ("music", "Sport", "teste", None):
        empty_db.execute(
            "insert into public.events (category, lat, long) values (%s, %s, %s)",
            (category, LAT, LNG),
        )

    migrate(empty_db, ("20261011000000_places_catalog.sql",))

    rows = empty_db.execute("select category from public.events order by category nulls last").fetchall()
    assert [r[0] for r in rows] == ["desporto", "musica_show", "outros", None]


# ------------------------------------------------------------------ grants --


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_clients_cannot_call_the_new_signature(db: Any, role: str) -> None:
    rows = db.execute(
        "select has_function_privilege(%s, p.oid, 'execute'), pronargs "
        "from pg_proc p where p.proname = 'match_events'",
        (role,),
    ).fetchall()

    assert rows == [(False, 9)]
