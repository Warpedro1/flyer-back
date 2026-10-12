"""CPF and phone on profiles (migration 20261015000000): valid, unique, set at sign-up."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("psycopg")

MIGRATION = ("20261015000000_profile_cpf_phone.sql",)

# What exists in production before this migration (created in the dashboard).
STUBS = """
create schema if not exists auth;
create table auth.users (id uuid primary key, email text, raw_user_meta_data jsonb);
create table public.subscription_plans (id uuid primary key default gen_random_uuid(), name text);
insert into public.subscription_plans (name) values ('Free');
create table public.ai_chats (user_id uuid);
create table public.profiles (id uuid primary key, plan_id uuid, name text, email text);
"""
TRIGGER = """
create trigger on_auth_user_created after insert on auth.users
  for each row execute function public.handle_new_user();
"""


@pytest.fixture
def db(empty_db: Any, migrate: Callable[..., None]) -> Any:
    empty_db.execute(STUBS)
    migrate(empty_db, MIGRATION)
    empty_db.execute(TRIGGER)
    return empty_db


def _signup(db: Any, email: str, meta: dict[str, Any]) -> str:
    user_id = str(uuid.uuid4())
    db.execute(
        "insert into auth.users (id, email, raw_user_meta_data) values (%s, %s, %s::jsonb)",
        (user_id, email, json.dumps(meta)),
    )
    return user_id


def test_sign_up_copies_name_cpf_and_phone(db: Any) -> None:
    user = _signup(db, "ana@example.com", {"full_name": "Ana", "cpf": "52998224725", "phone": "+5581912345678"})

    row = db.execute("select name, email, cpf, phone from public.profiles where id = %s", (user,)).fetchone()
    assert row == ("Ana", "ana@example.com", "52998224725", "+5581912345678")
    assert db.execute("select count(*) from public.ai_chats").fetchone()[0] == 1


def test_google_sign_up_has_no_cpf_yet(db: Any) -> None:
    user = _signup(db, "g@example.com", {"name": "Gui"})

    assert db.execute("select name, cpf, phone from public.profiles where id = %s", (user,)).fetchone() == (
        "Gui",
        None,
        None,
    )


@pytest.mark.parametrize("field", ["cpf", "phone"])
def test_cpf_and_phone_are_unique(db: Any, field: str) -> None:
    meta = {"cpf": "52998224725", "phone": "+5581912345678"}
    _signup(db, "a@example.com", meta)

    other = {"cpf": "11144477735", "phone": "+5581999998888", field: meta[field]}
    with pytest.raises(Exception, match=f"profiles_{field}_key"):
        _signup(db, "b@example.com", other)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cpf", "52998224724"),  # wrong check digit
        ("cpf", "11111111111"),
        ("cpf", "5299822472"),
        ("phone", "+558131234567"),  # landline
        ("phone", "81912345678"),  # not E.164
    ],
)
def test_invalid_values_are_refused_by_the_database(db: Any, field: str, value: str) -> None:
    # Whoever calls Supabase directly, skipping the backend, still cannot store them.
    with pytest.raises(Exception, match=f"profiles_{field}_valid"):
        _signup(db, "x@example.com", {field: value})


def test_clients_cannot_call_the_cpf_check(db: Any) -> None:
    for role in ("anon", "authenticated"):
        allowed = db.execute(
            "select has_function_privilege(%s, 'public.is_valid_cpf(text)', 'execute')", (role,)
        ).fetchone()[0]
        assert allowed is False
