""""Meus dados" and the public identity (#24, #32; decisions 2026-11-01).

* The owner sees and edits their own data under `/auth/profile/*`: name (private),
  @username (once every 30 days), nickname, phone and photo. CPF stays read-only and
  masked; the taste keywords are never returned.
* These routes need the e-mail code and an activated sign-up, but not the CPF: an
  account completing its sign-up uses the same screen.
* Whatever other people see goes through the same checks as an event: personal data
  and the moderation.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import get_db_service
from app.core.limiter import limiter
from app.main import app
from app.services import moderation
from app.services.ai_service import ai_service
from app.services.db_service import DBService, UniqueViolationError

USER = "11111111-1111-4111-8111-111111111111"
PHOTO = f"{USER}/22222222-2222-4222-8222-222222222222.webp"
OLD_PHOTO = f"{USER}/33333333-3333-4333-8333-333333333333.jpg"
AUTH = {"Authorization": "Bearer token"}
PAYLOAD = {"sub": USER, "email": "ana@example.com", "session_id": "s-1", "amr": [{"method": "oauth", "timestamp": 1}]}
PROFILE: dict[str, Any] = {
    "name": "Ana Lima",
    "username": "ana.lima",
    "username_changed_at": None,
    "nickname": "Aninha",
    "email": "ana@example.com",
    "cpf": "52998224725",
    "phone": "+5581912345678",
    "avatar_path": None,
    "signup_pending": False,
    "interest_keywords": ["frevo", "maracatu"],
}


def _signed(paths: list[str], **_: Any) -> dict[str, str]:
    return {p: f"https://storage.example/{p}?token=t" for p in paths}


@pytest.fixture
def db() -> Iterator[MagicMock]:
    limiter.reset()
    deps.reset_verified_sessions_cache()
    fake = MagicMock()
    fake.is_session_verified = AsyncMock(return_value=True)
    fake.get_account_state = AsyncMock(return_value="ok")
    fake.get_my_profile = AsyncMock(return_value=dict(PROFILE))
    fake.update_own_profile = AsyncMock(return_value=True)
    fake.change_username = AsyncMock(return_value=True)
    fake.get_username_holder = AsyncMock(return_value=None)
    fake.get_signup_ticket_created_at = AsyncMock(return_value=None)
    # A ticketless pending sign-up was created just now (its ticket is on the way).
    fake.get_auth_user_created_at = AsyncMock(return_value=datetime.now(timezone.utc))
    fake.delete_auth_user = AsyncMock()
    fake.generate_username = AsyncMock(return_value="ana.lima42")
    fake.find_identity_conflicts = AsyncMock(return_value=set())
    fake.find_pending_signups = AsyncMock(return_value=[])
    fake.sign_media_paths = AsyncMock(side_effect=_signed)
    fake.delete_storage_objects = AsyncMock()
    app.dependency_overrides[get_db_service] = lambda: fake
    with patch("app.api.deps.DBService", return_value=fake), patch(
        "app.api.deps.decode_access_token", return_value=PAYLOAD
    ):
        yield fake
    app.dependency_overrides.clear()


def _client() -> TestClient:
    return TestClient(app)


def _says(flagged: dict[str, list[str]]) -> Any:
    async def moderate(inputs: list[Any]) -> list[tuple[bool, list[str]]]:
        out = []
        for text in inputs:
            hit = next((cats for word, cats in flagged.items() if word in str(text)), None)
            out.append((False, hit) if hit else (True, []))
        return out

    return moderate


# ------------------------------------------------------------------- access --


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/auth/profile/me", None),
        ("put", "/auth/profile/name", {"name": "Ana"}),
        ("put", "/auth/profile/username", {"username": "ana.b"}),
        ("put", "/auth/profile/nickname", {"nickname": "Aninha"}),
        ("put", "/auth/profile/phone", {"phone": None}),
        ("put", "/auth/profile/avatar", {"path": PHOTO}),
        ("delete", "/auth/profile/avatar", None),
    ],
)
def test_a_pending_sign_up_cannot_touch_the_account(db: MagicMock, method: str, path: str, body: Any) -> None:
    db.get_account_state.return_value = "pending"

    response = _client().request(method.upper(), path, json=body, headers=AUTH)

    assert response.status_code == 403
    assert response.json()["detail"] == "signup_pending"


def test_an_account_without_cpf_reaches_its_data(db: MagicMock) -> None:
    db.get_account_state.return_value = "incomplete"
    db.get_my_profile.return_value = {**PROFILE, "cpf": None, "phone": None}

    response = _client().get("/auth/profile/me", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["complete"] is False
    assert response.json()["cpf_masked"] is None


def test_a_password_session_still_needs_the_code(db: MagicMock) -> None:
    db.is_session_verified.return_value = False
    password = {**PAYLOAD, "amr": [{"method": "password", "timestamp": 1}]}

    with patch("app.api.deps.decode_access_token", return_value=password):
        response = _client().get("/auth/profile/me", headers=AUTH)

    assert response.status_code == 403
    assert response.json()["detail"] == "email_code_required"


# ------------------------------------------------------------------- my data --


def test_my_data_shows_the_owner_everything_but_the_keywords(db: MagicMock) -> None:
    db.get_my_profile.return_value = {**PROFILE, "avatar_path": PHOTO}

    body = _client().get("/auth/profile/me", headers=AUTH).json()

    assert body == {
        "name": "Ana Lima",
        "username": "ana.lima",
        "username_change_available_at": None,
        "nickname": "Aninha",
        "email": "ana@example.com",
        "cpf_masked": "***.982.247-**",
        "phone_masked": "(81) 9****-5678",
        "avatar_url": f"https://storage.example/{PHOTO}?token=t",
        "complete": True,
    }
    assert db.sign_media_paths.await_args.kwargs["bucket"] == "avatars"


def test_my_data_says_when_the_username_can_change_again(db: MagicMock) -> None:
    changed = datetime.now(timezone.utc) - timedelta(days=10)
    db.get_my_profile.return_value = {**PROFILE, "username_changed_at": changed.isoformat()}

    body = _client().get("/auth/profile/me", headers=AUTH).json()

    available = datetime.fromisoformat(body["username_change_available_at"])
    assert abs(available - (changed + timedelta(days=30))) < timedelta(seconds=1)


def test_a_photo_that_cannot_be_signed_is_left_out(db: MagicMock) -> None:
    db.get_my_profile.return_value = {**PROFILE, "avatar_path": PHOTO}
    db.sign_media_paths.side_effect = RuntimeError("storage down")

    body = _client().get("/auth/profile/me", headers=AUTH).json()

    assert body["avatar_url"] is None


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/auth/profile/me", None),
        ("put", "/auth/profile/name", {"name": "Ana"}),
        ("put", "/auth/profile/username", {"username": "ana.bia"}),
        ("put", "/auth/profile/nickname", {"nickname": "Aninha"}),
        ("put", "/auth/profile/phone", {"phone": None}),
        ("put", "/auth/profile/avatar", {"path": PHOTO}),
    ],
)
def test_a_session_whose_account_is_gone_is_invalid(db: MagicMock, method: str, path: str, body: Any) -> None:
    db.get_my_profile.return_value = {}
    db.update_own_profile.return_value = False

    response = _client().request(method.upper(), path, json=body, headers=AUTH)

    assert response.status_code == 401
    assert response.json()["code"] == "session_invalid"


# ---------------------------------------------------------------------- name --


def test_the_name_is_saved_trimmed(db: MagicMock) -> None:
    response = _client().put("/auth/profile/name", json={"name": "  Ana Beatriz Lima "}, headers=AUTH)

    assert response.status_code == 204
    db.update_own_profile.assert_awaited_once_with(USER, {"name": "Ana Beatriz Lima"})


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_an_empty_or_long_name_is_refused(db: MagicMock, name: str) -> None:
    response = _client().put("/auth/profile/name", json={"name": name}, headers=AUTH)

    assert response.status_code == 422
    db.update_own_profile.assert_not_awaited()


# ------------------------------------------------------------------ username --


def test_a_new_username_is_saved_normalized(db: MagicMock) -> None:
    response = _client().put("/auth/profile/username", json={"username": " @Ana.Bia "}, headers=AUTH)

    assert response.status_code == 204
    args = db.change_username.await_args
    assert args.args == (USER, "ana.bia")
    assert args.kwargs["not_changed_since"] < datetime.now(timezone.utc) - timedelta(days=29)


@pytest.mark.parametrize(
    ("typed", "code"),
    [("ab", "username_invalid"), ("ana..b", "username_invalid"), ("81999998888", "username_invalid"),
     ("zé.lima", "username_invalid"), ("flyer1", "username_reserved"), ("admin_br", "username_reserved")],
)
def test_an_invalid_or_reserved_username_is_refused(db: MagicMock, typed: str, code: str) -> None:
    response = _client().put("/auth/profile/username", json={"username": typed}, headers=AUTH)

    assert response.status_code == 422
    assert response.json()["code"] == code
    db.change_username.assert_not_awaited()


def test_saving_the_same_username_changes_nothing(db: MagicMock) -> None:
    response = _client().put("/auth/profile/username", json={"username": "Ana.Lima"}, headers=AUTH)

    assert response.status_code == 204
    db.change_username.assert_not_awaited()


def test_the_username_changes_once_every_30_days(db: MagicMock) -> None:
    changed = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
    db.get_my_profile.return_value = {**PROFILE, "username_changed_at": changed.isoformat()}

    with patch("app.api.auth._now", return_value=changed + timedelta(days=3)):
        response = _client().put("/auth/profile/username", json={"username": "ana.bia"}, headers=AUTH)

    assert response.status_code == 409
    assert response.json()["code"] == "username_change_too_soon"
    assert "20/12/2026" in response.json()["detail"]
    db.change_username.assert_not_awaited()


def test_a_change_that_loses_a_race_with_another_change_is_too_soon(db: MagicMock) -> None:
    db.change_username.return_value = False

    response = _client().put("/auth/profile/username", json={"username": "ana.bia"}, headers=AUTH)

    assert response.status_code == 409
    assert response.json()["code"] == "username_change_too_soon"


def test_a_username_held_by_someone_is_taken(db: MagicMock) -> None:
    db.get_username_holder.return_value = {"id": "other", "signup_pending": False}

    response = _client().put("/auth/profile/username", json={"username": "ana.bia"}, headers=AUTH)

    assert response.status_code == 409
    assert response.json()["code"] == "username_taken"


def test_a_username_taken_in_a_race_is_taken(db: MagicMock) -> None:
    db.change_username.side_effect = UniqueViolationError()

    response = _client().put("/auth/profile/username", json={"username": "ana.bia"}, headers=AUTH)

    assert response.status_code == 409
    assert response.json()["code"] == "username_taken"


def test_a_recent_pending_sign_up_keeps_its_username(db: MagicMock) -> None:
    db.get_username_holder.return_value = {"id": "pending-1", "signup_pending": True}
    db.get_signup_ticket_created_at.return_value = datetime.now(timezone.utc) - timedelta(hours=2)

    response = _client().put("/auth/profile/username", json={"username": "ana.bia"}, headers=AUTH)

    assert response.status_code == 409
    db.delete_auth_user.assert_not_awaited()


def test_a_forgotten_pending_sign_up_gives_its_username_away(db: MagicMock) -> None:
    db.get_username_holder.return_value = {"id": "pending-1", "signup_pending": True}
    db.get_signup_ticket_created_at.return_value = datetime.now(timezone.utc) - timedelta(hours=30)

    response = _client().put("/auth/profile/username", json={"username": "ana.bia"}, headers=AUTH)

    assert response.status_code == 204
    db.delete_auth_user.assert_awaited_once_with("pending-1")


def test_a_username_the_moderation_refuses_is_not_saved(db: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "moderate_inputs", _says({"palavrao": ["harassment"]}))

    response = _client().put("/auth/profile/username", json={"username": "palavrao.feio"}, headers=AUTH)

    assert response.status_code == 422
    assert response.json()["code"] == "content_blocked"
    db.change_username.assert_not_awaited()


def test_moderation_down_is_never_a_pass(db: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "moderate_inputs", AsyncMock(side_effect=RuntimeError("down")))

    response = _client().put("/auth/profile/username", json={"username": "ana.bia"}, headers=AUTH)

    assert response.status_code == 503
    assert response.json()["code"] == "moderation_unavailable"


@pytest.mark.parametrize("typed", ["ana.81999998888", "ana81999998888", "a52998224725"])
def test_a_username_carrying_a_phone_or_cpf_is_refused(db: MagicMock, typed: str) -> None:
    # Eight digits in a row could be a phone or a CPF, glued to letters or not.
    response = _client().put("/auth/profile/username", json={"username": typed}, headers=AUTH)

    assert response.status_code == 422
    assert response.json()["code"] == "username_invalid"
    db.change_username.assert_not_awaited()


# ------------------------------------------------------------------ nickname --


def test_the_nickname_is_saved_trimmed(db: MagicMock) -> None:
    response = _client().put("/auth/profile/nickname", json={"nickname": "  Aninha do Frevo "}, headers=AUTH)

    assert response.status_code == 204
    db.update_own_profile.assert_awaited_once_with(USER, {"nickname": "Aninha do Frevo"})


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_empty_nickname_removes_it(db: MagicMock, value: str | None) -> None:
    response = _client().put("/auth/profile/nickname", json={"nickname": value}, headers=AUTH)

    assert response.status_code == 204
    db.update_own_profile.assert_awaited_once_with(USER, {"nickname": None})


@pytest.mark.parametrize(
    ("nickname", "code"),
    [
        ("Suporte Flyer", "nickname_reserved"),
        ("SuporteFlyer", "nickname_reserved"),
        ("Moderação", "nickname_reserved"),
        ("Fl\u200byer", "nickname_invalid"),
        ("\u202enimda", "nickname_invalid"),
        ("\u0410dmin", "nickname_invalid"),
        ("\u3164", "nickname_invalid"),
        ("...", "nickname_invalid"),
        ("Flyer™", "nickname_reserved"),
        ("Ana 81 99999-8888", "personal_data"),
        ("x" * 41, "nickname_too_long"),
    ],
)
def test_a_bad_nickname_is_refused(db: MagicMock, nickname: str, code: str) -> None:
    response = _client().put("/auth/profile/nickname", json={"nickname": nickname}, headers=AUTH)

    assert response.status_code == 422
    assert response.json()["code"] == code
    db.update_own_profile.assert_not_awaited()


def test_a_nickname_the_moderation_refuses_is_not_saved(db: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_service, "moderate_inputs", _says({"Ódio": ["hate"]}))

    response = _client().put("/auth/profile/nickname", json={"nickname": "Ódio total"}, headers=AUTH)

    assert response.status_code == 422
    assert response.json()["code"] == "content_blocked"
    assert "vulgo" in response.json()["detail"].lower()


# --------------------------------------------------------------------- phone --


def test_a_phone_is_added(db: MagicMock) -> None:
    response = _client().put("/auth/profile/phone", json={"phone": "(81) 98765-4321"}, headers=AUTH)

    assert response.status_code == 204
    db.find_identity_conflicts.assert_awaited_once_with(
        email=None, cpf=None, phone="+5581987654321", exclude_user_id=USER, active_only=True
    )
    db.update_own_profile.assert_awaited_once_with(USER, {"phone": "+5581987654321"})


@pytest.mark.parametrize("value", [None, ""])
def test_the_phone_is_removed(db: MagicMock, value: str | None) -> None:
    response = _client().put("/auth/profile/phone", json={"phone": value}, headers=AUTH)

    assert response.status_code == 204
    db.find_identity_conflicts.assert_not_awaited()
    db.update_own_profile.assert_awaited_once_with(USER, {"phone": None})


def test_a_phone_of_another_account_is_refused(db: MagicMock) -> None:
    db.find_identity_conflicts.return_value = {"phone"}

    response = _client().put("/auth/profile/phone", json={"phone": "(81) 98765-4321"}, headers=AUTH)

    assert response.status_code == 409
    assert response.json()["detail"] == "Este telefone já tem uma conta."
    db.update_own_profile.assert_not_awaited()


def test_a_phone_taken_in_a_race_is_refused(db: MagicMock) -> None:
    db.update_own_profile.side_effect = UniqueViolationError()

    response = _client().put("/auth/profile/phone", json={"phone": "(81) 98765-4321"}, headers=AUTH)

    assert response.status_code == 409


def test_an_invalid_phone_is_refused(db: MagicMock) -> None:
    response = _client().put("/auth/profile/phone", json={"phone": "1234"}, headers=AUTH)

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("current", "adopted"),
    [("usuario123456", {"username": "ana.lima42"}), ("ana.escolhido", None)],
)
def test_completing_the_cpf_turns_a_neutral_username_into_one_from_the_name(
    db: MagicMock, current: str, adopted: dict[str, str] | None
) -> None:
    # Google accounts reach the backend first here: their name was never checked.
    db.get_account_state.return_value = "incomplete"
    db.complete_profile_identity = AsyncMock(return_value=True)
    db.get_my_profile.return_value = {**PROFILE, "username": current, "cpf": None}

    response = _client().put("/auth/profile/identity", json={"cpf": "529.982.247-25"}, headers=AUTH)

    assert response.status_code == 204
    if adopted:
        db.update_own_profile.assert_awaited_once_with(USER, adopted)
    else:
        db.update_own_profile.assert_not_awaited()


def test_a_failure_choosing_the_name_username_never_fails_the_cpf_step(db: MagicMock) -> None:
    db.get_account_state.return_value = "incomplete"
    db.complete_profile_identity = AsyncMock(return_value=True)
    db.get_my_profile.return_value = {**PROFILE, "username": "usuario123456"}
    db.update_own_profile.side_effect = UniqueViolationError()

    response = _client().put("/auth/profile/identity", json={"cpf": "529.982.247-25"}, headers=AUTH)

    assert response.status_code == 204


def test_a_cpf_taken_in_a_race_is_a_conflict_not_a_crash(db: MagicMock) -> None:
    db.get_account_state.return_value = "incomplete"
    db.complete_profile_identity = AsyncMock(side_effect=UniqueViolationError())

    response = _client().put("/auth/profile/identity", json={"cpf": "529.982.247-25"}, headers=AUTH)

    assert response.status_code == 409


# --------------------------------------------------------------------- photo --


def test_a_photo_is_moderated_saved_and_the_old_one_deleted(db: MagicMock) -> None:
    db.get_my_profile.return_value = {**PROFILE, "avatar_path": OLD_PHOTO}
    seen: list[Any] = []

    async def verdicts(_db: Any, paths: Any, **kwargs: Any) -> list[Any]:
        seen.append((list(paths), kwargs))
        return [moderation.Verdict(True)]

    with patch.object(moderation, "path_verdicts", verdicts):
        response = _client().put("/auth/profile/avatar", json={"path": PHOTO}, headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"avatar_url": f"https://storage.example/{PHOTO}?token=t"}
    assert seen == [([PHOTO], {"bucket": "avatars"})]
    db.update_own_profile.assert_awaited_once_with(USER, {"avatar_path": PHOTO})
    db.delete_storage_objects.assert_awaited_once_with("avatars", [OLD_PHOTO])


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("33333333-0000-4000-8000-000000000000/22222222-2222-4222-8222-222222222222.webp", 403),
        (f"{USER}/foto.webp", 422),
        (f"{USER}/22222222-2222-4222-8222-222222222222.gif", 422),
        (f"{USER}/../x/22222222-2222-4222-8222-222222222222.webp", 422),
        ("https://example.com/foto.jpg", 422),
    ],
)
def test_a_photo_outside_the_owners_folder_is_refused(db: MagicMock, path: str, status: int) -> None:
    response = _client().put("/auth/profile/avatar", json={"path": path}, headers=AUTH)

    assert response.status_code == status
    db.update_own_profile.assert_not_awaited()


@pytest.mark.parametrize(
    ("verdict", "deleted"),
    [
        (moderation.Verdict(False, ("sexual",)), True),
        (moderation.Verdict(False, unreadable=True), True),
        (moderation.Verdict(False, unreadable=True, missing=True), False),
    ],
)
def test_a_refused_photo_is_deleted_and_not_saved(db: MagicMock, verdict: Any, deleted: bool) -> None:
    with patch.object(moderation, "path_verdicts", AsyncMock(return_value=[verdict])):
        response = _client().put("/auth/profile/avatar", json={"path": PHOTO}, headers=AUTH)

    assert response.status_code == 422
    db.update_own_profile.assert_not_awaited()
    if deleted:
        db.delete_storage_objects.assert_awaited_once_with("avatars", [PHOTO])
    else:
        db.delete_storage_objects.assert_not_awaited()


def test_a_photo_cannot_be_saved_while_the_moderation_is_down(db: MagicMock) -> None:
    with patch.object(moderation, "path_verdicts", AsyncMock(side_effect=moderation.ModerationUnavailable())):
        response = _client().put("/auth/profile/avatar", json={"path": PHOTO}, headers=AUTH)

    assert response.status_code == 503
    db.update_own_profile.assert_not_awaited()


def test_a_failed_deletion_never_fails_the_request(db: MagicMock) -> None:
    db.get_my_profile.return_value = {**PROFILE, "avatar_path": OLD_PHOTO}
    db.delete_storage_objects.side_effect = RuntimeError("storage down")

    response = _client().put("/auth/profile/avatar", json={"path": PHOTO}, headers=AUTH)

    assert response.status_code == 200


def test_the_photo_is_removed(db: MagicMock) -> None:
    db.get_my_profile.return_value = {**PROFILE, "avatar_path": OLD_PHOTO}

    response = _client().delete("/auth/profile/avatar", headers=AUTH)

    assert response.status_code == 204
    db.update_own_profile.assert_awaited_once_with(USER, {"avatar_path": None})
    db.delete_storage_objects.assert_awaited_once_with("avatars", [OLD_PHOTO])


# ---------------------------------------------------- username availability --


def test_a_free_username_is_available_without_logging_in(db: MagicMock) -> None:
    response = TestClient(app).get("/auth/username-available", params={"u": "@Ana.Bia"})

    assert response.status_code == 200
    assert response.json() == {"available": True, "reason": None, "suggestion": None}
    db.get_username_holder.assert_awaited_once_with("ana.bia")


@pytest.mark.parametrize(("typed", "reason"), [("a!", "invalid"), ("flyer1", "reserved")])
def test_an_invalid_or_reserved_username_is_not_available(db: MagicMock, typed: str, reason: str) -> None:
    body = TestClient(app).get("/auth/username-available", params={"u": typed}).json()

    assert body["available"] is False
    assert body["reason"] == reason
    assert body["suggestion"] == "ana.lima42"


def test_a_taken_username_comes_with_a_suggestion(db: MagicMock) -> None:
    db.get_username_holder.return_value = {"id": "other", "signup_pending": False}

    body = TestClient(app).get("/auth/username-available", params={"u": "ana.lima"}).json()

    assert body == {"available": False, "reason": "taken", "suggestion": "ana.lima42"}
    db.generate_username.assert_awaited_once_with("ana.lima")


def test_a_forgotten_pending_sign_up_does_not_hold_a_username(db: MagicMock) -> None:
    db.get_username_holder.return_value = {"id": "pending-1", "signup_pending": True}
    db.get_signup_ticket_created_at.return_value = datetime.now(timezone.utc) - timedelta(days=3)

    body = TestClient(app).get("/auth/username-available", params={"u": "ana.lima"}).json()

    assert body["available"] is True
    db.delete_auth_user.assert_not_awaited()


def test_a_sign_up_still_being_created_holds_its_username(db: MagicMock) -> None:
    # Between the account and its ticket there is no ticket yet: not "forgotten".
    db.get_username_holder.return_value = {"id": "pending-1", "signup_pending": True}
    db.get_signup_ticket_created_at.return_value = None
    db.get_auth_user_created_at.return_value = datetime.now(timezone.utc) - timedelta(minutes=2)

    body = TestClient(app).get("/auth/username-available", params={"u": "ana.lima"}).json()

    assert body["available"] is False
    assert body["reason"] == "taken"


@pytest.mark.parametrize("created", [timedelta(minutes=30), None])
def test_a_sign_up_that_never_got_its_ticket_gives_the_username_away(db: MagicMock, created: timedelta | None) -> None:
    db.get_username_holder.return_value = {"id": "pending-1", "signup_pending": True}
    db.get_signup_ticket_created_at.return_value = None
    db.get_auth_user_created_at.return_value = datetime.now(timezone.utc) - created if created else None

    body = TestClient(app).get("/auth/username-available", params={"u": "ana.lima"}).json()

    assert body["available"] is True


# ------------------------------------------------------------------ sign-up --

SIGNUP = {
    "name": "Ana Lima",
    "email": "ana@example.com",
    "cpf": "529.982.247-25",
    "password": "frevo2026",
}


@pytest.fixture
def signup_db(db: MagicMock) -> MagicMock:
    db.signup_user = AsyncMock(return_value="user-9")
    db.create_signup_ticket = AsyncMock()
    return db


def test_sign_up_saves_the_chosen_username_and_nickname_after_the_ticket(signup_db: MagicMock) -> None:
    order: list[str] = []
    signup_db.create_signup_ticket.side_effect = lambda *_: order.append("ticket")
    signup_db.update_own_profile.side_effect = lambda *_: order.append("identity") or True

    response = TestClient(app).post("/auth/signup", json={**SIGNUP, "username": "@Ana.Lima", "nickname": " Aninha "})

    assert response.status_code == 201
    data = signup_db.signup_user.await_args.kwargs["data"]
    # Never through the metadata: Supabase Auth stores it as given, without the moderation.
    assert "username" not in data
    assert "nickname" not in data
    signup_db.update_own_profile.assert_awaited_once_with("user-9", {"username": "ana.lima", "nickname": "Aninha"})
    # Until the ticket exists the account counts as "being created" for everyone else.
    assert order == ["ticket", "identity"]


def test_a_username_taken_during_the_sign_up_keeps_the_generated_one(signup_db: MagicMock) -> None:
    signup_db.update_own_profile.side_effect = [UniqueViolationError(), True]

    response = TestClient(app).post("/auth/signup", json={**SIGNUP, "username": "ana.lima", "nickname": "Aninha"})

    assert response.status_code == 201
    assert signup_db.update_own_profile.await_args_list[-1].args == ("user-9", {"nickname": "Aninha"})


def test_signing_up_again_keeps_the_username_of_the_sign_up_it_replaces(signup_db: MagicMock) -> None:
    # Same person, same e-mail, within 24 h: their own pending sign-up holds the username.
    signup_db.get_username_holder.return_value = {"id": "pending-1", "signup_pending": True}
    signup_db.get_signup_ticket_created_at.return_value = datetime.now(timezone.utc) - timedelta(hours=1)
    signup_db.find_pending_signups.return_value = ["pending-1"]

    response = TestClient(app).post("/auth/signup", json={**SIGNUP, "username": "ana.lima"})

    assert response.status_code == 201
    signup_db.delete_auth_user.assert_awaited_once_with("pending-1")


def test_sign_up_without_a_username_gets_one_from_the_name_after_the_checks(signup_db: MagicMock) -> None:
    # The app before #32 sends none: the account starts neutral and gets one made from
    # the name once it passes the same checks as a chosen one.
    response = TestClient(app).post("/auth/signup", json=SIGNUP)

    assert response.status_code == 201
    assert "username" not in signup_db.signup_user.await_args.kwargs["data"]
    signup_db.generate_username.assert_awaited_once_with("Ana Lima")
    signup_db.update_own_profile.assert_awaited_once_with("user-9", {"username": "ana.lima42"})


def test_a_name_the_moderation_refuses_never_becomes_the_username(
    signup_db: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    signup_db.generate_username.return_value = "palavrao.feio"
    monkeypatch.setattr(ai_service, "moderate_inputs", _says({"palavrao": ["harassment"]}))

    response = TestClient(app).post("/auth/signup", json={**SIGNUP, "name": "Palavrão Feio"})

    assert response.status_code == 201
    signup_db.update_own_profile.assert_not_awaited()


def test_a_name_that_gives_no_username_keeps_the_neutral_one(signup_db: MagicMock) -> None:
    signup_db.generate_username.return_value = "usuario"

    TestClient(app).post("/auth/signup", json={**SIGNUP, "name": "😀"})

    signup_db.update_own_profile.assert_not_awaited()


def test_sign_up_with_a_taken_username_is_refused(signup_db: MagicMock) -> None:
    signup_db.get_username_holder.return_value = {"id": "other", "signup_pending": False}

    response = TestClient(app).post("/auth/signup", json={**SIGNUP, "username": "ana.lima"})

    assert response.status_code == 409
    assert "@usuário" in response.json()["detail"]
    signup_db.signup_user.assert_not_awaited()


def test_sign_up_replaces_a_forgotten_pending_sign_up_holding_the_username(signup_db: MagicMock) -> None:
    signup_db.get_username_holder.return_value = {"id": "pending-1", "signup_pending": True}
    signup_db.get_signup_ticket_created_at.return_value = datetime.now(timezone.utc) - timedelta(days=2)

    response = TestClient(app).post("/auth/signup", json={**SIGNUP, "username": "ana.lima"})

    assert response.status_code == 201
    signup_db.delete_auth_user.assert_awaited_once_with("pending-1")


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        ({"username": "flyer1"}, "username_reserved"),
        ({"username": "a!"}, "username_invalid"),
        ({"nickname": "Suporte Flyer"}, "nickname_reserved"),
        ({"nickname": "x" * 41}, "nickname_too_long"),
        ({"nickname": "Ana\u200bLima"}, "nickname_invalid"),
    ],
)
def test_sign_up_with_a_bad_username_or_nickname_is_refused(signup_db: MagicMock, extra: dict[str, str], code: str) -> None:
    response = TestClient(app).post("/auth/signup", json={**SIGNUP, **extra})

    assert response.status_code == 422
    assert response.json()["code"] == code
    signup_db.signup_user.assert_not_awaited()


def test_a_nickname_that_fails_to_save_does_not_fail_the_sign_up(signup_db: MagicMock) -> None:
    signup_db.update_own_profile.side_effect = RuntimeError("db down")

    response = TestClient(app).post("/auth/signup", json={**SIGNUP, "nickname": "Aninha"})

    assert response.status_code == 201


# ----------------------------------------------------------------- status --


def test_the_status_carries_the_username_and_the_photo(db: MagicMock) -> None:
    db.get_profile_identity = AsyncMock(
        return_value={"cpf": "52998224725", "phone": None, "signup_pending": False, "username": "ana.lima", "avatar_path": PHOTO}
    )

    body = _client().get("/auth/profile/status", headers=AUTH).json()

    assert body["username"] == "ana.lima"
    assert body["avatar_url"] == f"https://storage.example/{PHOTO}?token=t"


def test_the_status_without_a_photo_signs_nothing(db: MagicMock) -> None:
    db.get_profile_identity = AsyncMock(
        return_value={"cpf": "52998224725", "phone": None, "signup_pending": False, "username": "ana.lima", "avatar_path": None}
    )

    body = _client().get("/auth/profile/status", headers=AUTH).json()

    assert body["avatar_url"] is None
    db.sign_media_paths.assert_not_awaited()


# ---------------------------------------------------- what the database is asked --


def _http(method: str, json_body: Any) -> MagicMock:
    client = MagicMock()
    response = httpx.Response(200, json=json_body, request=httpx.Request(method.upper(), "http://x"))
    setattr(client, method, AsyncMock(return_value=response))
    client.request = AsyncMock(return_value=response)
    return client


async def test_the_30_days_are_checked_in_the_same_write() -> None:
    client = _http("patch", [{"id": USER}])
    since = datetime(2026, 10, 3, tzinfo=timezone.utc)

    assert await DBService(client).change_username(USER, "ana.bia", not_changed_since=since) is True

    params = client.patch.await_args.kwargs["params"]
    assert params["id"] == f"eq.{USER}"
    assert params["or"] == f"(username_changed_at.is.null,username_changed_at.lt.{since.isoformat()})"
    assert client.patch.await_args.kwargs["json"]["username"] == "ana.bia"
    assert "username_changed_at" in client.patch.await_args.kwargs["json"]


async def test_a_change_that_matched_no_row_was_too_soon() -> None:
    client = _http("patch", [])

    assert await DBService(client).change_username(USER, "ana.bia", not_changed_since=datetime.now(timezone.utc)) is False


async def test_my_data_never_selects_the_keywords() -> None:
    client = _http("get", [PROFILE])

    await DBService(client).get_my_profile(USER)

    select = client.get.await_args.kwargs["params"]["select"]
    assert "interest" not in select
    assert "keywords" not in select


async def test_photos_are_deleted_from_the_avatars_bucket() -> None:
    client = _http("delete", {})

    await DBService(client).delete_storage_objects("avatars", [PHOTO])

    method, url = client.request.await_args.args
    assert (method, url) == ("DELETE", "/storage/v1/object/avatars")
    assert client.request.await_args.kwargs["json"] == {"prefixes": [PHOTO]}


async def test_photos_are_signed_in_their_bucket() -> None:
    client = _http("post", [{"path": PHOTO, "signedURL": "/object/sign/avatars/x?token=t"}])

    out = await DBService(client).sign_media_paths([PHOTO], bucket="avatars")

    assert client.post.await_args.args[0] == "/storage/v1/object/sign/avatars"
    assert out[PHOTO].endswith("/storage/v1/object/sign/avatars/x?token=t")


async def test_suggestions_come_from_the_database_rule() -> None:
    client = _http("post", "ana.lima42")

    assert await DBService(client).generate_username("ana.lima") == "ana.lima42"
    assert client.post.await_args.args[0] == "/rest/v1/rpc/generate_username"
    assert client.post.await_args.kwargs["json"] == {"full_name": "ana.lima"}
