"""Personal data out of logs and error records (LGPD, #25 phase 1; #32)."""

from __future__ import annotations

import pytest

from app.core.log_scrub import scrub, scrub_value


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("falhou para ana@example.com", "falhou para [e-mail]"),
        ("celular (81) 99999-8888", "celular [telefone]"),
        ("cpf 529.982.247-25", "cpf [cpf]"),
        # PostgREST URLs in exception messages carry their filters percent-encoded.
        (
            "Client error '409' for url 'http://x/rest/v1/profiles?phone=eq.%2B5581999998888&select=id'",
            "Client error '409' for url 'http://x/rest/v1/profiles?phone=eq.[telefone]&select=id'",
        ),
        # The `eq.` goes with it (dots are valid in an address): more out, never less.
        ("profiles?email=eq.ana%40example.com", "profiles?email=[e-mail]"),
        # A raw user id never reaches a log (only its hash).
        ("profiles?id=eq.11111111-1111-4111-8111-111111111111", "profiles?id=eq.[id]"),
        ("100% frevo", "100% frevo"),
    ],
)
def test_scrub(text: str, expected: str) -> None:
    assert scrub(text) == expected


def test_scrub_value_goes_into_dicts_and_lists() -> None:
    assert scrub_value({"a": ["ana@example.com", 3], "b": None}) == {"a": ["[e-mail]", 3], "b": None}
