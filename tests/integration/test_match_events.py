"""`match_events` against a real Postgres with pgvector.

The unit tests mock PostgREST, which is how a missing `match_events` went unnoticed
(issue #4). These run the migration file itself on a throwaway database. They are
skipped unless TEST_DATABASE_URL points at a server with the `vector` extension
available (CI starts one; locally: any Postgres 16 + pgvector).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

pytest.importorskip("psycopg")

DIMS = 1536
# Lisbon. ~0.009 degrees of latitude is ~1 km.
LAT, LNG = 38.7223, -9.1393


def _unit(index: int) -> str:
    vec = [0.0] * DIMS
    vec[index] = 1.0
    return "[" + ",".join(str(x) for x in vec) + "]"


def _mix(a: int, b: int, weight_a: float) -> str:
    vec = [0.0] * DIMS
    vec[a] = weight_a
    vec[b] = (1 - weight_a**2) ** 0.5
    return "[" + ",".join(str(x) for x in vec) + "]"


def _event(
    conn: Any,
    *,
    km_north: float = 0.0,
    days: float | None = 1.0,
    embedding: str | None = None,
) -> str:
    when = None if days is None else datetime.now(timezone.utc) + timedelta(days=days)
    row = conn.execute(
        "insert into public.events (lat, long, event_date, event_embedding) "
        "values (%s, %s, %s, %s::extensions.vector) returning id",
        (LAT + km_north / 111.0, LNG, when, embedding),
    ).fetchone()
    return str(row[0])


def _match(conn: Any, query: str | None, radius_km: float = 10.0) -> list[tuple[str, Any, float]]:
    rows = conn.execute(
        "select id, similarity, distance_km "
        "from public.match_events(%s::extensions.vector, %s, %s, %s)",
        (query, LAT, LNG, radius_km),
    ).fetchall()
    return [(str(r[0]), r[1], float(r[2])) for r in rows]


def test_only_events_inside_the_radius(db: Any) -> None:
    near = _event(db, km_north=2, embedding=_unit(0))
    _event(db, km_north=30, embedding=_unit(0))

    rows = _match(db, _unit(0), radius_km=10)

    assert [r[0] for r in rows] == [near]
    assert rows[0][2] == pytest.approx(2.0, abs=0.1)


def test_past_events_are_left_out_and_undated_ones_kept(db: Any) -> None:
    upcoming = _event(db, days=2, embedding=_unit(0))
    undated = _event(db, days=None, embedding=_unit(0))
    _event(db, days=-1, embedding=_unit(0))

    ids = {r[0] for r in _match(db, _unit(0))}

    assert ids == {upcoming, undated}


def test_personalised_order_is_by_similarity(db: Any) -> None:
    close = _event(db, embedding=_mix(0, 1, 0.9))
    far = _event(db, embedding=_unit(1))
    exact = _event(db, embedding=_unit(0))

    rows = _match(db, _unit(0))

    assert [r[0] for r in rows] == [exact, close, far]
    assert rows[0][1] == pytest.approx(1.0, abs=1e-5)


def test_events_without_embedding_come_last_by_date(db: Any) -> None:
    # Decision 2026-10-09: an event whose embedding failed must stay discoverable.
    later_plain = _event(db, days=5, embedding=None)
    sooner_plain = _event(db, days=2, embedding=None)
    weak_match = _event(db, days=9, embedding=_unit(1))

    rows = _match(db, _unit(0))

    assert [r[0] for r in rows] == [weak_match, sooner_plain, later_plain]
    assert rows[1][1] is None


def test_without_a_query_vector_it_orders_by_date(db: Any) -> None:
    # Fallback for users who never onboarded (#4 decision): nearby, soonest first.
    third = _event(db, days=None, embedding=_unit(0))
    second = _event(db, days=4, embedding=_unit(0))
    first = _event(db, days=1, embedding=None)

    rows = _match(db, None)

    assert [r[0] for r in rows] == [first, second, third]
    assert all(r[1] is None for r in rows)


def test_results_are_capped_at_200(db: Any) -> None:
    db.execute(
        "insert into public.events (lat, long, event_date) "
        "select %s, %s, now() + interval '1 day' from generate_series(1, 205)",
        (LAT, LNG),
    )

    assert len(_match(db, None)) == 200


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_clients_cannot_call_it(db: Any, role: str) -> None:
    rows = db.execute(
        "select has_function_privilege(%s, p.oid, 'execute') "
        "from pg_proc p where p.proname = 'match_events'",
        (role,),
    ).fetchall()

    # Exactly one overload, or PostgREST cannot tell which one to call.
    assert [r[0] for r in rows] == [False]


def test_service_role_can_call_it_with_a_pinned_search_path(db: Any) -> None:
    allowed, config = db.execute(
        "select has_function_privilege('service_role', p.oid, 'execute'), p.proconfig "
        "from pg_proc p where p.proname = 'match_events'"
    ).fetchone()

    assert allowed is True
    assert config and any(c.startswith("search_path=") for c in config)
