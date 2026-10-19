"""Content moderation of events and places (#22 part A, decisions 2026-10-18).

OpenAI's moderation (`MODERATION_MODEL`, omni-moderation) checks the texts and the
photos of what enters the catalogue:

* created or edited in the app: refused on the spot, before anything is written
  (422 ``content_blocked``, the reason in pt-BR), together with third-party
  personal data (422 ``personal_data``); moderation down is 503
  ``moderation_unavailable``, never a pass;
* imported: what the moderation refuses never enters (an existing row is hidden),
  contacts are replaced by "[contato removido]", and a cover it refuses or cannot
  read is dropped (``etl/load.py``);
* the rows that were here before: ``POST /admin/events/moderation``.

Videos cannot be checked by the moderation model and go through unchecked.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from openai import APIStatusError, APITimeoutError

from app.core.config import settings
from app.core.errors import ApiError
from app.core.personal_data import find_personal_data, redact_personal_data
from app.services import error_log
from app.services.ai_service import ai_service

logger = logging.getLogger(__name__)

CONTENT_BLOCKED = "content_blocked"
PERSONAL_DATA = "personal_data"
MODERATION_UNAVAILABLE = "moderation_unavailable"
MODERATION_UNAVAILABLE_MESSAGE = "Não conseguimos verificar o conteúdo agora. Tente de novo em instantes."
IMAGE_UNREADABLE_MESSAGE = (
    "Não conseguimos verificar uma das imagens. Envie fotos em JPG, PNG, WEBP ou GIF sem animação."
)

UNREADABLE_IMAGE = "unreadable_image"

# What the creator reads, by the SDK's category name.
_CATEGORY_LABELS: dict[str, str] = {
    "sexual": "conteúdo sexual",
    "sexual_minors": "conteúdo sexual envolvendo menores",
    "harassment": "assédio",
    "harassment_threatening": "ameaça",
    "hate": "discurso de ódio",
    "hate_threatening": "ameaça contra um grupo",
    "illicit": "atividade ilegal",
    "illicit_violent": "atividade ilegal violenta",
    "self_harm": "automutilação",
    "self_harm_intent": "automutilação",
    "self_harm_instructions": "automutilação",
    "violence": "violência",
    "violence_graphic": "violência explícita",
    # Not the moderation's: an app event's photo it could not read (moderation pass).
    UNREADABLE_IMAGE: "imagem que não conseguimos verificar",
}
_TEXTS_PER_REQUEST = 32
# A signed URL handed to the moderation only has to live for the call.
_IMAGE_URL_TTL_SECONDS = 120


class ModerationUnavailable(Exception):
    """The moderation could not answer (outage, time limit, key)."""


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    categories: tuple[str, ...] = ()
    # The image could not be read (format, size, a host that refuses the fetch).
    unreadable: bool = False


def reasons(categories: Sequence[str]) -> str:
    labels: list[str] = []
    for name in categories:
        label = _CATEGORY_LABELS.get(name.replace("/", "_").replace("-", "_"), "conteúdo impróprio")
        if label not in labels:
            labels.append(label)
    return ", ".join(labels) or "conteúdo impróprio"


def event_moderation_hash(title: str | None, description: str | None, cover_url: str | None) -> str:
    """md5 of what the moderation saw. The same expression as the generated column
    ``events.moderation_stale`` (migration ``event_moderation``): change both together."""
    raw = "\x1f".join((title or "", description or "", cover_url or ""))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


# ------------------------------------------------------------ the two calls --


async def _record(code: str, exc: BaseException, **details: Any) -> None:
    await error_log.record_error(
        source="ai",
        code=code,
        error_type=type(exc).__name__,
        message=None if isinstance(exc, (TimeoutError, APITimeoutError)) else str(exc),
        details={"model": settings.MODERATION_MODEL, **details},
    )


def _text_chunks(texts: Sequence[str]) -> list[list[tuple[int, str]]]:
    """The non-empty texts, by position, in requests of ``_TEXTS_PER_REQUEST``."""
    todo = [(i, t[:8000]) for i, t in enumerate(texts) if t and t.strip()]
    return [todo[start : start + _TEXTS_PER_REQUEST] for start in range(0, len(todo), _TEXTS_PER_REQUEST)]


async def _chunk_verdicts(chunk: list[tuple[int, str]]) -> list[Verdict]:
    results = await ai_service.moderate_inputs([t for _, t in chunk])
    if len(results) != len(chunk):
        raise ValueError(f"expected {len(chunk)} results, got {len(results)}")
    return [Verdict(allowed, tuple(cats)) for allowed, cats in results]


async def text_verdicts(texts: Sequence[str]) -> list[Verdict]:
    """One verdict per text; empty texts pass without a call.

    Raises ``ModerationUnavailable``: a text that could not be checked is never let
    through by this function.
    """
    out: list[Verdict] = [Verdict(True)] * len(texts)
    for chunk in _text_chunks(texts):
        try:
            verdicts = await _chunk_verdicts(chunk)
        except Exception as exc:
            logger.warning("Text moderation failed (%s)", type(exc).__name__)
            await _record("moderation_api_error", exc, stage="text")
            raise ModerationUnavailable from exc
        for (i, _), verdict in zip(chunk, verdicts, strict=True):
            out[i] = verdict
    return out


async def image_verdict(url: str) -> Verdict:
    """One image, by URL (the moderation fetches it).

    An image the moderation refuses to read (4xx) is ``unreadable``; anything else
    that fails raises ``ModerationUnavailable``.
    """
    try:
        [(allowed, cats)] = await ai_service.moderate_inputs([{"type": "image_url", "image_url": {"url": url}}])
        return Verdict(allowed, tuple(cats))
    except APIStatusError as exc:
        if 400 <= exc.status_code < 500 and exc.status_code not in (401, 403, 429):
            logger.warning("Image moderation could not read the image (%s)", exc.status_code)
            return Verdict(False, unreadable=True)
        logger.warning("Image moderation failed (%s)", exc.status_code)
        await _record("moderation_api_error", exc, stage="image")
        raise ModerationUnavailable from exc
    except Exception as exc:
        logger.warning("Image moderation failed (%s)", type(exc).__name__)
        await _record("moderation_api_error", exc, stage="image")
        raise ModerationUnavailable from exc


async def image_verdicts(urls: Sequence[str], *, concurrency: int = 8) -> list[Verdict]:
    gate = asyncio.Semaphore(concurrency)

    async def one(url: str) -> Verdict:
        async with gate:
            return await image_verdict(url)

    return list(await asyncio.gather(*(one(u) for u in urls)))


async def path_verdicts(db: Any, paths: Sequence[str], *, missing_unreadable: bool = False) -> list[Verdict]:
    """Images uploaded to the private bucket: signed for two minutes, just for the call.

    Storage leaves out what it cannot sign (an object that no longer exists). That
    raises ``ModerationUnavailable``, or, with ``missing_unreadable`` (the moderation
    pass over stored events, where retrying never helps), is an unreadable image.
    A failed signing request always raises.
    """
    try:
        signed = await db.sign_media_paths(list(paths), ttl=_IMAGE_URL_TTL_SECONDS)
    except Exception as exc:
        raise ModerationUnavailable("the images could not be signed") from exc
    missing = [p for p in paths if p not in signed]
    if missing and not missing_unreadable:
        raise ModerationUnavailable(f"{len(missing)} image(s) could not be signed")
    present = [p for p in paths if p in signed]
    checked = dict(zip(present, await image_verdicts([signed[p] for p in present]), strict=True)) if present else {}
    return [checked.get(p, Verdict(False, unreadable=True)) for p in paths]


# ----------------------------------------------------- created in the app --

# Each text the creator writes, with how its refusal reads ("O título foi recusado…").
_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("title", "O título", "recusado"),
    ("description", "A descrição", "recusada"),
    ("location_name", "O local", "recusado"),
    ("trophy_name", "O nome do troféu", "recusado"),
    ("trophy_description", "A descrição do troféu", "recusada"),
)
_KIND_ARTICLES = {"e-mail": "um e-mail", "telefone": "um telefone", "CPF": "um CPF"}


def _join(parts: Sequence[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " e " + parts[-1]


def _event_texts(body: Any) -> dict[str, str]:
    trophy = getattr(body, "trophy", None)
    return {
        "title": body.title or "",
        "description": body.description or "",
        "location_name": body.location_name or "",
        "trophy_name": (trophy.name if trophy else None) or "",
        "trophy_description": (trophy.description if trophy else None) or "",
    }


async def check_event_content(db: Any, body: Any) -> None:
    """Refuse, before anything is written, an event whose texts or photos the
    moderation refuses or whose texts carry someone's e-mail, phone or CPF.

    ``body`` is an ``EventCreate``/``EventUpdate`` whose media and icon paths were
    already checked as the creator's own.
    """
    texts = _event_texts(body)
    for key, label, _ in _FIELDS:
        kinds = find_personal_data(texts[key])
        if kinds:
            found = _join([_KIND_ARTICLES[k] for k in kinds])
            raise ApiError(
                422,
                PERSONAL_DATA,
                f"{label} tem {found}. Remova antes de salvar: o evento fica visível para todos.",
            )

    paths = [m.media_url for m in body.media if getattr(m.type, "value", m.type) == "image"]
    icon = body.trophy.icon_url if getattr(body, "trophy", None) else None
    if icon:
        paths.append(icon)
    try:
        verdicts = await text_verdicts([texts[key] for key, _, _ in _FIELDS])
        for (_, label, refused), verdict in zip(_FIELDS, verdicts, strict=True):
            if not verdict.allowed:
                raise ApiError(
                    422,
                    CONTENT_BLOCKED,
                    f"{label} foi {refused} pela moderação ({reasons(verdict.categories)}). Revise e tente de novo.",
                )
        if not paths:
            return
        image_results = await path_verdicts(db, paths)
    except ModerationUnavailable:
        raise ApiError(503, MODERATION_UNAVAILABLE, MODERATION_UNAVAILABLE_MESSAGE) from None
    for path, verdict in zip(paths, image_results, strict=True):
        if verdict.unreadable:
            raise ApiError(422, CONTENT_BLOCKED, IMAGE_UNREADABLE_MESSAGE)
        if not verdict.allowed:
            what = "O ícone do troféu foi recusado" if path == icon else "Uma das fotos foi recusada"
            raise ApiError(
                422,
                CONTENT_BLOCKED,
                f"{what} pela moderação ({reasons(verdict.categories)}). Troque a imagem e tente de novo.",
            )


# ---------------------------------------------------------------- imported --

# Below this there is no point starting an image check: it would only time out.
_MIN_CALL_SECONDS = 1.0
# Text requests of one import at the same time (32 texts each).
_TEXT_CONCURRENCY = 4


@dataclass(frozen=True, slots=True)
class ImportCheck:
    """What the moderation said about one imported item."""

    # False: the text was refused; the item never enters (an existing row is hidden).
    allowed: bool
    categories: tuple[str, ...] = ()
    # The cover was refused or could not be read: the item goes in without it.
    drop_cover: bool = False
    # Could not be read (rather than refused): not remembered, so the next import
    # tries it again (the host may only have been slow for the moderation's fetch).
    cover_unreadable: bool = False


def import_text(title: str | None, description: str | None) -> str:
    return "\n".join(t for t in (title, description) if t)


async def check_imports(
    items: Sequence[tuple[str, str | None]],
    *,
    budget_seconds: float,
) -> list[ImportCheck | None]:
    """``(text, cover_url)`` per item → its check, or None when it could not be
    checked within ``budget_seconds`` (the moderation was down or time ran out):
    such an item is left for the next import or the moderation pass."""
    if not items:
        return []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    texts = await _import_text_verdicts([t for t, _ in items], deadline)

    out: list[ImportCheck | None] = [None] * len(items)
    pending: list[tuple[int, str]] = []
    for i, ((_, cover), verdict) in enumerate(zip(items, texts, strict=True)):
        if verdict is None:
            continue
        if not verdict.allowed:
            out[i] = ImportCheck(False, verdict.categories)
        elif cover:
            pending.append((i, cover))
        else:
            out[i] = ImportCheck(True)

    gate = asyncio.Semaphore(8)

    async def one(i: int, url: str) -> None:
        async with gate:
            left = deadline - loop.time()
            if left < _MIN_CALL_SECONDS:
                return
            try:
                verdict = await asyncio.wait_for(image_verdict(url), timeout=left)
            except (ModerationUnavailable, TimeoutError):
                return
            out[i] = ImportCheck(
                True, verdict.categories, drop_cover=not verdict.allowed, cover_unreadable=verdict.unreadable
            )

    await asyncio.gather(*(one(i, url) for i, url in pending))
    return out


async def _import_text_verdicts(texts: Sequence[str], deadline: float) -> list[Verdict | None]:
    """Like ``text_verdicts``, but a few requests at a time and each one on its own:
    a request that fails or does not finish by ``deadline`` leaves only its own texts
    unchecked (None); the ones that answered keep their verdicts."""
    loop = asyncio.get_running_loop()
    out: list[Verdict | None] = [None if t and t.strip() else Verdict(True) for t in texts]
    gate = asyncio.Semaphore(_TEXT_CONCURRENCY)
    failures: list[BaseException] = []

    async def one(chunk: list[tuple[int, str]]) -> None:
        async with gate:
            left = deadline - loop.time()
            if left <= 0:
                return
            try:
                verdicts = await asyncio.wait_for(_chunk_verdicts(chunk), timeout=left)
            except Exception as exc:
                # Our own deadline is time running out; anything else, the moderation's
                # own time limit included (MODERATION_TIMEOUT_SECONDS), is it failing.
                if not (isinstance(exc, TimeoutError) and loop.time() >= deadline - 0.05):
                    failures.append(exc)
                return
        for (i, _), verdict in zip(chunk, verdicts, strict=True):
            out[i] = verdict

    await asyncio.gather(*(one(chunk) for chunk in _text_chunks(texts)))
    unchecked = sum(v is None for v in out)
    if unchecked:
        logger.warning("Import moderation left %d texts for the next run.", unchecked)
    if failures:
        # Once per import, not once per request.
        await _record("moderation_api_error", failures[0], stage="import_text", failed_requests=len(failures))
    return out


def redact_import(title: str, description: str | None) -> tuple[str, str | None, bool]:
    """LGPD: contacts in an imported title or description become "[contato removido]"."""
    new_title = redact_personal_data(title) or title
    new_description = redact_personal_data(description)
    return new_title, new_description, (new_title, new_description) != (title, description)


# ----------------------------------------------------------- moderation pass --


@dataclass(slots=True)
class PassResult:
    picked: int = 0
    checked: int = 0
    blocked: int = 0
    covers_removed: int = 0
    contacts_removed: int = 0
    # Could not be checked within the budget: still stale, for the next call.
    not_checked: int = 0


async def _app_row_check(db: Any, row: dict[str, Any]) -> ImportCheck:
    """An event created in the app before #22 A, checked as saving it today would:
    title, description, place, trophy, photos and trophy icon (videos cannot be).

    A photo the moderation cannot read hides the event too: it could not be saved
    today, and its creator can fix it by editing. Raises when something could not be
    checked (the row then stays stale).
    """
    event_id = str(row["id"])
    media = await db.get_event_media_rows(event_id)
    trophy = await db.get_trophy_template_by_event(event_id) or {}
    texts = [
        import_text(str(row.get("title") or ""), row.get("description")),
        row.get("location_name") or "",
        trophy.get("name") or "",
        trophy.get("description") or "",
    ]
    refused = [v for v in await text_verdicts(texts) if not v.allowed]
    if refused:
        return ImportCheck(False, tuple(dict.fromkeys(c for v in refused for c in v.categories)))
    paths = [str(m["media_url"]) for m in media if m.get("type") == "image" and m.get("media_url")]
    paths += [p for p in (trophy.get("icon_url"), row.get("cover_url")) if p]
    if paths:
        images = await path_verdicts(db, paths, missing_unreadable=True)
        bad = [c for v in images if not v.allowed for c in (v.categories or (UNREADABLE_IMAGE,))]
        if bad:
            return ImportCheck(False, tuple(dict.fromkeys(bad)))
    return ImportCheck(True)


async def _app_row_checks(db: Any, rows: list[dict[str, Any]], *, budget_seconds: float) -> list[ImportCheck | None]:
    """A few rows at a time, each bounded by the same deadline: a row that does not
    finish in time is left stale (None), the ones that did keep their checks."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    gate = asyncio.Semaphore(4)

    async def one(row: dict[str, Any]) -> ImportCheck | None:
        async with gate:
            left = deadline - loop.time()
            if left < _MIN_CALL_SECONDS:
                return None
            try:
                return await asyncio.wait_for(_app_row_check(db, row), timeout=left)
            except Exception as exc:  # noqa: BLE001 — anything unchecked stays stale
                logger.warning("Moderation pass could not check an app event (%s)", type(exc).__name__)
                return None

    return list(await asyncio.gather(*(one(r) for r in rows)))


