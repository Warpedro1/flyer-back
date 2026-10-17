"""Reading the ticket's QR at the door is the check-in (decisions 2026-10-17).

* Events created in the app: the creator reads the attendee's QR; the backend checks
  the check-in window, the RSVP, and the attendee's position carried in the token
  (200 m plus the reported GPS accuracy, capped at 300 m). Without a position the
  creator may confirm by sight; too far is refused outright.
* An accepted scan admits the RSVP, gives the event's trophy and tells the attendee's
  ticket screen over Pusher. The trophy and the notice are best effort.
* The single-use ``jti`` is only spent when the entry is accepted: a refusal lets
  the same QR be read again after the problem is fixed.
* Imported events are informative: "Tenho interesse" RSVPs, but no ticket.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user_with_token, get_db_service
from app.api.events import _row_to_event_read
from app.core.config import settings
from app.core.limiter import limiter
from app.main import app
from app.services.checkin_service import checkin_method
from app.services.db_service import RpcError, UniqueViolationError
from app.services.rsvp_qr import AttendeePosition, issue_token

NOW = datetime.now(timezone.utc)
SECRET = "test-qr-secret"
LAT, LONG = -8.06, -34.87
# ~111 m per 0.001 degree of latitude.
EVENT = {
    "id": "evt-1",
    "kind": "event",
    "source": None,
    "creator_id": "creator-1",
    "title": "Noite de Frevo",
    "lat": LAT,
    "long": LONG,
    "event_date": (NOW - timedelta(hours=1)).isoformat(),
    "ends_at": (NOW + timedelta(hours=2)).isoformat(),
    "checkin_opens_at": (NOW - timedelta(hours=1)).isoformat(),
    "checkin_closed_at": None,
}
RSVP = {"id": "rsvp-1", "event_id": "evt-1", "user_id": "ana-1", "status": "confirmed"}
TEMPLATE = {"id": "tpl-1", "name": "Frevo!", "description": None, "icon_url": None}
HERE = AttendeePosition(lat=LAT, long=LONG, accuracy=10.0)


@pytest.fixture(autouse=True)
def _qr_secret() -> Iterator[None]:
    with patch.object(settings, "RSVP_QR_SECRET", SECRET):
        yield


@pytest.fixture
def db() -> Iterator[MagicMock]:
    limiter.reset()
    fake = MagicMock()
    fake.get_event_by_id = AsyncMock(return_value=EVENT)
    fake.get_rsvp_by_id = AsyncMock(return_value=RSVP)
    fake.get_rsvp = AsyncMock(return_value=RSVP)
    fake.sweep_expired_calls = AsyncMock()
    fake.rsvp_admit = AsyncMock(return_value={**RSVP, "status": "admitted"})
    fake.get_rsvp_profile = AsyncMock(return_value={"id": "ana-1", "name": "Ana"})
    fake.get_trophy_template_by_event = AsyncMock(return_value=TEMPLATE)
    fake.ensure_default_trophy_template = AsyncMock(return_value=TEMPLATE)
    fake.insert_trophy = AsyncMock(return_value={"id": "tr-1", "acquired_at": NOW.isoformat()})
    fake.get_user_trophy_for_template = AsyncMock(return_value={"id": "tr-0", "acquired_at": NOW.isoformat()})
    fake.sign_media_paths = AsyncMock(return_value={})
    app.dependency_overrides[get_current_user_with_token] = lambda: ("creator-1", "jwt")
    app.dependency_overrides[get_db_service] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


@pytest.fixture
def published() -> Iterator[AsyncMock]:
    with patch("app.api.rsvp.realtime.publish", AsyncMock(return_value=True)) as publish:
        yield publish


def _token(position: AttendeePosition | None = HERE) -> str:
    return issue_token("rsvp-1", "evt-1", "ana-1", position)[0]


def _scan(token: str, **extra: Any) -> Any:
    return TestClient(app).post("/events/evt-1/attendance/scan", json={"token": token, **extra})


# ------------------------------------------------------------------ method --


@pytest.mark.parametrize(
    ("row", "method"),
    [
        ({"kind": "event", "source": None}, "qr"),
        ({"kind": "event"}, "qr"),
        ({"kind": "event", "source": "sympla"}, "none"),
        ({"kind": "place", "source": "google_maps"}, "gps"),
        ({"kind": "place", "source": None}, "gps"),
    ],
)
def test_how_each_kind_of_event_checks_in(row: dict[str, Any], method: str) -> None:
    assert checkin_method(row) == method


def test_the_event_read_says_how_to_check_in() -> None:
    row = {**EVENT, "source": "sympla"}

    assert _row_to_event_read(row, []).checkin_method == "none"
    assert _row_to_event_read(EVENT, []).checkin_method == "qr"


# ----------------------------------------------------------------- success --


def test_a_scan_admits_gives_the_trophy_and_tells_the_attendee(
    db: MagicMock, published: AsyncMock
) -> None:
    response = _scan(_token())

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "admitted"
    assert body["attendee"] == {"id": "ana-1", "name": "Ana"}
    assert body["trophy"]["name"] == "Frevo!"
    assert body["location_checked"] is True
    db.rsvp_admit.assert_awaited_once()
    db.insert_trophy.assert_awaited_once_with("ana-1", "tpl-1", "jwt")
    published.assert_awaited_once()
    user_id, event_name, payload = published.await_args.args
    assert (user_id, event_name) == ("ana-1", "checkin:done")
    assert payload["event_id"] == "evt-1"
    assert payload["trophy"]["name"] == "Frevo!"


def test_without_a_custom_trophy_the_default_one_is_given(db: MagicMock, published: AsyncMock) -> None:
    db.get_trophy_template_by_event.return_value = None
    db.ensure_default_trophy_template.return_value = {"id": "tpl-new", "name": "Presença: Noite de Frevo"}

    response = _scan(_token())

    assert response.json()["trophy"]["name"] == "Presença: Noite de Frevo"
    db.ensure_default_trophy_template.assert_awaited_once_with("evt-1", "Presença: Noite de Frevo")


def test_someone_who_already_had_the_trophy_still_gets_in(db: MagicMock, published: AsyncMock) -> None:
    db.insert_trophy.side_effect = UniqueViolationError("dup")

    response = _scan(_token())

    assert response.status_code == 200
    assert response.json()["trophy"]["id"] == "tr-0"


def test_a_trophy_failure_does_not_undo_the_entry(
    db: MagicMock, published: AsyncMock, recorded: list[dict[str, Any]]
) -> None:
    db.insert_trophy.side_effect = RuntimeError("db down")

    response = _scan(_token())

    assert response.status_code == 200
    assert response.json()["trophy"] is None
    assert [r["code"] for r in recorded] == ["checkin_trophy_failed"]
    published.assert_awaited_once()


def test_a_pusher_failure_does_not_change_the_answer(db: MagicMock) -> None:
    with patch("app.api.rsvp.realtime.publish", AsyncMock(side_effect=RuntimeError("down"))):
        response = _scan(_token())

    assert response.status_code == 200


# ------------------------------------------------------------- the window --


def test_before_check_in_opens_the_scan_is_refused_and_the_qr_kept(db: MagicMock, published: AsyncMock) -> None:
    db.get_event_by_id.return_value = {
        **EVENT,
        "event_date": (NOW + timedelta(hours=2)).isoformat(),
        "checkin_opens_at": (NOW + timedelta(hours=2)).isoformat(),
        "ends_at": (NOW + timedelta(hours=5)).isoformat(),
    }

    response = _scan(_token())

    assert response.status_code == 409
    assert response.json()["code"] == "checkin_not_open"
    assert "abre às" in response.json()["detail"]
    db.rsvp_admit.assert_not_called()
    published.assert_not_called()


def test_after_the_event_ends_the_scan_is_refused(db: MagicMock, published: AsyncMock) -> None:
    db.get_event_by_id.return_value = {**EVENT, "ends_at": (NOW - timedelta(minutes=1)).isoformat()}

    response = _scan(_token())

    assert response.status_code == 409
    assert response.json()["code"] == "checkin_closed"
    db.rsvp_admit.assert_not_called()


def test_a_check_in_closed_by_the_creator_refuses_the_scan(db: MagicMock, published: AsyncMock) -> None:
    db.get_event_by_id.return_value = {**EVENT, "checkin_closed_at": (NOW - timedelta(minutes=5)).isoformat()}

    assert _scan(_token()).json()["code"] == "checkin_closed"


def test_the_event_read_reads_the_columns_the_scan_needs(db: MagicMock, published: AsyncMock) -> None:
    _scan(_token())

    select = db.get_event_by_id.await_args.kwargs["select"]
    assert "embedding" not in select
    assert {"kind", "source", "title", "lat", "long", "ends_at", "checkin_opens_at"} <= set(select.split(","))


# ----------------------------------------------------------- the location --


def test_close_enough_with_a_poor_signal_gets_in(db: MagicMock, published: AsyncMock) -> None:
    # ~333 m away, but the phone says it is only good to 150 m: 200 + 150 = 350 m.
    there = AttendeePosition(lat=LAT + 0.003, long=LONG, accuracy=150.0)

    assert _scan(_token(there)).status_code == 200


def test_too_far_is_refused_without_a_way_to_confirm(db: MagicMock, published: AsyncMock) -> None:
    there = AttendeePosition(lat=LAT + 0.003, long=LONG, accuracy=100.0)

    response = _scan(_token(there), confirm_without_location=True)

    assert response.status_code == 403
    assert response.json()["code"] == "attendee_too_far"
    db.rsvp_admit.assert_not_called()


def test_a_huge_accuracy_does_not_stretch_the_limit_past_500_m(db: MagicMock, published: AsyncMock) -> None:
    there = AttendeePosition(lat=LAT + 0.0055, long=LONG, accuracy=300.0)  # ~611 m

    assert _scan(_token(there)).json()["code"] == "attendee_too_far"


def test_without_a_position_the_creator_is_asked_to_confirm(db: MagicMock, published: AsyncMock) -> None:
    response = _scan(_token(None))

    assert response.status_code == 409
    assert response.json()["code"] == "location_missing"
    assert "Ana" in response.json()["detail"]
    db.rsvp_admit.assert_not_called()


def test_the_creator_can_confirm_someone_without_a_position(db: MagicMock, published: AsyncMock) -> None:
    response = _scan(_token(None), confirm_without_location=True)

    assert response.status_code == 200
    assert response.json()["location_checked"] is False


def test_an_event_without_coordinates_skips_the_location(db: MagicMock, published: AsyncMock) -> None:
    db.get_event_by_id.return_value = {**EVENT, "lat": None, "long": None}

    response = _scan(_token(None))

    assert response.status_code == 200
    assert response.json()["location_checked"] is False


# --------------------------------------------------------------- refusals --


@pytest.mark.parametrize(
    ("status_value", "code"),
    [
        ("waitlisted", "not_your_turn"),
        ("no_show", "call_expired"),
        ("cancelled", "rsvp_cancelled"),
        ("admitted", "already_admitted"),
    ],
)
def test_who_cannot_enter_is_refused_before_the_location(
    db: MagicMock, published: AsyncMock, status_value: str, code: str
) -> None:
    db.get_rsvp_by_id.return_value = {**RSVP, "status": status_value}

    # No position: the creator must not be asked to confirm someone who cannot enter.
    response = _scan(_token(None))

    assert response.status_code == 409
    assert response.json()["code"] == code
    db.rsvp_admit.assert_not_called()


def test_someone_already_in_still_gets_the_trophy(db: MagicMock, published: AsyncMock) -> None:
    # A trophy that failed on the first scan is given on the next one.
    db.get_rsvp_by_id.return_value = {**RSVP, "status": "admitted"}

    _scan(_token())

    db.insert_trophy.assert_awaited_once()


def test_a_vanished_rsvp_is_404(db: MagicMock, published: AsyncMock) -> None:
    db.get_rsvp_by_id.return_value = None

    response = _scan(_token())

    assert response.status_code == 404
    assert response.json()["code"] == "rsvp_not_found"


def test_a_qr_read_twice_is_refused(db: MagicMock, published: AsyncMock) -> None:
    db.rsvp_admit.side_effect = UniqueViolationError("jti")

    response = _scan(_token())

    assert response.status_code == 409
    assert response.json()["code"] == "qr_already_used"


def test_the_race_with_another_scan_answers_already_admitted(db: MagicMock, published: AsyncMock) -> None:
    db.rsvp_admit.side_effect = RpcError("already_admitted")

    assert _scan(_token()).json()["code"] == "already_admitted"


def test_an_expired_qr_answers_its_code(db: MagicMock, published: AsyncMock) -> None:
    with patch("app.services.rsvp_qr.settings.RSVP_QR_TTL_SECONDS", -60):
        token = _token()

    response = _scan(token)

    assert response.status_code == 401
    assert response.json()["code"] == "qr_expired"


def test_only_the_creator_can_scan(db: MagicMock, published: AsyncMock) -> None:
    app.dependency_overrides[get_current_user_with_token] = lambda: ("someone-else", "jwt")

    assert _scan(_token()).status_code == 403
    db.rsvp_admit.assert_not_called()


def test_an_imported_event_has_no_ticket_to_scan(db: MagicMock, published: AsyncMock) -> None:
    db.get_event_by_id.return_value = {**EVENT, "source": "sympla"}

    response = _scan(_token())

    assert response.status_code == 409
    assert response.json()["code"] == "ticket_unavailable"


# ------------------------------------------------------- issuing the QR --


def test_the_ticket_sends_its_position_in_the_body(db: MagicMock) -> None:
    app.dependency_overrides[get_current_user_with_token] = lambda: ("ana-1", "jwt")
    fix_at = (NOW - timedelta(seconds=5)).isoformat()

    response = TestClient(app).post(
        "/events/evt-1/rsvp/qr", json={"lat": LAT, "long": LONG, "accuracy": 8.0, "fix_at": fix_at}
    )

    assert response.status_code == 200
    from app.services.rsvp_qr import verify_token

    assert verify_token(response.json()["token"], "evt-1").position == AttendeePosition(LAT, LONG, 8.0)


def test_a_ticket_without_position_still_gets_a_qr(db: MagicMock) -> None:
    app.dependency_overrides[get_current_user_with_token] = lambda: ("ana-1", "jwt")

    response = TestClient(app).post("/events/evt-1/rsvp/qr", json={})

    assert response.status_code == 200


def test_the_old_get_still_works_during_the_deploy(db: MagicMock) -> None:
    app.dependency_overrides[get_current_user_with_token] = lambda: ("ana-1", "jwt")

    assert TestClient(app).get("/events/evt-1/rsvp/qr").status_code == 200


def test_an_imported_event_gives_no_ticket(db: MagicMock) -> None:
    app.dependency_overrides[get_current_user_with_token] = lambda: ("ana-1", "jwt")
    db.get_event_by_id.return_value = {**EVENT, "source": "sympla"}

    response = TestClient(app).post("/events/evt-1/rsvp/qr", json={})

    assert response.status_code == 409
    assert response.json()["code"] == "ticket_unavailable"


def test_a_position_out_of_range_is_invalid(db: MagicMock) -> None:
    app.dependency_overrides[get_current_user_with_token] = lambda: ("ana-1", "jwt")

    response = TestClient(app).post("/events/evt-1/rsvp/qr", json={"lat": 120, "long": LONG})

    assert response.status_code == 422
