"""Taste agent keywords (migration 20261018020000, #31 and #22 B).

`events.keywords_stale` is a generated column: the md5 the app computes in
`taste_agent.event_content_hash` must be the one Postgres computes, or every event
looks stale forever and the reprocessing never ends.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.services.taste_agent import event_content_hash

pytest.importorskip("psycopg")

MIGRATION = ("20261018020000_taste_keywords.sql",)

STUBS = """
alter table public.events add column if not exists description text;
create table public.profiles (id uuid primary key, name text);
create table public.user_taste_documents (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  step_key text not null,
  content text not null,
  extraction text not null default 'llm'
);
"""


@pytest.fixture
def kdb(db: Any, migrate: Any) -> Any:
    db.execute(STUBS)
    migrate(db, MIGRATION)
    return db


def _event(db: Any, title: str, description: str | None, category: str | None) -> str:
    event_id = str(uuid.uuid4())
    db.execute(
        "insert into public.events (id, title, description, category, lat, long) values (%s, %s, %s, %s, -8.05, -34.9)",
        (event_id, title, description, category),
    )
    return event_id


def _stale(db: Any, event_id: str) -> bool:
    return db.execute("select keywords_stale from public.events where id = %s", (event_id,)).fetchone()[0]


def test_an_event_without_keywords_is_stale(kdb: Any) -> None:
    assert _stale(kdb, _event(kdb, "Show de frevo", "No Marco Zero", "musica_show")) is True


@pytest.mark.parametrize(
    ("title", "description", "category"),
    [
        ("Forró de Lais Senna", None, "musica_show"),
        ("Conferência “Da Visão ao Valor” – 2026", "Linha 1\n\nLinha 2… çãõ", "palestra_conhecimento"),
        ("Praia", "", None),
    ],
)
def test_the_apps_hash_is_the_databases(kdb: Any, title: str, description: str | None, category: str | None) -> None:
    event_id = _event(kdb, title, description, category)

    kdb.execute(
        "update public.events set keywords = %s, keywords_hash = %s where id = %s",
        (["forró"], event_content_hash(title, description, category), event_id),
    )

    assert _stale(kdb, event_id) is False


def test_a_new_description_makes_it_stale_again(kdb: Any) -> None:
    event_id = _event(kdb, "Show de frevo", "No Marco Zero", "musica_show")
    kdb.execute(
        "update public.events set keywords_hash = %s where id = %s",
        (event_content_hash("Show de frevo", "No Marco Zero", "musica_show"), event_id),
    )

    kdb.execute("update public.events set description = 'No Recife Antigo' where id = %s", (event_id,))

    assert _stale(kdb, event_id) is True


def test_profiles_and_answers_get_their_columns(kdb: Any) -> None:
    user_id = str(uuid.uuid4())
    kdb.execute(
        "insert into public.profiles (id, interest_keywords, interest_summary) values (%s, %s, %s)",
        (user_id, ["literatura clássica", "música brasileira"], "Gosta de clássicos."),
    )
    kdb.execute(
        "insert into public.user_taste_documents (user_id, step_key, content, analysis) values (%s, 'favorite_media', 'x', %s::jsonb)",
        (user_id, '{"keywords": []}'),
    )

    row = kdb.execute("select interest_keywords from public.profiles where id = %s", (user_id,)).fetchone()
    assert row[0] == ["literatura clássica", "música brasileira"]
