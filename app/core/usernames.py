"""Public @username and nickname rules (#32, decisions 2026-11-01).

Mirrors `public.is_valid_username` and `public.is_reserved_username` in
supabase/migrations/20261102000000_public_identity.sql: the database refuses what these
refuse, and tests/test_usernames.py keeps the reserved list equal in both.
"""

from __future__ import annotations

import re
import unicodedata

# Taken by the app or its staff. A name is reserved when any of its pieces between dots,
# underscores and digits is made only of these words, so `flyer1`, `admin_br`,
# `pedro.flyer` and `suporteflyer` are, and `flyerzinho` or `administrador` are not.
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

# At most 7 digits in all: a phone or a CPF does not fit, however it is split.
MAX_USERNAME_DIGITS = 7

_FORMAT = re.compile(r"[a-z0-9_][a-z0-9._]{1,18}[a-z0-9_]")
_PIECES = re.compile(r"[._0-9]+")
_ONLY_RESERVED = re.compile("(?:" + "|".join(sorted(RESERVED_USERNAMES)) + ")+")
_ONLY_AUTHORITY = re.compile("(?:" + "|".join(sorted(AUTHORITY_WORDS)) + ")+")
# "SuporteFlyer" is two words.
_CAMEL = re.compile(r"(?<=[a-zà-ÿ])(?=[A-ZÀ-Þ])")
# Characters that show nothing (or almost nothing) besides the control and format
# categories: combining grapheme joiner, Hangul and halfwidth fillers, Mongolian and
# Khmer invisible marks, variation selectors, braille blank.
_INVISIBLE = frozenset(
    [0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0, *range(0x180B, 0x1810), *range(0xFE00, 0xFE10)]
    + list(range(0xE0100, 0xE01F0))
)


def normalize_username(value: str) -> str:
    """What the person typed, as stored: no spaces, no leading `@`, lower case."""
    return value.strip().removeprefix("@").strip().lower()


def is_reserved_username(value: str) -> bool:
    return any(_ONLY_RESERVED.fullmatch(piece) for piece in _PIECES.split(value.lower()) if piece)


def username_problem(value: str) -> str | None:
    """`"invalid"`, `"reserved"` or None for a normalized username."""
    if (
        not _FORMAT.fullmatch(value)
        or ".." in value
        or not re.search(r"[a-z]", value)
        or sum(c.isdigit() for c in value) > MAX_USERNAME_DIGITS
    ):
        return "invalid"
    if is_reserved_username(value):
        return "reserved"
    return None


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def has_hidden_characters(text: str) -> bool:
    """Characters that show nothing or change how the rest reads (zero-width space,
    right-to-left override, fillers, variation selectors): a nickname must read as it is."""
    return any(
        unicodedata.category(c) in {"Cc", "Cf", "Co", "Cn", "Cs"} or ord(c) in _INVISIBLE for c in text
    )


def has_visible_text(text: str) -> bool:
    """At least one letter or digit: a nickname is not only symbols or spaces."""
    return any(c.isalnum() for c in text)


def mixes_scripts(text: str) -> bool:
    """Latin letters together with Cyrillic or Greek ones: how look-alikes pass ("Аdmin")."""
    scripts = set()
    for c in text:
        if c.isalpha():
            name = unicodedata.name(c, "")
            scripts.add(name.split(" ", 1)[0] if name.startswith(("CYRILLIC", "GREEK")) else "LATIN" if "LATIN" in name else "OTHER")
    return "LATIN" in scripts and bool(scripts & {"CYRILLIC", "GREEK"})


def uses_authority_word(text: str) -> bool:
    """True when a nickname has a word made only of AUTHORITY_WORDS ("Suporte Flyer",
    "SuporteFlyer", "SUPORTEFLYER", "Flyer™"); "Flyerzinho" is fine."""
    letters_only = "".join(c if c.isalpha() else " " for c in _CAMEL.sub(" ", text))
    return any(_ONLY_AUTHORITY.fullmatch(_fold(word)) for word in letters_only.split())
