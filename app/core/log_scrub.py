"""Personal data out of logs and error records (LGPD, #25 phase 1).

Exception messages can carry what the request had: a PostgREST filter with the
e-mail, a CPF in a validation error. Whatever is written to ``app_errors`` or to a
log line goes through ``scrub`` first.
"""

from __future__ import annotations

import re
from typing import Any

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Brazilian mobiles with or without +55, with or without punctuation.
_PHONE = re.compile(r"(?<![\w])(?:\+?55[\s-]?)?\(?\d{2}\)?[\s-]?9\d{4}[\s-]?\d{4}(?!\d)")
_CPF = re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)")


def scrub(text: str) -> str:
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
