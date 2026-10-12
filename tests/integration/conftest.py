"""Shared fixtures for the Postgres integration tests.

Each test gets a throwaway database with just enough of the Supabase schema (roles,
the `extensions` schema with pgvector, a minimal `events` table) and then the real
migration files, in order. Skipped unless TEST_DATABASE_URL points at a Postgres 16+
server with the `vector` extension available (CI starts one).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "supabase" / "migrations"

# The migrations that touch `events`, in the order they run in production.
EVENT_MIGRATIONS = (
    "20260618000000_events_ingestion.sql",
    "20261009010000_match_events.sql",
    "20261011000000_places_catalog.sql",
    "20261012000000_trophy_templates_one_per_event.sql",
    "20261013000000_event_end_and_checkin_window.sql",
)

# The `events` columns that existed before any versioned migration (the table itself
# was created in the Supabase dashboard). Defaults match production.
BOOTSTRAP = """
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then create role service_role nologin; end if;
end $$;
create schema extensions;
create extension vector schema extensions;
create table public.events (
  id uuid primary key default gen_random_uuid(),
  title text not null default 'evento',
  category text,
  lat double precision not null,
  long double precision not null,
  event_date timestamptz,
  price numeric default 0.00,
  event_embedding extensions.vector(1536)
);
create table public.trophy_templates (
  id uuid primary key default gen_random_uuid(),
  event_id uuid references public.events (id) on delete cascade,
  name text not null,
  icon_url text,
  description text,
  created_at timestamptz default now()
);
"""


def apply_migrations(conn: Any, names: tuple[str, ...] = EVENT_MIGRATIONS) -> None:
    for name in names:
        conn.execute((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def empty_db() -> Iterator[Any]:
    """A fresh database with the bootstrap schema but no migrations applied."""
    psycopg = pytest.importorskip("psycopg")
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL not set")
    name = f"flyer_it_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f'create database "{name}"')
    url = psycopg.conninfo.make_conninfo(admin_url, dbname=name)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(BOOTSTRAP)
            yield conn
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(f'drop database if exists "{name}" with (force)')


@pytest.fixture
def db(empty_db: Any) -> Any:
    """A fresh database with every `events` migration applied."""
    apply_migrations(empty_db)
    return empty_db


@pytest.fixture
def migrate() -> Callable[..., None]:
    return apply_migrations
