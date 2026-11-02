"""@username and nickname rules (#32, decisions 2026-11-01)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.usernames import (
    RESERVED_USERNAMES,
    has_hidden_characters,
    is_reserved_username,
    normalize_username,
    username_problem,
    uses_authority_word,
)

MIGRATION = Path(__file__).resolve().parents[1] / "supabase" / "migrations" / "20261102000000_public_identity.sql"


@pytest.mark.parametrize(
    ("typed", "stored"),
    [("@Pedro.Guerra", "pedro.guerra"), ("  ana_b ", "ana_b"), (" @ Bia ", "bia"), ("zé", "zé")],
)
def test_normalize_username(typed: str, stored: str) -> None:
    assert normalize_username(typed) == stored


@pytest.mark.parametrize(
    "name",
    ["pedro.guerra", "ana_b", "bia", "x9z", "_ana_", "maria.de.lourdes12", "a" * 20, "ana2026"],
)
def test_valid_usernames(name: str) -> None:
    assert username_problem(name) is None


@pytest.mark.parametrize(
    "name",
    [
        "ab",  # too short
        "a" * 21,  # too long
        ".ana",  # starts with a dot
        "ana.",  # ends with a dot
        "ana..b",  # two dots
        "ana b",
        "Ana",  # not normalized
        "zé",  # accents are not allowed
        "ana-b",
        "81999998888",  # no letter: it could be a phone or a CPF
        "ana81999998888",  # eight digits in a row: a phone glued to letters
        "a52998224725",
        "12345678ab",
        "___",
        ""
    ],
)
def test_invalid_usernames(name: str) -> None:
    assert username_problem(name) == "invalid"


@pytest.mark.parametrize(
    "name",
    ["admin", "flyer", "flyer1", "admin_br", "pedro.flyer", "suporte.tecnico", "null", "www", "equipe.azul"],
)
def test_reserved_usernames(name: str) -> None:
    assert username_problem(name) == "reserved"
    assert is_reserved_username(name)


@pytest.mark.parametrize("name", ["flyerzinho", "ajudante", "administrador", "rootbeer", "contatos"])
def test_words_that_only_start_like_a_reserved_one_are_free(name: str) -> None:
    assert username_problem(name) is None


@pytest.mark.parametrize(
    ("nickname", "blocked"),
    [
        ("Suporte Flyer", True),
        ("SuporteFlyer", True),
        ("PedroAdmin", True),
        ("Moderação", True),
        ("Equipe do flyer1", True),
        ("Admin", True),
        ("Zé do Coco", False),
        ("Equipe Azul", False),
        ("Ajuda aí", False),
        ("Flyerzinho", False),
    ],
)
def test_nickname_authority_words(nickname: str, blocked: bool) -> None:
    assert uses_authority_word(nickname) is blocked


@pytest.mark.parametrize(
    ("text", "hidden"),
    [("Fl\u200byer", True), ("\u202enimda", True), ("Ana\tLima", True), ("Zé do Coco", False), ("Ana 🎉", False)],
)
def test_hidden_characters(text: str, hidden: bool) -> None:
    assert has_hidden_characters(text) is hidden


def test_the_migration_reserves_the_same_words() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    start = sql.index("create or replace function public.is_reserved_username")
    listed = re.search(r"array\[([^\]]*)\]::text\[\]", sql[start:])

    assert listed is not None
    assert set(re.findall(r"'([a-z]+)'", listed.group(1))) == RESERVED_USERNAMES
