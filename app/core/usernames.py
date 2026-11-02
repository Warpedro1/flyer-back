"""Public @username and nickname rules (#32, decisions 2026-11-01).

Mirrors `public.is_valid_username` and `public.is_reserved_username` in
supabase/migrations/20261102000000_public_identity.sql: the database refuses what these
refuse, and tests/test_usernames.py keeps the reserved list equal in both.
"""

from __future__ import annotations

import re
import unicodedata

# Taken by the app or its staff. A name is reserved when any of its pieces between dots,
# underscores and digits is one of these, so `flyer1`, `admin_br` and `pedro.flyer` are.
RESERVED_USERNAMES: frozenset[str] = frozenset(
    {
        "admin",
        "flyer",
        "suporte",
        "ajuda",
        "contato",
        "oficial",
        "equipe",
        "moderacao",
        "sistema",
        "seguranca",
        "root",
        "api",
        "www",
        "null",
    }
)

# A nickname is free text, but these words would make it pass for the app or its staff.
AUTHORITY_WORDS: frozenset[str] = frozenset(
    {"admin", "flyer", "suporte", "oficial", "moderacao", "seguranca", "sistema"}
)

USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 20
NICKNAME_MAX_LENGTH = 40

_FORMAT = re.compile(r"[a-z0-9_][a-z0-9._]{1,18}[a-z0-9_]")
_PIECES = re.compile(r"[._0-9]+")
_WORDS = re.compile(r"[^a-z]+")
# Eight digits in a row could be a phone or a CPF.
_LONG_NUMBER = re.compile(r"[0-9]{8}")
# "SuporteFlyer" is two words.
_CAMEL = re.compile(r"(?<=[a-zà-ÿ])(?=[A-ZÀ-Þ])")


def normalize_username(value: str) -> str:
    """What the person typed, as stored: no spaces, no leading `@`, lower case."""
    return value.strip().removeprefix("@").strip().lower()


def is_reserved_username(value: str) -> bool:
    return any(piece in RESERVED_USERNAMES for piece in _PIECES.split(value.lower()))


def username_problem(value: str) -> str | None:
    """`"invalid"`, `"reserved"` or None for a normalized username."""
    if (
        not _FORMAT.fullmatch(value)
        or ".." in value
        or not re.search(r"[a-z]", value)
        or _LONG_NUMBER.search(value)
    ):
        return "invalid"
    if is_reserved_username(value):
        return "reserved"
    return None


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def has_hidden_characters(text: str) -> bool:
    """Control and format characters (zero-width space, right-to-left override…): they
    make a nickname read differently from what it is."""
    return any(unicodedata.category(c) in {"Cc", "Cf"} for c in text)


def uses_authority_word(text: str) -> bool:
    """True when a nickname contains one of the AUTHORITY_WORDS as a word ("Suporte
    Flyer", "SuporteFlyer")."""
    return any(word in AUTHORITY_WORDS for word in _WORDS.split(_fold(_CAMEL.sub(" ", text))))