async def moderate_stale(db: Any, *, limit: int, budget_seconds: float) -> PassResult:
    """Check up to ``limit`` rows the moderation has not seen (``moderation_stale``):
    the catalogue that was here before #22 A, and whatever an import left unchecked.

    Imported rows also lose their contacts (LGPD) and, when refused or unreadable,
    their cover; a refused row is hidden. Rows created in the app are checked as
    saving them would (place, trophy and photos too) and hidden when refused, but
    their text is the creator's and is never rewritten.
    """
    rows = await db.list_stale_moderation_events(limit=limit)
    result = PassResult(picked=len(rows))
    if not rows:
        return result
    prepared: list[tuple[dict[str, Any], str, str | None, bool]] = []
    for row in rows:
        title, description = str(row.get("title") or ""), row.get("description")
        redacted = False
        if row.get("source"):
            title, description, redacted = redact_import(title, description)
        prepared.append((row, title, description, redacted))
    imported = [i for i, (r, _, _, _) in enumerate(prepared) if r.get("source")]
    from_app = [i for i, (r, _, _, _) in enumerate(prepared) if not r.get("source")]
    import_checks, app_checks = await asyncio.gather(
        check_imports(
            [(import_text(prepared[i][1], prepared[i][2]), prepared[i][0].get("cover_url")) for i in imported],
            budget_seconds=budget_seconds,
        ),
        _app_row_checks(db, [prepared[i][0] for i in from_app], budget_seconds=budget_seconds),
    )
    checks: list[ImportCheck | None] = [None] * len(prepared)
    for i, check in [*zip(imported, import_checks, strict=True), *zip(from_app, app_checks, strict=True)]:
        checks[i] = check
    now = datetime.now(timezone.utc).isoformat()
    gate = asyncio.Semaphore(8)

    async def write(row: dict[str, Any], title: str, description: str | None, redacted: bool, check: ImportCheck) -> None:
        cover = row.get("cover_url")
        fields: dict[str, Any] = {"moderated_at": now}
        if redacted:
            fields.update(title=title, description=description)
            result.contacts_removed += 1
        dropped = None
        if check.allowed and check.drop_cover and cover and row.get("source"):
            dropped = None if check.cover_unreadable else cover
            cover = None
            fields["cover_url"] = None
            result.covers_removed += 1
        fields.update(
            moderation_hash=event_moderation_hash(title, description, cover),
            # So the next import, which still brings that cover, knows it was refused.
            moderation_dropped_cover=dropped,
            moderation_blocked=not check.allowed,
            moderation_reasons=None if check.allowed else list(check.categories),
        )
        async with gate:
            try:
                await db.set_event_moderation(str(row["id"]), fields)
            except Exception:
                logger.exception("Moderation write failed for one event; it stays stale.")
                result.not_checked += 1
                return
        result.checked += 1
        if not check.allowed:
            result.blocked += 1

    jobs = []
    for (row, title, description, redacted), check in zip(prepared, checks, strict=True):
        if check is None:
            result.not_checked += 1
        else:
            jobs.append(write(row, title, description, redacted, check))
    await asyncio.gather(*jobs)
    return result
