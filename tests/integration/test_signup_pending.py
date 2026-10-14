"""Pending sign-ups and their tickets (migration 20261016000000, decision 2026-10-14)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("psycopg")

MIGRATIONS = ("20261015000000_profile_cpf_phone.sql", "20261016000000_signup_pending.sql")

# What exists in production before these migrations (created in the dashboard).
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
TRIGGER = """
create trigger on_auth_user_created after insert on auth.users
  for each row execute function public.handle_new_user();
"""


@pytest.fixture
def db(empty_db: Any, migrate: Callable[..., None]) -> Any:
    empty_db.execute(STUBS)
    migrate(empty_db, MIGRATIONS)
    empty_db.execute(TRIGGER)
    return empty_db


def _signup(db: Any, email: str, meta: dict[str, Any]) -> str:
    user_id = str(uuid.uuid4())
    db.execute(
        "insert into auth.users (id, email, raw_user_meta_data) values (%s, %s, %s::jsonb)",
        (user_id, email, json.dumps(meta)),
    )
    return user_id


def _pending(db: Any, user_id: str) -> bool:
    return db.execute("select signup_pending from public.profiles where id = %s", (user_id,)).fetchone()[0]


def test_a_flyer_sign_up_starts_pending(db: Any) -> None:
    user = _signup(
        db,
        "ana@example.com",
        {"name": "Ana", "cpf": "52998224725", "phone": "+5581912345678", "flyer_signup": True},
    )

    assert _pending(db, user) is True
    row = db.execute("select name, cpf, phone from public.profiles where id = %s", (user,)).fetchone()
    assert row == ("Ana", "52998224725", "+5581912345678")


def test_google_accounts_start_active(db: Any) -> None:
    assert _pending(db, _signup(db, "g@example.com", {"full_name": "Gui"})) is False


def test_existing_profiles_stay_active(empty_db: Any, migrate: Callable[..., None]) -> None:
    empty_db.execute(STUBS)
    user = str(uuid.uuid4())
    empty_db.execute("insert into auth.users (id, email) values (%s, 'old@example.com')", (user,))
    empty_db.execute("insert into public.profiles (id, email) values (%s, 'old@example.com')", (user,))

    migrate(empty_db, MIGRATIONS)

    assert _pending(empty_db, user) is False


def test_deleting_the_account_deletes_its_ticket(db: Any) -> None:
    user = _signup(db, "ana@example.com", {"flyer_signup": True})
    db.execute("insert into public.signup_tickets (user_id, ticket_hash) values (%s, 'h')", (user,))

    db.execute("delete from auth.users where id = %s", (user,))

    assert db.execute("select count(*) from public.signup_tickets").fetchone()[0] == 0


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_clients_cannot_touch_the_tickets(db: Any, role: str) -> None:
    for privilege in ("select", "insert", "update", "delete"):
        allowed = db.execute(
            "select has_table_privilege(%s, 'public.signup_tickets', %s)", (role, privilege)
        ).fetchone()[0]
        assert allowed is False, f"{role} has {privilege}"


def test_tickets_have_rls_on(db: Any) -> None:
    enabled = db.execute(
        "select relrowsecurity from pg_class where oid = 'public.signup_tickets'::regclass"
    ).fetchone()[0]
    assert enabled is True
