"""Accounts by e-mail (decisions 2026-10-12).

* Sign-up: name, e-mail, CPF, mobile and password; CPF, mobile and e-mail are unique.
  Public route (there is no account yet), rate limited per client. The e-mail carries
  a 6-digit code, no link, and the code only counts in the browser that signed up
  (decisions 2026-10-14): that browser gets a secret ticket, and the account stays pending
  until `/auth/signup/activate` presents it.
* Second factor: a session opened with a password must verify the 6-digit code that
  Supabase Auth e-mails to the account before using the API. The address always comes
  from the token, never from the client.
* CPF and phone on every account: Google accounts complete them on first access.
* "Meus dados" and the public identity (#24, #32; decisions 2026-11-01): the owner
  edits their name (private), @username (once every 30 days), nickname, phone and
  photo. What other people see goes through personal data and the moderation first.

`app/api/deps.py` enforces the code and the complete profile on every other route;
the routes here skip exactly the check they exist to satisfy.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.api.authz import require_avatar_path
from app.api.deps import (
    DbServiceDep,
    PendingSession,
    get_current_user_incomplete_profile,
    get_current_user_pending_code,
    get_current_user_pending_profile,
    remember_complete_profile,
    remember_verified_session,
)
from app.core.br_documents import mask_cpf, mask_phone
from app.core.config import settings
from app.core.errors import SESSION_INVALID_MESSAGE, ApiError, session_invalid
from app.core.limiter import limiter
from app.core.personal_data import find_personal_data
from app.core.tz import LOCAL_TZ, parse_aware
from app.core.usernames import (
    NICKNAME_MAX_LENGTH,
    has_hidden_characters,
    normalize_username,
    username_problem,
    uses_authority_word,
)
from app.models.schemas import (
    AvatarIn,
    AvatarRead,
    EmailCodeIn,
    EmailCodeStatus,
    MyProfileRead,
    NameIn,
    NicknameIn,
    PhoneIn,
    ProfileIdentityIn,
    ProfileIdentityStatus,
    SignupActivateIn,
    SignupIn,
    SignupOut,
    UsernameAvailability,
    UsernameIn,
)
from app.services import moderation
from app.services.db_service import UniqueViolationError
from app.services.moderation import (
    CONTENT_BLOCKED,
    MODERATION_UNAVAILABLE,
    MODERATION_UNAVAILABLE_MESSAGE,
    PERSONAL_DATA,
    ModerationUnavailable,
    reasons,
    text_verdicts,
)
from app.services.public_profile import discard_avatars, sign_avatars

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

PendingDep = Annotated[PendingSession, Depends(get_current_user_pending_code)]
PendingProfileDep = Annotated[str, Depends(get_current_user_pending_profile)]
# "Meus dados": the code and an activated sign-up, the CPF may still be missing (#24).
MyAccountDep = Annotated[str, Depends(get_current_user_incomplete_profile)]

# Said plainly (decision 2026-10-12), in this order when several are taken.
_TAKEN = {
    "email": "Este e-mail já tem uma conta.",
    "cpf": "Este CPF já tem uma conta.",
    "phone": "Este telefone já tem uma conta.",
    "username": "Este @usuário já está em uso.",
}
_TAKEN_ORDER = ("email", "cpf", "phone", "username")

# The @username changes once every 30 days (decision 2026-11-01).
USERNAME_CHANGE_INTERVAL = timedelta(days=30)
# A sign-up that waited longer than its ticket (24 h in the app) will never be
# finished: it no longer holds its @username.
STALE_SIGNUP_AFTER = timedelta(hours=24)

USERNAME_TAKEN_MESSAGE = _TAKEN["username"] + " Escolha outro."
USERNAME_RULE_MESSAGE = (
    "Use de 3 a 20 letras minúsculas sem acento, números (até 7 seguidos), ponto ou _, com "
    "pelo menos uma letra e sem ponto no começo, no fim ou repetido."
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _conflict(taken: set[str]) -> HTTPException:
    message = " ".join(_TAKEN[field] for field in _TAKEN_ORDER if field in taken)
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)


def _ticket_hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode()).hexdigest()


async def _forgotten_sign_up(db: DbServiceDep, holder: dict[str, Any]) -> bool:
    """A pending sign-up whose ticket is older than its lifetime can never be finished.
    Without a ticket it is still being created (the ticket comes right after)."""
    if not holder.get("signup_pending"):
        return False
    created = await db.get_signup_ticket_created_at(str(holder["id"]))
    return created is not None and _now() - created > STALE_SIGNUP_AFTER


async def _username_holder(
    db: DbServiceDep, username: str, *, user_id: str | None = None, replaced: frozenset[str] = frozenset()
) -> tuple[bool, str | None]:
    """(taken, forgotten sign-up to delete). A recent pending sign-up keeps its
    @username: anyone could otherwise cancel someone's sign-up by picking the same one.
    ``replaced``: pending sign-ups this request deletes anyway (the same person trying
    again with the same e-mail, CPF or phone)."""
    holder = await db.get_username_holder(username)
    if not holder or str(holder.get("id")) == user_id or str(holder.get("id")) in replaced:
        return False, None
    if await _forgotten_sign_up(db, holder):
        return False, str(holder["id"])
    return True, None


async def _claim_identity(
    db: DbServiceDep,
    *,
    email: str | None,
    cpf: str | None,
    phone: str | None,
    username: str | None = None,
    exclude_user_id: str | None = None,
) -> None:
    """409 if an active account has them; sign-ups still waiting for their code give
    way (wrong e-mail, another device, lost code) and are deleted. For the @username,
    only once the sign-up was forgotten (``_username_holder``)."""
    taken: set[str] = set()
    replaced: list[str] = []
    if email or cpf or phone:
        conflict_args: dict[str, str] = {"exclude_user_id": exclude_user_id} if exclude_user_id else {}
        taken = set(
            await db.find_identity_conflicts(email=email, cpf=cpf, phone=phone, **conflict_args, active_only=True)
        )
        replaced = [
            stale_id
            for stale_id in await db.find_pending_signups(email=email, cpf=cpf, phone=phone)
            if stale_id != exclude_user_id
        ]
    forgotten: str | None = None
    if username:
        username_taken, forgotten = await _username_holder(
            db, username, user_id=exclude_user_id, replaced=frozenset(replaced)
        )
        if username_taken:
            taken.add("username")
    if taken:
        raise _conflict(taken)
    for stale_id in replaced:
        await db.delete_auth_user(stale_id)
    if forgotten:
        await db.delete_auth_user(forgotten)


# ------------------------------------------- what other people will see --


def _require_username_rules(username: str) -> None:
    problem = username_problem(username)
    if problem == "invalid":
        raise ApiError(422, "username_invalid", USERNAME_RULE_MESSAGE)
    if problem == "reserved":
        raise ApiError(422, "username_reserved", "Este @usuário é reservado. Escolha outro.")


def _nickname_value(raw: str | None) -> str | None:
    """The nickname to store (None removes it), or 422 with the reason."""
    nickname = " ".join((raw or "").split())
    if not nickname:
        return None
    if len(nickname) > NICKNAME_MAX_LENGTH:
        raise ApiError(422, "nickname_too_long", f"O vulgo pode ter até {NICKNAME_MAX_LENGTH} caracteres.")
    if has_hidden_characters(nickname):
        raise ApiError(422, "nickname_invalid", "O vulgo não pode ter caracteres invisíveis ou de controle.")
    if uses_authority_word(nickname):
        raise ApiError(
            422, "nickname_reserved", "O vulgo não pode usar palavras como Flyer, admin, suporte ou moderação."
        )
    return nickname


async def _check_public_texts(*, username: str | None = None, nickname: str | None = None) -> None:
    """Personal data, then the moderation, for the @username and nickname everyone sees.
    Moderation down is 503, never a pass (#22 A)."""
    fields: list[tuple[str, str]] = []
    if username:
        # Words the moderation can read: "palavra.feia" is checked as "palavra feia".
        fields.append(("O @usuário", username.replace(".", " ").replace("_", " ")))
    if nickname:
        fields.append(("O vulgo", nickname))
    for label, text in fields:
        if find_personal_data(text):
            raise ApiError(422, PERSONAL_DATA, f"{label} não pode ter e-mail, telefone ou CPF.")
    if not fields:
        return
    try:
        verdicts = await text_verdicts([text for _, text in fields])
    except ModerationUnavailable:
        raise ApiError(503, MODERATION_UNAVAILABLE, MODERATION_UNAVAILABLE_MESSAGE) from None
    for (label, _), verdict in zip(fields, verdicts, strict=True):
        if not verdict.allowed:
            raise ApiError(
                422,
                CONTENT_BLOCKED,
                f"{label} foi recusado pela moderação ({reasons(verdict.categories)}). Escolha outro.",
            )


async def _update_own(db: DbServiceDep, user_id: str, data: dict[str, Any]) -> None:
    """Write the owner's fields; 401 `session_invalid` when the account is gone."""
    if not await db.update_own_profile(user_id, data):
        raise session_invalid()


def _username_too_soon(available_at: datetime | None) -> ApiError:
    if available_at is None:
        message = "Você trocou seu @usuário há menos de 30 dias."
    else:
        message = f"Você poderá trocar seu @usuário de novo em {available_at.astimezone(LOCAL_TZ):%d/%m/%Y}."
    return ApiError(409, "username_change_too_soon", message)


# ----------------------------------------------------------------- sign-up --


@router.post("/signup", status_code=status.HTTP_201_CREATED, response_model=SignupOut)
@limiter.limit("5/10minute")
async def signup(request: Request, body: SignupIn, db: DbServiceDep) -> SignupOut:
    """Create a pending account; Supabase e-mails the 6-digit code.

    Answers the ticket the browser must present with the code (`/auth/signup/activate`).
    Only its hash is stored. The @username and nickname are checked like in "Meus
    dados"; without a @username (the app before #32) the database makes one.
    """
    username = normalize_username(body.username) if body.username and body.username.strip() else None
    if username:
        _require_username_rules(username)
    nickname = _nickname_value(body.nickname)
    await _check_public_texts(username=username, nickname=nickname)
    await _claim_identity(db, email=body.email, cpf=body.cpf, phone=body.phone, username=username)
    try:
        user_id = await db.signup_user(
            email=body.email,
            password=body.password,
            data={
                "name": body.name,
                "full_name": body.name,
                "cpf": body.cpf,
                # Optional (#29): the profile trigger stores a missing one as null.
                **({"phone": body.phone} if body.phone else {}),
                # The profile trigger creates the profile as pending.
                "flyer_signup": True,
            },
        )
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == status.HTTP_429_TOO_MANY_REQUESTS:
            raise HTTPException(
                status_code=code, detail="Muitas tentativas seguidas. Aguarde um minuto e tente de novo."
            ) from None
        if code in (400, 422):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Não foi possível criar a conta: verifique o e-mail e a senha.",
            ) from None
        # Two sign-ups with the same CPF or phone at once: the unique index stops the
        # second inside Supabase ("Database error saving new user").
        logger.warning("Supabase sign-up failed (%s)", code)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Este CPF, telefone ou e-mail já tem uma conta.",
        ) from None
    # The ticket first: until it exists the new account counts as "being created".
    ticket = secrets.token_urlsafe(32)
    await db.create_signup_ticket(user_id, _ticket_hash(ticket))
    await _save_chosen_identity(db, user_id, username, nickname)
    return SignupOut(ticket=ticket)


