"""@username, nickname and photo on profiles (migration 20261102000000, #32 and #24).

Decisions 2026-11-01: every profile has a unique @username made from the full name
(accounts that existed too), the nickname is optional, and neither comes from the
sign-up metadata (the backend writes the chosen ones after its moderation; whoever calls
Supabase Auth directly would skip it). `profiles.email` follows the e-mail of the
account in Auth.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from app.core.usernames import is_reserved_username, username_problem

psycopg = pytest.importorskip("psycopg")

BEFORE = ("20261015000000_profile_cpf_phone.sql", "20261016000000_signup_pending.sql")
MIGRATION = ("20261102000000_public_identity.sql",)

# What exists in production before these migrations (created in the dashboard).
STUBS = """
create schema if not exists auth;
create table auth.users (id uuid primary key, email text, raw_user_meta_data jsonb);
create table public.subscription_plans (id uuid primary key default gen_random_uuid(), name text);
insert into public.subscription_plans (name) values ('Free');
create table public.ai_chats (user_id uuid);
create table public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  plan_id uuid, name text, email text unique
);
"""
TRIGGER = """
create trigger on_auth_user_created after insert on auth.users
  for each row execute function public.handle_new_user();
"""


@pytest.fixture
def db(empty_db: Any, migrate: Callable[..., None]) -> Any:
    empty_db.execute(STUBS)
    migrate(empty_db, BEFORE + MIGRATION)
    empty_db.execute(TRIGGER)
    return empty_db


def _signup(conn: Any, email: str, meta: dict[str, Any]) -> str:
    user_id = str(uuid.uuid4())
    conn.execute(
        "insert into auth.users (id, email, raw_user_meta_data) values (%s, %s, %s::jsonb)",
        (user_id, email, json.dumps(meta)),
    )
    return user_id


def _profile(conn: Any, user_id: str) -> tuple[Any, ...]:
    return conn.execute(
        "select username, username_changed_at, nickname, avatar_path from public.profiles where id = %s",
        (user_id,),
    ).fetchone()


def _username(conn: Any, user_id: str) -> str:
    return _profile(conn, user_id)[0]


def test_a_sign_up_gets_a_username_from_its_name(db: Any) -> None:
    user = _signup(db, "ana@example.com", {"name": "Ana Beatriz", "flyer_signup": True})

    assert _profile(db, user) == ("ana.beatriz", None, None, None)


@pytest.mark.parametrize("wanted", ["ana.bia", "palavrao.feio", "A!", "flyer1"])
def test_a_username_in_the_metadata_is_ignored(db: Any, wanted: str) -> None:
    # Whoever calls Supabase Auth directly would skip the backend's moderation.
    user = _signup(db, "ana@example.com", {"name": "Ana Lima", "username": wanted})

    assert _username(db, user) == "ana.lima"


def test_google_accounts_get_a_username_from_the_full_name(db: Any) -> None:
    first = _signup(db, "p1@example.com", {"full_name": "Pedro Guerra"})
    second = _signup(db, "p2@example.com", {"full_name": "Pedro Guerra"})

    assert _username(db, first) == "pedro.guerra"
    assert re.fullmatch(r"pedro\.guerra\d{2,4}", _username(db, second))


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (None, "usuario"),
        ("", "usuario"),
        ("😀🎉", "usuario"),
        ("王小明", "usuario"),
        ("A", "usuario"),
        ("Jo", "usuario"),
        ("Admin", "usuario"),
        ("Suporte Flyer", "usuario"),
        ("D'Ávila", "d.avila"),
        ("  Ana  ", "ana"),
        ("Ana_Maria", "ana.maria"),
        ("João Ção Müller", "joao.cao.muller"),
        ("José da Silva Xavier Pereira", "jose.da.silva.x"),
        ("x" * 100, "x" * 15),
        ("2026", "usuario"),
        ("Ana 81999998888", "usuario"),
        ("Turma 2026", "turma.2026"),
    ],
)
def test_generated_usernames(db: Any, name: str | None, expected: str) -> None:
    generated = db.execute("select public.generate_username(%s)", (name,)).fetchone()[0]

    assert generated == expected
    assert username_problem(generated) is None


def test_digits_added_to_a_name_ending_in_digits_come_after_an_underscore(db: Any) -> None:
    first = _signup(db, "t1@example.com", {"name": "Turma 1234567"})
    second = _signup(db, "t2@example.com", {"name": "Turma 1234567"})

    assert _username(db, first) == "turma.1234567"
    assert re.fullmatch(r"turma\.1234567_\d{2,4}", _username(db, second))


def test_the_generator_never_returns_a_taken_username(db: Any) -> None:
    users = [_signup(db, f"u{i}@example.com", {"full_name": "Bia"}) for i in range(5)]

    names = [_username(db, u) for u in users]
    assert len(set(names)) == 5
    assert names[0] == "bia"
    assert all(username_problem(n) is None for n in names)


def test_the_nickname_never_comes_from_the_metadata(db: Any) -> None:
    user = _signup(db, "ana@example.com", {"name": "Ana", "nickname": "Suporte Flyer"})

    assert _profile(db, user)[2] is None


def test_a_profile_inserted_without_a_username_gets_one(db: Any) -> None:
    # The backend's get_or_create_profile inserts only the id (and here a name).
    user = str(uuid.uuid4())
    db.execute("set session_replication_role = replica")
    db.execute("insert into auth.users (id, email) values (%s, 'bia@example.com')", (user,))
    db.execute("set session_replication_role = origin")

    db.execute("insert into public.profiles (id, name) values (%s, 'Bia Lima')", (user,))

    assert _username(db, user) == "bia.lima"


def test_existing_profiles_get_a_username_and_their_email_from_auth(empty_db: Any, migrate: Callable[..., None]) -> None:
    empty_db.execute(STUBS)
    migrate(empty_db, BEFORE)
    rows = [("Ana Lima", "a1@example.com", "old@example.com"), ("Ana Lima", "a2@example.com", None), (None, "x@example.com", "x@example.com")]
    ids = []
    for name, auth_email, profile_email in rows:
        user = str(uuid.uuid4())
        ids.append(user)
        empty_db.execute("insert into auth.users (id, email) values (%s, %s)", (user, auth_email))
        empty_db.execute("insert into public.profiles (id, name, email) values (%s, %s, %s)", (user, name, profile_email))

    migrate(empty_db, MIGRATION)

    names = [_username(empty_db, u) for u in ids]
    # Which of the two Anas gets the plain one depends only on the order of the ids.
    plain, numbered = sorted(names[:2], key=len)
    assert plain == "ana.lima"
    assert re.fullmatch(r"ana\.lima\d{2,4}", numbered)
    assert names[2] == "usuario"
    emails = [empty_db.execute("select email from public.profiles where id = %s", (u,)).fetchone()[0] for u in ids]
    assert emails == ["a1@example.com", "a2@example.com", "x@example.com"]


def test_two_sign_ups_racing_for_the_same_username_both_succeed(db: Any) -> None:
    url = db.info.dsn
    first_id = str(uuid.uuid4())
    meta = json.dumps({"name": "Ana Souza"})
    with psycopg.connect(url) as first:
        first.execute(
            "insert into auth.users (id, email, raw_user_meta_data) values (%s, 'a1@example.com', %s::jsonb)",
            (first_id, meta),
        )
        # `first` holds the username without committing; the second sign-up blocks on the
        # unique index and must pick another username when `first` commits.
        result: dict[str, Any] = {}

        def second_sign_up() -> None:
            try:
                with psycopg.connect(url, autocommit=True) as second:
                    result["id"] = _signup(second, "a2@example.com", {"name": "Ana Souza"})
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc

        worker = threading.Thread(target=second_sign_up)
        worker.start()
        worker.join(timeout=1)
        assert worker.is_alive(), "the second sign-up should wait for the first"
        first.commit()
        worker.join(timeout=10)

    assert "error" not in result, result.get("error")
    assert _username(db, first_id) == "ana.souza"
    assert re.fullmatch(r"ana\.souza\d{2,4}", _username(db, result["id"]))


def test_a_cpf_clash_still_fails_the_sign_up(db: Any) -> None:
    _signup(db, "a1@example.com", {"name": "Ana", "cpf": "52998224725"})

    with pytest.raises(psycopg.errors.UniqueViolation):
        _signup(db, "a2@example.com", {"name": "Ana", "cpf": "52998224725"})


def test_a_new_email_in_auth_reaches_the_profile(db: Any) -> None:
    user = _signup(db, "old@example.com", {"name": "Ana"})

    db.execute("update auth.users set email = 'new@example.com' where id = %s", (user,))

    assert db.execute("select email from public.profiles where id = %s", (user,)).fetchone()[0] == "new@example.com"


def test_other_auth_updates_leave_the_profile_alone(db: Any) -> None:
    user = _signup(db, "ana@example.com", {"name": "Ana"})
    db.execute("update public.profiles set email = 'kept@example.com' where id = %s", (user,))

    db.execute("update auth.users set raw_user_meta_data = '{}'::jsonb where id = %s", (user,))

    assert db.execute("select email from public.profiles where id = %s", (user,)).fetchone()[0] == "kept@example.com"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("username", "Flyer1"),
        ("username", "flyer1"),
        ("username", "a..b"),
        ("username", "ab"),
        ("nickname", "x" * 41),
        ("nickname", ""),
        ("avatar_path", "outra-pasta/foto.jpg"),
        ("avatar_path", "https://example.com/foto.jpg"),
    ],
)
def test_the_database_refuses_bad_values(db: Any, column: str, value: str) -> None:
    user = _signup(db, "ana@example.com", {"name": "Ana"})

    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(f"update public.profiles set {column} = %s where id = %s", (value, user))


def test_the_username_cannot_be_removed(db: Any) -> None:
    user = _signup(db, "ana@example.com", {"name": "Ana"})

    with pytest.raises(psycopg.errors.NotNullViolation):
        db.execute("update public.profiles set username = null where id = %s", (user,))


def test_usernames_are_unique(db: Any) -> None:
    _signup(db, "a1@example.com", {"name": "Ana"})
    other = _signup(db, "a2@example.com", {"name": "Bia"})

    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("update public.profiles set username = 'ana' where id = %s", (other,))


def test_a_valid_avatar_path_is_accepted(db: Any) -> None:
    user = _signup(db, "ana@example.com", {"name": "Ana"})
    path = f"{user}/{uuid.uuid4()}.webp"

    db.execute("update public.profiles set avatar_path = %s where id = %s", (path, user))

    assert _profile(db, user)[3] == path


PROBES = [
    "pedro.guerra", "ana_b", "flyer", "flyer1", "admin_br", "pedro.flyer", "flyerzinho", "ajudante",
    "ab", "a" * 20, "a" * 21, ".ana", "ana.", "ana..b", "81999998888", "ana2026", "_ana_", "null",
    "www.ana", "equipe.azul", "seguranca", "sistema9", "root_", "api.x", "contatos",
    "ana81999998888", "a52998224725", "ana1234567", "12345678ab",
]


@pytest.mark.parametrize("probe", PROBES)
def test_sql_and_python_agree(db: Any, probe: str) -> None:
    valid, reserved = db.execute(
        "select public.is_valid_username(%s), public.is_reserved_username(%s)", (probe, probe)
    ).fetchone()

    assert valid is (username_problem(probe) is None)
    assert reserved is is_reserved_username(probe)


FUNCTIONS = ("is_reserved_username", "is_valid_username", "generate_username", "profiles_fill_username", "sync_profile_email")


@pytest.mark.parametrize("role", ["anon", "authenticated"])
@pytest.mark.parametrize("function", FUNCTIONS + ("handle_new_user",))
def test_clients_cannot_call_the_functions(db: Any, role: str, function: str) -> None:
    allowed = db.execute(
        "select bool_or(has_function_privilege(%s, p.oid, 'execute')) from pg_proc p "
        "join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public' and p.proname = %s",
        (role, function),
    ).fetchone()[0]

    assert allowed is False


@pytest.mark.parametrize("function", ["is_reserved_username", "is_valid_username", "generate_username"])
def test_the_backend_can_call_the_username_functions(db: Any, function: str) -> None:
    allowed = db.execute(
        "select has_function_privilege('service_role', p.oid, 'execute') from pg_proc p "
        "join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public' and p.proname = %s",
        (function,),
    ).fetchone()[0]

    assert allowed is True


@pytest.mark.parametrize("function", FUNCTIONS + ("handle_new_user",))
def test_every_function_pins_its_search_path(db: Any, function: str) -> None:
    config = db.execute(
        "select p.proconfig from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
        "where n.nspname = 'public' and p.proname = %s",
        (function,),
    ).fetchone()[0]

    assert config and any(c.startswith("search_path=") for c in config)
