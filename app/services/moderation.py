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


async def text_verdicts(texts: Sequence[str]) -> list[Verdict]:
    """One verdict per text; empty texts pass without a call.

    Raises ``ModerationUnavailable``: a text that could not be checked is never let
    through by this function.
    """
    out: list[Verdict] = [Verdict(True)] * len(texts)
    todo = [(i, t[:8000]) for i, t in enumerate(texts) if t and t.strip()]
    for start in range(0, len(todo), _TEXTS_PER_REQUEST):
        chunk = todo[start : start + _TEXTS_PER_REQUEST]
        try:
            results = await ai_service.moderate_inputs([t for _, t in chunk])
            if len(results) != len(chunk):
                raise ValueError(f"expected {len(chunk)} results, got {len(results)}")
        except Exception as exc:
            logger.warning("Text moderation failed (%s)", type(exc).__name__)
            await _record("moderation_api_error", exc, stage="text")
            raise ModerationUnavailable from exc
        for (i, _), (allowed, cats) in zip(chunk, results, strict=True):
            out[i] = Verdict(allowed, tuple(cats))
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


async def path_verdicts(db: Any, paths: Sequence[str]) -> list[Verdict]:
    """Images uploaded to the private bucket: signed for two minutes, just for the call."""
    signed = await db.sign_media_paths(list(paths), ttl=_IMAGE_URL_TTL_SECONDS)
    missing = [p for p in paths if p not in signed]
    if missing:
        raise ModerationUnavailable(f"{len(missing)} image(s) could not be signed")
    return await image_verdicts([signed[p] for p in paths])


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


@dataclass(frozen=True, slots=True)
class ImportCheck:
    """What the moderation said about one imported item."""

    # False: the text was refused; the item never enters (an existing row is hidden).
    allowed: bool
    categories: tuple[str, ...] = ()
    # The cover was refused or could not be read: the item goes in without it.
    drop_cover: bool = False


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
    try:
        texts = await asyncio.wait_for(text_verdicts([t for t, _ in items]), timeout=budget_seconds)
    except (ModerationUnavailable, TimeoutError):
        logger.warning("Import moderation unavailable for %d items; they wait for the next run.", len(items))
        return [None] * len(items)

    out: list[ImportCheck | None] = [None] * len(items)
    pending: list[tuple[int, str]] = []
    for i, ((_, cover), verdict) in enumerate(zip(items, texts, strict=True)):
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
            out[i] = ImportCheck(True, verdict.categories, drop_cover=not verdict.allowed)

    await asyncio.gather(*(one(i, url) for i, url in pending))
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


async def moderate_stale(db: Any, *, limit: int, budget_seconds: float) -> PassResult:
    """Check up to ``limit`` rows the moderation has not seen (``moderation_stale``):
    the catalogue that was here before #22 A, and whatever an import left unchecked.

    Imported rows also lose their contacts (LGPD) and, when refused or unreadable,
    their cover; a refused row is hidden. Rows created in the app are only checked:
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
    checks = await check_imports(
        [(import_text(t, d), r.get("cover_url") if r.get("source") else None) for r, t, d, _ in prepared],
        budget_seconds=budget_seconds,
    )
    now = datetime.now(timezone.utc).isoformat()
    gate = asyncio.Semaphore(8)

    async def write(row: dict[str, Any], title: str, description: str | None, redacted: bool, check: ImportCheck) -> None:
        cover = row.get("cover_url")
        fields: dict[str, Any] = {"moderated_at": now}
        if redacted:
            fields.update(title=title, description=description)
            result.contacts_removed += 1
        if check.allowed and check.drop_cover and cover:
            cover = None
            fields["cover_url"] = None
            result.covers_removed += 1
        fields.update(
            moderation_hash=event_moderation_hash(title, description, cover),
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
