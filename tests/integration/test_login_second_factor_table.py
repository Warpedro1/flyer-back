"""Verified login sessions (migration 20261014000000): backend-only table."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("psycopg")

MIGRATION = ("20261014000000_login_second_factor.sql",)


def test_a_session_is_verified_once(empty_db: Any, migrate: Callable[..., None]) -> None:
    migrate(empty_db, MIGRATION)
    session = "00000000-0000-0000-0000-000000000001"
    user = "00000000-0000-0000-0000-0000000000aa"
    upsert = (
        "insert into public.login_second_factor (session_id, user_id) values (%s, %s) "
        "on conflict (session_id) do nothing"
    )

    empty_db.execute(upsert, (session, user))
    empty_db.execute(upsert, (session, user))

    assert empty_db.execute("select count(*) from public.login_second_factor").fetchone()[0] == 1


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_clients_cannot_read_or_write_it(empty_db: Any, migrate: Callable[..., None], role: str) -> None:
    migrate(empty_db, MIGRATION)

    for privilege in ("select", "insert", "update", "delete"):
        allowed = empty_db.execute(
            "select has_table_privilege(%s, 'public.login_second_factor', %s)", (role, privilege)
        ).fetchone()[0]
        assert allowed is False, f"{role} has {privilege}"


def test_rls_is_on(empty_db: Any, migrate: Callable[..., None]) -> None:
    migrate(empty_db, MIGRATION)

    enabled = empty_db.execute(
        "select relrowsecurity from pg_class where oid = 'public.login_second_factor'::regclass"
    ).fetchone()[0]
    assert enabled is True
