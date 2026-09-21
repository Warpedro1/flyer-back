"""Admission tokens behind the QR code: signing, expiry and event binding."""

from __future__ import annotations

import time
from unittest.mock import patch

import jwt as pyjwt
import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.services.rsvp_qr import issue_token, verify_token

SECRET = "test-qr-secret"


@pytest.fixture(autouse=True)
def _qr_secret():
    with patch.object(settings, "RSVP_QR_SECRET", SECRET):
        yield


def test_issue_then_verify_round_trip() -> None:
    token, expires_at = issue_token("rsvp-1", "evt-1", "user-1")

    claims = verify_token(token, "evt-1")

    assert claims.rsvp_id == "rsvp-1"
    assert claims.event_id == "evt-1"
    assert claims.user_id == "user-1"
    assert claims.jti
    assert expires_at.timestamp() > time.time()


def test_each_token_carries_a_fresh_jti() -> None:
    # Single-use admission depends on the jti differing between refreshes.
    first = verify_token(issue_token("rsvp-1", "evt-1", "user-1")[0], "evt-1")
    second = verify_token(issue_token("rsvp-1", "evt-1", "user-1")[0], "evt-1")

    assert first.jti != second.jti


def test_expired_token_is_rejected() -> None:
    expired = pyjwt.encode(
        {
            "rsvp": "rsvp-1",
            "evt": "evt-1",
            "sub": "user-1",
            "jti": "abc",
            "iat": int(time.time()) - 600,
            "exp": int(time.time()) - 60,
        },
        SECRET,
        algorithm="HS256",
    )

    with pytest.raises(HTTPException) as exc:
        verify_token(expired, "evt-1")

    assert exc.value.status_code == 401
    assert "expirado" in str(exc.value.detail).lower()


def test_token_signed_with_another_secret_is_rejected() -> None:
    forged = pyjwt.encode(
        {
            "rsvp": "rsvp-1",
            "evt": "evt-1",
            "sub": "user-1",
            "jti": "abc",
            "exp": int(time.time()) + 60,
        },
        "not-our-secret",
        algorithm="HS256",
    )

    with pytest.raises(HTTPException) as exc:
        verify_token(forged, "evt-1")

    assert exc.value.status_code == 401


def test_token_from_another_event_is_rejected() -> None:
    token, _ = issue_token("rsvp-1", "evt-OTHER", "user-1")

    with pytest.raises(HTTPException) as exc:
        verify_token(token, "evt-1")

    # 403 rather than 401: the token is genuine, it just is not for this door.
    assert exc.value.status_code == 403
    assert "não é deste evento" in str(exc.value.detail)


def test_token_missing_claims_is_rejected() -> None:
    incomplete = pyjwt.encode(
        {"evt": "evt-1", "exp": int(time.time()) + 60},
        SECRET,
        algorithm="HS256",
    )

    with pytest.raises(HTTPException) as exc:
        verify_token(incomplete, "evt-1")

    assert exc.value.status_code == 401


def test_feature_is_off_without_a_configured_secret() -> None:
    with patch.object(settings, "RSVP_QR_SECRET", ""):
        with pytest.raises(HTTPException) as issuing:
            issue_token("rsvp-1", "evt-1", "user-1")
        with pytest.raises(HTTPException) as verifying:
            verify_token("whatever", "evt-1")

    assert issuing.value.status_code == 503
    assert verifying.value.status_code == 503
