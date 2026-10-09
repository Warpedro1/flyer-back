"""Fold a chat-derived embedding into the user's stored interest vector."""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Any

from app.core.config import settings
from app.models.schemas import EMBEDDING_DIMENSIONS
from app.services.ai_service import ai_service
from app.services.db_service import DBService

logger = logging.getLogger(__name__)

# Concurrent syncs of the same user are rare (one chat, rate-limited route);
# a few re-reads are plenty before reporting the conflict.
_MAX_ATTEMPTS = 3


class SyncOutcome(str, Enum):
    updated = "updated"
    no_profile = "no_profile"
    conflict = "conflict"


def parse_interest_embedding(raw: Any) -> list[float] | None:
    """PostgREST returns vectors as JSON arrays or as their text form."""
    if raw is None:
        return None
    if isinstance(raw, list):
        return [float(x) for x in raw]
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            return [float(x) for x in json.loads(text)]
    return None


def _blend(current: list[float] | None, new: list[float], sync_count: int) -> list[float]:
    """Anchor to the onboarding vector for the first syncs, then drift organically."""
    if (
        current is None
        or len(current) != len(new)
        or len(new) != EMBEDDING_DIMENSIONS
    ):
        return new
    if sync_count < settings.SYNC_ANCHOR_COUNT:
        return ai_service.blend_interest_weighted(
            current,
            new,
            settings.INTEREST_ANCHOR_WEIGHT_CURRENT,
            settings.INTEREST_ANCHOR_WEIGHT_NEW,
        )
    return ai_service.update_interest_organically(current, new)


async def apply_chat_sync(
    db: DBService,
    user_id: str,
    jwt: str,
    new_vec: list[float],
) -> SyncOutcome:
    """Blend ``new_vec`` into the profile and bump the sync counter atomically.

    The write is guarded by the counter that was read, so a concurrent sync makes
    this one re-read and blend on top of it instead of overwriting it.
    """
    for _ in range(_MAX_ATTEMPTS):
        profile = await db.get_profile(user_id, jwt)
        if not profile:
            return SyncOutcome.no_profile
        raw_count = profile.get("interest_sync_count_after_onboarding")
        expected = int(raw_count) if raw_count is not None else None
        current = parse_interest_embedding(profile.get("interest_embedding"))
        final = _blend(current, new_vec, expected or 0)
        if await db.update_interest_vector_if_count(user_id, final, expected):
            return SyncOutcome.updated
    logger.warning("Interest sync gave up after %d conflicts (user_id=%s)", _MAX_ATTEMPTS, user_id)
    return SyncOutcome.conflict
