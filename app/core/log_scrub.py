"""Personal data out of logs and error records (LGPD, #25 phase 1).

Exception messages can carry what the request had: a PostgREST filter with the
e-mail, a CPF in a validation error. Whatever is written to ``app_errors`` or to a
log line goes through ``scrub`` first.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Brazilian mobiles with or without +55, with or without punctuation.
_PHONE = re.compile(r"(?<![\w])(?:\+?55[\s-]?)?\(?\d{2}\)?[\s-]?9\d{4}[\s-]?\d{4}(?!\d)")
_CPF = re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)")
# Account and row ids: a raw user id must not reach a log either (only its hash).
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def scrub(text: str) -> str:
    # PostgREST URLs in exception messages carry their filters percent-encoded
    # (`phone=eq.%2B5581…`, `email=eq.ana%40…`): decode before looking.
    if "%" in text:
        text = unquote(text)
    text = _UUID.sub("[id]", text)
    text = _EMAIL.sub("[e-mail]", text)
    text = _PHONE.sub("[telefone]", text)
    return _CPF.sub("[cpf]", text)


def scrub_value(value: Any) -> Any:
    """`scrub` every string inside dicts and lists (the `details` of a record)."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: scrub_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_value(v) for v in value]
    return value
