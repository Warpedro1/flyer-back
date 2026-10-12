"""CPF and Brazilian mobile numbers (decisions 2026-10-12).

Normalised forms are what the database stores: CPF as 11 digits, phone in E.164
(`+55DD9XXXXXXXX`). The database checks the same rules (migration
20261015000000_profile_cpf_phone).
"""

from __future__ import annotations

import re

_NON_DIGITS = re.compile(r"\D")


def _digits(value: str) -> str:
    return _NON_DIGITS.sub("", value or "")


def _cpf_check_digit(digits: str) -> int:
    weight = len(digits) + 1
    total = sum(int(d) * (weight - i) for i, d in enumerate(digits))
    rest = (total * 10) % 11
    return 0 if rest == 10 else rest


def normalize_cpf(value: str) -> str | None:
    """The CPF as 11 digits, or None if it is not a valid CPF."""
    cpf = _digits(value)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return None
    if _cpf_check_digit(cpf[:9]) != int(cpf[9]) or _cpf_check_digit(cpf[:10]) != int(cpf[10]):
        return None
    return cpf


def normalize_phone(value: str) -> str | None:
    """A Brazilian mobile number in E.164 (+55 + DDD + 9 + 8 digits), or None."""
    digits = _digits(value)
    if len(digits) == 13 and digits.startswith("55"):
        digits = digits[2:]
    if len(digits) != 11:
        return None
    ddd, number = digits[:2], digits[2:]
    if "0" in ddd or not number.startswith("9"):
        return None
    return f"+55{digits}"


def mask_cpf(cpf: str | None) -> str | None:
    """***.456.789-** : enough for the owner to recognise it."""
    if not cpf or len(cpf) != 11:
        return None
    return f"***.{cpf[3:6]}.{cpf[6:9]}-**"


def mask_phone(phone: str | None) -> str | None:
    """(81) 9****-5678"""
    if not phone or not phone.startswith("+55") or len(phone) != 14:
        return None
    ddd, number = phone[3:5], phone[5:]
    return f"({ddd}) {number[0]}****-{number[-4:]}"
