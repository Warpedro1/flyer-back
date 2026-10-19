"""Discovery without a profile puts what most people like first (decision 2026-10-19).

Migration 20261019000000: `events.reviews_count` (Google's review count), and
`match_events` without a query vector orders the events of broad appeal before the
others (each by date), then the places, most reviewed first. With a vector the
order stays by similarity.
"""

from __future__ import annotations

import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.services.etl.taxonomy import BROAD_APPEAL_CATEGORIES

pytest.importorskip("psycopg")

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
MIGRATIONS = ("20261018030000_event_moderation.sql", "20261019000000_place_popularity.sql")
DIMS = 1536
LAT, LNG = -8.0631, -34.8711


@pytest.fixture
def pdb(db: Any, migrate: Any) -> Any:
    db.execute("alter table public.events add column if not exists description text")
    migrate(db, MIGRATIONS)
    return db


def _unit(index: int) -> str:
    vec = [0.0] * DIMS
    vec[index] = 1.0
    return "[" + ",".join(str(x) for x in vec) + "]"


def _row(
    conn: Any,
    *,
    kind: str = "event",
    category: str | None = None,
    days: float | None = 1.0,
    reviews: int | None = None,
    rating: str | None = None,
    embedding: str | None = None,
) -> str:
    when = None if days is None else datetime.now(timezone.utc) + timedelta(days=days)
    row = conn.execute(
        "insert into public.events (kind, category, lat, long, event_date, reviews_count, rating, event_embedding) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s::extensions.vector) returning id",
        (kind, category, LAT, LNG, when, reviews, rating, embedding),
    ).fetchone()
    return str(row[0])


def _place(conn: Any, **kwargs: Any) -> str:
    return _row(conn, kind="place", days=None, **kwargs)


def _match(conn: Any, query: str | None) -> list[str]:
    rows = conn.execute(
        "select id from public.match_events(%s::extensions.vector, %s, %s, %s)",
        (query, LAT, LNG, 10.0),
    ).fetchall()
    return [str(r[0]) for r in rows]


def test_without_a_profile_the_most_reviewed_places_come_first(pdb: Any) -> None:
    unknown = _place(pdb, category="jogos")
    few = _place(pdb, category="praia_natureza", reviews=120, rating="4.9")
    many = _place(pdb, category="praia_natureza", reviews=9000, rating="4.6")
    same_reviews_better = _place(pdb, category="arte_cultura", reviews=120, rating="4.95")

    assert _match(pdb, None) == [many, same_reviews_better, few, unknown]


def test_without_a_profile_broad_events_come_first_each_group_by_date(pdb: Any) -> None:
    congress_soon = _row(pdb, category="tecnologia_negocios", days=1)
    show_later = _row(pdb, category="musica_show", days=5)
    play_soon = _row(pdb, category="teatro_cinema", days=2)
    talk_later = _row(pdb, category="palestra_conhecimento", days=3)
    park = _place(pdb, category="praia_natureza", reviews=5000)

    assert _match(pdb, None) == [play_soon, show_later, congress_soon, talk_later, park]


def test_with_a_profile_the_order_stays_by_similarity(pdb: Any) -> None:
    popular_far = _place(pdb, category="praia_natureza", reviews=9000, embedding=_unit(1))
    niche_close = _row(pdb, category="tecnologia_negocios", days=3, embedding=_unit(0))

    assert _match(pdb, _unit(0)) == [niche_close, popular_far]


# About 1 km of longitude at Recife's latitude. The query's bounding box only limits
# the latitude, so rows to the east or west are what the radius itself must leave out.
KM_EAST = 1 / 110.2


def test_rows_outside_the_radius_are_left_out(pdb: Any) -> None:
    near = _place(pdb, reviews=10)
    pdb.execute(
        "insert into public.events (kind, lat, long, reviews_count) values ('place', %s, %s, 9000)",
        (LAT, LNG + 20 * KM_EAST),
    )

    assert _match(pdb, None) == [near]


def _previous_function(conn: Any) -> None:
    """The match_events this migration replaces, side by side as match_events_before."""
    sql = (MIGRATIONS_DIR / MIGRATIONS[0]).read_text()
    start = sql.index("create or replace function public.match_events(")
    end = sql.index("$fn$;", start) + len("$fn$;")
    conn.execute(sql[start:end].replace("public.match_events(", "public.match_events_before(", 1))


