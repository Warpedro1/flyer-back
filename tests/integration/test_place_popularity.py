"""Discovery without a profile puts what most people like first (decision 2026-10-19).

Migration 20261019000000: `events.reviews_count` (Google's review count), and
`match_events` without a query vector orders the events of broad appeal before the
others (each by date), then the places, most reviewed first. With a vector the
order stays by similarity.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.services.etl.taxonomy import BROAD_APPEAL_CATEGORIES

pytest.importorskip("psycopg")

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
    sql = (Path(__file__).resolve().parents[2] / "supabase" / "migrations" / MIGRATIONS[1]).read_text()
    listed = re.search(r"array\[([^\]]*)\]::text\[\]", sql)

    assert listed is not None
    assert set(re.findall(r"'([a-z_]+)'", listed.group(1))) == set(BROAD_APPEAL_CATEGORIES)
