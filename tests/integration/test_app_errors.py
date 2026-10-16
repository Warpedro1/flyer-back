"""Error records (migration 20261017000000, #25 phase 1): backend-only, kept 30 days."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("psycopg")

MIGRATION = ("20261017000000_app_errors.sql",)
INSERT = "insert into public.app_errors (source, code, message) values ('api', 'internal_error', 'boom')"


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_clients_cannot_read_or_write_it(empty_db: Any, migrate: Callable[..., None], role: str) -> None:
    migrate(empty_db, MIGRATION)

    for privilege in ("select", "insert", "update", "delete"):
        allowed = empty_db.execute(
            "select has_table_privilege(%s, 'public.app_errors', %s)", (role, privilege)
        ).fetchone()[0]
        assert allowed is False, f"{role} has {privilege}"


def test_the_backend_can_write_it(empty_db: Any, migrate: Callable[..., None]) -> None:
    migrate(empty_db, MIGRATION)

    allowed = empty_db.execute(
        "select has_table_privilege('service_role', 'public.app_errors', 'insert')"
    ).fetchone()[0]
    assert allowed is True


def test_rls_is_on(empty_db: Any, migrate: Callable[..., None]) -> None:
    migrate(empty_db, MIGRATION)

    enabled = empty_db.execute(
        "select relrowsecurity from pg_class where oid = 'public.app_errors'::regclass"
    ).fetchone()[0]
    assert enabled is True


def test_only_known_sources(empty_db: Any, migrate: Callable[..., None]) -> None:
    migrate(empty_db, MIGRATION)
    psycopg = pytest.importorskip("psycopg")

    with pytest.raises(psycopg.errors.CheckViolation):
        empty_db.execute("insert into public.app_errors (source) values ('other')")


def test_rows_older_than_30_days_go_on_the_next_insert(empty_db: Any, migrate: Callable[..., None]) -> None:
    migrate(empty_db, MIGRATION)
    empty_db.execute(
        "insert into public.app_errors (source, created_at) values "
        "('api', now() - interval '31 days'), ('api', now() - interval '29 days')"
    )

    empty_db.execute(INSERT)

    ages = empty_db.execute(
        "select extract(day from now() - created_at)::int from public.app_errors order by created_at"
    ).fetchall()
    assert [a[0] for a in ages] == [29, 0]


def test_the_cleanup_function_is_not_callable_by_clients(empty_db: Any, migrate: Callable[..., None]) -> None:
    migrate(empty_db, MIGRATION)

    for role in ("anon", "authenticated"):
        allowed = empty_db.execute(
            "select has_function_privilege(%s, 'public.purge_old_app_errors()', 'execute')", (role,)
        ).fetchone()[0]
        assert allowed is False, role
