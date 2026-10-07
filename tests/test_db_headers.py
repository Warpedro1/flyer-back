"""PostgREST auth headers for both Supabase key formats (issue #9).

The legacy service_role key is a JWT and goes in ``apikey`` and ``Authorization``.
The new secret keys (``sb_secret_...``) are not JWTs: the platform rejects them in
``Authorization: Bearer`` with "Invalid JWT", so they must travel in ``apikey`` only.
Supporting both lets the key be swapped on Vercel without a code deploy.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.core.config import settings
from app.services.db_service import DBService


@pytest.fixture
def db() -> DBService:
    return DBService(MagicMock())


def test_legacy_jwt_key_is_sent_in_apikey_and_authorization(
    db: DBService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "SUPABASE_KEY", "eyJhbGciOiJIUzI1NiJ9.legacy.sig")

    headers = db._admin_headers()

    assert headers["apikey"] == "eyJhbGciOiJIUzI1NiJ9.legacy.sig"
    assert headers["Authorization"] == "Bearer eyJhbGciOiJIUzI1NiJ9.legacy.sig"


def test_secret_key_is_sent_in_apikey_only(db: DBService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SUPABASE_KEY", "sb_secret_abc123")

    headers = db._admin_headers()

    assert headers["apikey"] == "sb_secret_abc123"
    assert "Authorization" not in headers


def test_user_headers_follow_the_same_rule(db: DBService, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SUPABASE_KEY", "sb_secret_abc123")

    headers = db._user_headers("user-jwt")

    assert headers["apikey"] == "sb_secret_abc123"
    assert "Authorization" not in headers
