"""Third-party personal data in event texts (LGPD, #22 part A, decisions 2026-10-18).

An event's title, description, place and trophy are shown to everybody, so an
e-mail, a phone number or a CPF in them is refused when the event is created or
edited in the app, and replaced by "[contato removido]" when it is imported.

`log_scrub` keeps its own, narrower patterns: it only has to keep logs clean.
"""

from __future__ import annotations

import re

from app.core.br_documents import normalize_cpf

CONTACT_REMOVED = "[contato removido]"

EMAIL = "e-mail"
PHONE = "telefone"
CPF = "CPF"

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# A Brazilian number with its area code: mobiles (9 and 8 digits, often written
# "9 9999-8888") and landlines (8 digits starting with 2 to 5), with or without +55.
_PHONE = re.compile(
    r"(?<![\w])(?:\+?55[\s.-]?)?\(?\d{2}\)?[\s.-]?(?:9[\s.-]?\d{4}|[2-5]\d{3})[\s.-]?\d{4}(?![\w])"
)
_CPF = re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)")


def _valid_cpfs(text: str) -> list[re.Match[str]]:
    # Any 11 digits look like a CPF; only a valid check digit makes it one.
    return [m for m in _CPF.finditer(text) if normalize_cpf(m.group(0))]


def find_personal_data(text: str | None) -> list[str]:
    """The kinds found (e-mail, telefone, CPF), in that order, each once."""
    if not text:
        return []
    found: list[str] = []
    if _EMAIL.search(text):
        found.append(EMAIL)
    # A valid CPF also matches the phone pattern; look for phones without it.
    without_cpf = _CPF.sub(lambda m: "" if normalize_cpf(m.group(0)) else m.group(0), text)
    if _PHONE.search(without_cpf):
        found.append(PHONE)
    if _valid_cpfs(text):
        found.append(CPF)
    return found


def redact_personal_data(text: str | None) -> str | None:
    """The text with every e-mail, phone and CPF replaced by "[contato removido]"."""
    if not text:
        return text
    out = _EMAIL.sub(CONTACT_REMOVED, text)
    out = _CPF.sub(lambda m: CONTACT_REMOVED if normalize_cpf(m.group(0)) else m.group(0), out)
    return _PHONE.sub(CONTACT_REMOVED, out)
