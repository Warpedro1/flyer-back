"""Introduction tour seen (migration 20261018000000, Warpedro1/flyer#23)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("psycopg")

BEFORE = ("20261015000000_profile_cpf_phone.sql", "20261016000000_signup_pending.sql")
MIGRATION = ("20261018000000_profile_intro_seen.sql",)

STUBS = """
create schema if not exists auth;
create table auth.users (id uuid primary key, email text, raw_user_meta_data jsonb);
create table public.subscription_plans (id uuid primary key default gen_random_uuid(), name text);
insert into public.subscription_plans (name) values ('Free');
create table public.ai_chats (user_id uuid);
create table public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  plan_id uuid, name text, email text
);
"""


def _profile(db: Any, *, pending: bool) -> str:
    user_id = str(uuid.uuid4())
    db.execute("insert into auth.users (id, email) values (%s, %s)", (user_id, f"{user_id}@example.com"))
    db.execute(
        "insert into public.profiles (id, email, signup_pending) values (%s, %s, %s)",
        (user_id, f"{user_id}@example.com", pending),
    )
    return user_id


def _seen(db: Any, user_id: str) -> Any:
    return db.execute("select intro_seen_at from public.profiles where id = %s", (user_id,)).fetchone()[0]


@pytest.fixture
def db(empty_db: Any, migrate: Callable[..., None]) -> Any:
    empty_db.execute(STUBS)
    migrate(empty_db, BEFORE)
    return empty_db


def test_existing_accounts_never_see_the_tour(db: Any, migrate: Callable[..., None]) -> None:
    active = _profile(db, pending=False)
    pending = _profile(db, pending=True)

    migrate(db, MIGRATION)

    assert _seen(db, active) is not None
    assert _seen(db, pending) is None


def test_new_accounts_start_without_it(db: Any, migrate: Callable[..., None]) -> None:
    migrate(db, MIGRATION)

    assert _seen(db, _profile(db, pending=False)) is None


def test_the_migration_can_run_twice(db: Any, migrate: Callable[..., None]) -> None:
    migrate(db, MIGRATION)
    migrate(db, MIGRATION)
