"""E-mail templates carry 6-digit codes, never one-time links.

E-mail security scanners (Safe Links, Mimecast…) open every link in a message
seconds after it arrives, which spends a single-use `{{ .ConfirmationURL }}` before
the person clicks it (#43: the password reset link "expired" for institutional
e-mails). A code cannot be spent by a scanner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "supabase" / "templates"


@pytest.mark.parametrize("name", ["confirm_signup.html", "magic_link_code.html", "reset_password.html"])
def test_template_sends_a_code_not_a_link(name: str) -> None:
    html = (TEMPLATES / name).read_text(encoding="utf-8")
    assert "{{ .Token }}" in html
    assert "ConfirmationURL" not in html
    assert "TokenHash" not in html