async def _save_chosen_identity(db: DbServiceDep, user_id: str, username: str | None, nickname: str | None) -> None:
    """The @username and nickname chosen at sign-up, already moderated. Never through the
    sign-up metadata, which Supabase Auth stores as given. Best effort: the account keeps
    the @username made from the name, and both can be set in "Meus dados"."""
    data = {key: value for key, value in (("username", username), ("nickname", nickname)) if value}
    if not data:
        return
    try:
        await db.update_own_profile(user_id, data)
    except UniqueViolationError:
        # The @username was taken a moment ago: keep the one made from the name.
        if nickname:
            await _save_chosen_identity(db, user_id, None, nickname)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Saving the chosen identity at sign-up failed (%s)", type(exc).__name__)


@router.post("/signup/activate", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/10minute")
async def activate_signup(
    request: Request, body: SignupActivateIn, user_id: PendingProfileDep, db: DbServiceDep
) -> Response:
    """Finish a sign-up: the session opened with the code plus the ticket of the browser
    that signed up. The code alone (whoever got the e-mail by mistake) is not enough."""
    if not await db.consume_signup_ticket(user_id, _ticket_hash(body.ticket)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Este código não foi pedido aqui. Digite o código no navegador em que você fez o cadastro, "
            "ou faça o cadastro de novo.",
        )
    await db.activate_signup(user_id)
    remember_complete_profile(user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------- CPF and phone --


@router.get("/profile/status", response_model=ProfileIdentityStatus)
@limiter.limit("30/minute")
async def profile_status(
    request: Request, user_id: PendingProfileDep, db: DbServiceDep
) -> ProfileIdentityStatus:
    """Whether the account has its CPF (the phone is optional, #29), with the owner's own
    data masked, and whether its sign-up still waits for the code."""
    identity = await db.get_profile_identity(user_id)
    cpf, phone = identity.get("cpf"), identity.get("phone")
    complete = bool(cpf)
    pending = bool(identity.get("signup_pending"))
    if complete and not pending:
        remember_complete_profile(user_id)
    photo = identity.get("avatar_path")
    avatars = await sign_avatars(db, [photo])
    return ProfileIdentityStatus(
        complete=complete,
        pending=pending,
        cpf_masked=mask_cpf(cpf),
        phone_masked=mask_phone(phone),
        intro_seen=bool(identity.get("intro_seen_at")),
        onboarded=bool(identity.get("onboarding_preferences_hash")),
        username=identity.get("username"),
        avatar_url=avatars.get(photo) if photo else None,
    )


@router.post("/profile/intro-seen", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def intro_seen(request: Request, user_id: PendingProfileDep, db: DbServiceDep) -> Response:
    """The introduction tour was finished or skipped (Warpedro1/flyer#23): never shown
    again, on any device. Idempotent."""
    await db.mark_intro_seen(user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/profile/identity", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/10minute")
async def complete_profile(
    request: Request, body: ProfileIdentityIn, user_id: PendingProfileDep, db: DbServiceDep
) -> Response:
    """Set the CPF, and the phone if given, once (Google accounts on first access)."""
    await _claim_identity(db, email=None, cpf=body.cpf, phone=body.phone, exclude_user_id=user_id)
    try:
        completed = await db.complete_profile_identity(user_id, body.cpf, body.phone)
    except UniqueViolationError:
        # Another account took the same CPF or phone a moment ago.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Este CPF ou telefone já tem uma conta."
        ) from None
    if not completed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="O cadastro já está completo: o CPF não pode ser alterado por aqui.",
        )
    remember_complete_profile(user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------ Meus dados --


@router.get("/profile/me", response_model=MyProfileRead)
@limiter.limit("60/minute")
async def my_profile(request: Request, user_id: MyAccountDep, db: DbServiceDep) -> MyProfileRead:
    """The owner's own data (#24): name, @username, nickname, e-mail, CPF and phone
    masked, and the photo. Never the taste keywords (decision 2026-11-01)."""
    row = await db.get_my_profile(user_id)
    if not row:
        raise session_invalid()
    photo = row.get("avatar_path")
    avatars = await sign_avatars(db, [photo])
    changed = parse_aware(row.get("username_changed_at"))
    available_at = changed + USERNAME_CHANGE_INTERVAL if changed else None
    return MyProfileRead(
        name=row.get("name"),
        username=str(row.get("username") or ""),
        username_change_available_at=available_at if available_at and available_at > _now() else None,
        nickname=row.get("nickname"),
        email=row.get("email"),
        cpf_masked=mask_cpf(row.get("cpf")),
        phone_masked=mask_phone(row.get("phone")),
        avatar_url=avatars.get(photo) if photo else None,
        complete=bool(row.get("cpf")),
    )


@router.put("/profile/name", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("20/hour")
async def change_name(request: Request, body: NameIn, user_id: MyAccountDep, db: DbServiceDep) -> Response:
    """The full name, which only the owner sees (decision 2026-11-01)."""
    await _update_own(db, user_id, {"name": body.name})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/profile/username", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/hour")
async def change_username(
    request: Request, body: UsernameIn, user_id: MyAccountDep, db: DbServiceDep
) -> Response:
    """A new @username: valid, not reserved, free, through personal data and the
    moderation, and at most once every 30 days (the old one is free at once)."""
    username = normalize_username(body.username)
    _require_username_rules(username)
    current = await db.get_my_profile(user_id)
    if not current:
        raise session_invalid()
    if current.get("username") == username:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    now = _now()
    changed = parse_aware(current.get("username_changed_at"))
    if changed and now < changed + USERNAME_CHANGE_INTERVAL:
        raise _username_too_soon(changed + USERNAME_CHANGE_INTERVAL)
    await _check_public_texts(username=username)
    taken, forgotten = await _username_holder(db, username, user_id=user_id)
    if taken:
        raise ApiError(409, "username_taken", USERNAME_TAKEN_MESSAGE)
    if forgotten:
        await db.delete_auth_user(forgotten)
    try:
        saved = await db.change_username(user_id, username, not_changed_since=now - USERNAME_CHANGE_INTERVAL)
    except UniqueViolationError:
        raise ApiError(409, "username_taken", USERNAME_TAKEN_MESSAGE) from None
    if not saved:
        # Another change of this account got there first.
        raise _username_too_soon(None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/profile/nickname", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("20/hour")
async def change_nickname(
    request: Request, body: NicknameIn, user_id: MyAccountDep, db: DbServiceDep
) -> Response:
    """The nickname ("vulgo", optional): empty removes it."""
    nickname = _nickname_value(body.nickname)
    await _check_public_texts(nickname=nickname)
    await _update_own(db, user_id, {"nickname": nickname})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/profile/phone", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/10minute")
async def change_phone(request: Request, body: PhoneIn, user_id: MyAccountDep, db: DbServiceDep) -> Response:
    """Add, change or remove (empty) the phone, which stays optional and unique (#29)."""
    if body.phone:
        await _claim_identity(db, email=None, cpf=None, phone=body.phone, exclude_user_id=user_id)
    try:
        await _update_own(db, user_id, {"phone": body.phone})
    except UniqueViolationError:
        raise _conflict({"phone"}) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/profile/avatar", response_model=AvatarRead)
@limiter.limit("10/hour")
async def change_avatar(request: Request, body: AvatarIn, user_id: MyAccountDep, db: DbServiceDep) -> AvatarRead:
    """Use a photo the app uploaded to the person's own folder: moderated first; a
    refused one is deleted, and the photo it replaces too."""
    require_avatar_path(user_id, body.path)
    try:
        [verdict] = await moderation.path_verdicts(db, [body.path], bucket=settings.AVATAR_BUCKET)
    except ModerationUnavailable:
        raise ApiError(503, MODERATION_UNAVAILABLE, MODERATION_UNAVAILABLE_MESSAGE) from None
    if verdict.missing:
        raise ApiError(422, CONTENT_BLOCKED, "A foto não está mais disponível. Envie de novo.")
    if verdict.unreadable or not verdict.allowed:
        await discard_avatars(db, [body.path])
        if verdict.unreadable:
            raise ApiError(422, CONTENT_BLOCKED, "Não conseguimos verificar esta foto. Envie outra em JPG, PNG ou WEBP.")
        raise ApiError(
            422,
            CONTENT_BLOCKED,
            f"A foto foi recusada pela moderação ({reasons(verdict.categories)}). Escolha outra.",
        )
    current = await db.get_my_profile(user_id)
    if not current:
        await discard_avatars(db, [body.path])
        raise session_invalid()
    previous = current.get("avatar_path")
    await _update_own(db, user_id, {"avatar_path": body.path})
    if previous != body.path:
        await discard_avatars(db, [previous])
    avatars = await sign_avatars(db, [body.path])
    return AvatarRead(avatar_url=avatars.get(body.path))


@router.delete("/profile/avatar", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/hour")
async def remove_avatar(request: Request, user_id: MyAccountDep, db: DbServiceDep) -> Response:
    """Remove the photo (the file goes too)."""
    previous = (await db.get_my_profile(user_id)).get("avatar_path")
    await _update_own(db, user_id, {"avatar_path": None})
    await discard_avatars(db, [previous])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/username-available", response_model=UsernameAvailability)
@limiter.limit("30/minute")
async def username_available(
    request: Request, db: DbServiceDep, u: Annotated[str, Query(max_length=40)]
) -> UsernameAvailability:
    """Whether a @username can be taken, while it is typed (sign-up, "Meus dados").

    Public: the sign-up has no session yet. It only says what every profile shows
    anyway; the moderation runs when it is saved.
    """
    username = normalize_username(u)
    problem = username_problem(username)
    if problem is None:
        taken, _ = await _username_holder(db, username)
        if not taken:
            return UsernameAvailability(available=True)
        problem = "taken"
    return UsernameAvailability(available=False, reason=problem, suggestion=await _suggestion(db, username))


async def _suggestion(db: DbServiceDep, typed: str) -> str | None:
    try:
        return await db.generate_username(typed)
    except Exception as exc:  # noqa: BLE001 — a suggestion is a nicety
        logger.warning("Suggesting a username failed (%s)", type(exc).__name__)
        return None


# ------------------------------------------------------- e-mail code --


def _require_code_session(session: PendingSession) -> tuple[str, str]:
    if not session.needs_code:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Esta sessão não precisa de código.",
        )
    if not session.email or not session.session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=SESSION_INVALID_MESSAGE,
        )
    return session.email, session.session_id


@router.get("/email-code/status", response_model=EmailCodeStatus)
@limiter.limit("30/minute")
async def email_code_status(
    request: Request, session: PendingDep, db: DbServiceDep
) -> EmailCodeStatus:
    """Whether this session still has to verify the e-mail code."""
    if not session.needs_code:
        return EmailCodeStatus(required=False, verified=True)
    verified = bool(session.session_id) and await db.is_session_verified(str(session.session_id))
    if verified:
        remember_verified_session(str(session.session_id))
    return EmailCodeStatus(required=True, verified=verified)


@router.post("/email-code/send", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("3/10minute")
async def send_email_code(request: Request, session: PendingDep, db: DbServiceDep) -> Response:
    """E-mail a 6-digit code to the account's own address.

    Supabase's refusals are said plainly (#30): a code sent less than a minute ago
    (429), or a session whose account no longer exists (400/404/422).
    """
    email, _ = _require_code_session(session)
    try:
        await db.send_email_code(email)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == status.HTTP_429_TOO_MANY_REQUESTS:
            raise ApiError(
                code,
                "email_code_recently_sent",
                "Já enviamos um código há menos de 1 minuto. Aguarde um pouco e peça outro.",
            ) from None
        if code in (400, 404, 422):
            raise session_invalid() from None
        raise
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/email-code/verify", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/10minute")
async def verify_email_code(
    request: Request, body: EmailCodeIn, session: PendingDep, db: DbServiceDep
) -> Response:
    """Check the code; on success this session can use the API."""
    email, session_id = _require_code_session(session)
    if not await db.verify_email_code(email, body.code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Código inválido ou expirado.",
        )
    await db.mark_session_verified(session_id, session.user_id)
    remember_verified_session(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