def _mixed_catalogue(conn: Any, rows: int = 60) -> None:
    rng = random.Random(20261019)
    vectors = [_unit(0), _unit(1), _unit(2), None]
    categories = ["musica_show", "tecnologia_negocios", "praia_natureza", "jogos", None]
    start = datetime(2099, 1, 1, tzinfo=timezone.utc)
    for _ in range(rows):
        kind = rng.choice(["event", "place"])
        when = None if kind == "place" else start + timedelta(days=rng.choice([1, 1, 5]))
        ended = kind == "event" and rng.random() < 0.15
        if ended:
            when = datetime.now(timezone.utc) - timedelta(days=2)
        conn.execute(
            "insert into public.events (kind, category, lat, long, event_date, ends_at, reviews_count, rating, price, "
            "moderation_blocked, event_embedding) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::extensions.vector)",
            (
                kind,
                rng.choice(categories),
                # Equal distances and dates on purpose: the ties must break the same way;
                # some rows lie east of the radius, inside the latitude box.
                LAT,
                LNG + rng.choice([0.0, 1.0, 1.0, 4.0, 15.0]) * KM_EAST,
                when,
                when + timedelta(hours=1) if ended and when else None,
                rng.choice([None, 50, 50, 3000]) if kind == "place" else None,
                rng.choice([None, "4.20", "4.80"]),
                rng.choice([None, 0, 40]),
                rng.random() < 0.1,
                rng.choice(vectors),
            ),
        )


@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"kinds": ["place"]},
        {"categories": ["musica_show", "praia_natureza"]},
        {"max_price": 10},
        {"date_from": datetime(2099, 1, 3, tzinfo=timezone.utc)},
        {"date_to": datetime(2099, 1, 3, tzinfo=timezone.utc)},
    ],
)
def test_with_a_profile_the_order_is_exactly_the_previous_one(pdb: Any, filters: dict[str, Any]) -> None:
    _previous_function(pdb)
    # More rows than the 200 cap: the same order must also cut at the same row.
    _mixed_catalogue(pdb, rows=320)
    args = ", ".join(f"{name} => %({name})s" for name in filters)
    tail = f", {args}" if args else ""

    for query in (_unit(0), _unit(1), _unit(7)):
        params = {"q": query, "lat": LAT, "lng": LNG, **filters}
        call = "select id, similarity, distance_km from public.{fn}(%(q)s::extensions.vector, %(lat)s, %(lng)s, 10.0" + tail + ")"
        before = pdb.execute(call.format(fn="match_events_before"), params).fetchall()
        after = pdb.execute(call.format(fn="match_events"), params).fetchall()

        assert after == before
        assert before
    full = pdb.execute(
        "select count(*) from public.match_events_before(%s::extensions.vector, %s, %s, 10.0)", (_unit(0), LAT, LNG)
    ).fetchone()[0]
    assert full == 200


def test_without_a_profile_the_same_rows_come_only_in_another_order(pdb: Any) -> None:
    _previous_function(pdb)
    # Under the 200 cap, where the two orders keep the same rows.
    _mixed_catalogue(pdb)

    before = pdb.execute("select id from public.match_events_before(null, %s, %s, 10.0)", (LAT, LNG)).fetchall()
    after = pdb.execute("select id from public.match_events(null, %s, %s, 10.0)", (LAT, LNG)).fetchall()

    assert sorted(after) == sorted(before)
    assert len(before) < 200


def test_without_a_profile_events_without_a_category_go_with_the_others_by_date(pdb: Any) -> None:
    talk = _row(pdb, category="palestra_conhecimento", days=1)
    unknown = _row(pdb, category=None, days=2)
    course = _row(pdb, category="cursos_oficinas", days=3)
    show = _row(pdb, category="musica_show", days=4)

    assert _match(pdb, None) == [show, talk, unknown, course]


def test_reviews_count_cannot_be_negative(pdb: Any) -> None:
    psycopg = pytest.importorskip("psycopg")

    with pytest.raises(psycopg.errors.CheckViolation):
        _place(pdb, reviews=-1)


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_clients_still_cannot_call_discovery(pdb: Any, role: str) -> None:
    rows = pdb.execute(
        "select has_function_privilege(%s, p.oid, 'execute') from pg_proc p where p.proname = 'match_events'",
        (role,),
    ).fetchall()

    assert [r[0] for r in rows] == [False]


def test_the_service_role_calls_it_with_a_pinned_search_path(pdb: Any) -> None:
    allowed, config = pdb.execute(
        "select has_function_privilege('service_role', p.oid, 'execute'), p.proconfig "
        "from pg_proc p where p.proname = 'match_events'"
    ).fetchone()

    assert allowed is True
    assert config and any(c.startswith("search_path=") for c in config)


def test_the_migrations_broad_list_is_the_apps() -> None:
    sql = (MIGRATIONS_DIR / MIGRATIONS[1]).read_text()
    listed = re.search(r"array\[([^\]]*)\]::text\[\]", sql)

    assert listed is not None
    assert set(re.findall(r"'([a-z_]+)'", listed.group(1))) == set(BROAD_APPEAL_CATEGORIES)
