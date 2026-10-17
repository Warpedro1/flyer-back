"""Admission tokens behind the QR code: signing, expiry and event binding."""

from __future__ import annotations

import time
from unittest.mock import patch

import jwt as pyjwt
import pytest

from app.core.config import settings
from app.core.errors import ApiError
from app.services.rsvp_qr import AttendeePosition, issue_token, position_from_fix, verify_token

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

    with pytest.raises(ApiError) as exc:
        verify_token(expired, "evt-1")

    assert exc.value.status_code == 401
    assert exc.value.code == "qr_expired"
    assert "expirado" in exc.value.message.lower()


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

    with pytest.raises(ApiError) as exc:
        verify_token(forged, "evt-1")

    assert exc.value.status_code == 401


def test_token_from_another_event_is_rejected() -> None:
    token, _ = issue_token("rsvp-1", "evt-OTHER", "user-1")

    with pytest.raises(ApiError) as exc:
        verify_token(token, "evt-1")

    # 403 rather than 401: the token is genuine, it just is not for this door.
    assert exc.value.status_code == 403
    assert exc.value.code == "qr_wrong_event"
    assert "não é deste evento" in exc.value.message


def test_token_missing_claims_is_rejected() -> None:
    incomplete = pyjwt.encode(
        {"evt": "evt-1", "exp": int(time.time()) + 60},
        SECRET,
        algorithm="HS256",
    )

    with pytest.raises(ApiError) as exc:
        verify_token(incomplete, "evt-1")

    assert exc.value.status_code == 401
    assert exc.value.code == "qr_invalid"


def test_feature_is_off_without_a_configured_secret() -> None:
    with patch.object(settings, "RSVP_QR_SECRET", ""):
        with pytest.raises(ApiError) as issuing:
            issue_token("rsvp-1", "evt-1", "user-1")
        with pytest.raises(ApiError) as verifying:
            verify_token("whatever", "evt-1")

    assert issuing.value.status_code == 503
    assert verifying.value.status_code == 503
    assert issuing.value.code == "qr_disabled"


# ------------------------------------------------- attendee's position (2026-10-17) --


def test_a_token_without_position_has_none() -> None:
    claims = verify_token(issue_token("rsvp-1", "evt-1", "user-1")[0], "evt-1")

    assert claims.position is None


def test_the_position_travels_inside_the_token_rounded() -> None:
    here = AttendeePosition(lat=-8.0631234, long=-34.8712345, accuracy=12.4)

    claims = verify_token(issue_token("rsvp-1", "evt-1", "user-1", here)[0], "evt-1")

    # About 11 m: enough for a door, without a precise trail in a QR anyone can read.
    assert claims.position == AttendeePosition(lat=-8.0631, long=-34.8712, accuracy=12.0)


def test_a_position_cannot_be_forged_in_the_token() -> None:
    token = issue_token("rsvp-1", "evt-1", "user-1", AttendeePosition(-8.06, -34.87, 10.0))[0]
    header, payload, signature = token.split(".")
    other = pyjwt.encode({"loc": [0.0, 0.0, 1.0]}, SECRET, algorithm="HS256").split(".")[1]

    with pytest.raises(ApiError) as exc:
        verify_token(f"{header}.{other}.{signature}", "evt-1")

    assert exc.value.code == "qr_invalid"


def test_a_garbled_position_counts_as_none() -> None:
    token = pyjwt.encode(
        {"rsvp": "r", "evt": "evt-1", "sub": "u", "jti": "j", "exp": int(time.time()) + 60, "loc": ["x", 1]},
        SECRET,
        algorithm="HS256",
    )

    assert verify_token(token, "evt-1").position is None


def test_the_accuracy_is_capped() -> None:
    fix = position_from_fix(-8.06, -34.87, 2_000.0, fix_age_seconds=5)

    assert fix is not None
    assert fix.accuracy == 300.0


def test_a_negative_accuracy_is_treated_as_zero() -> None:
    fix = position_from_fix(-8.06, -34.87, -5.0, fix_age_seconds=5)

    assert fix is not None
    assert fix.accuracy == 0.0


def test_an_old_position_is_dropped() -> None:
    assert position_from_fix(-8.06, -34.87, 10.0, fix_age_seconds=121) is None
    assert position_from_fix(-8.06, -34.87, 10.0, fix_age_seconds=119) is not None


def test_a_position_from_the_future_is_dropped() -> None:
    # A clock far ahead of ours: do not trust the reading.
    assert position_from_fix(-8.06, -34.87, 10.0, fix_age_seconds=-120) is None
