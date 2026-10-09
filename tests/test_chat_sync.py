"""POST /chat/sync: what goes into the interest vector, and how it is stored (#4)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.api.chat import _build_embedding_corpus
from app.core.config import settings
from app.models.schemas import EMBEDDING_DIMENSIONS
from app.services.ai_service import ai_service
from app.services.db_service import DBService
from app.services.interest_sync import SyncOutcome, apply_chat_sync

# Full-length vectors: a vector of any other size is treated as corrupt and replaced.
CURRENT = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
NEW = [0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 2)


# ---------------------------------------------------------------- corpus --


def test_corpus_has_only_what_the_user_wrote() -> None:
    # The assistant's replies used to be in the corpus, pulling the user's taste
    # vector towards the model's own vocabulary.
    messages = [
        {"sender": "user", "content": "Gosto de jazz ao vivo"},
        {"sender": "assistant", "content": "Recomendo o festival de música eletrónica"},
        {"sender": "user", "content": "e de teatro"},
    ]

    corpus = _build_embedding_corpus(messages)

    assert "jazz ao vivo" in corpus
    assert "teatro" in corpus
    assert "eletrónica" not in corpus


def test_corpus_is_empty_without_user_messages() -> None:
    assert _build_embedding_corpus([{"sender": "assistant", "content": "Olá!"}]) == ""


# ----------------------------------------------------------------- apply --


def _db(*profiles: dict | None, cas: list[bool] | None = None) -> MagicMock:
    db = MagicMock()
    db.get_profile = AsyncMock(side_effect=list(profiles))
    db.update_interest_vector_if_count = AsyncMock(side_effect=cas or [True])
    return db


async def test_first_sync_stores_the_new_vector_and_counts_from_null() -> None:
    db = _db({"id": "u1", "interest_embedding": None, "interest_sync_count_after_onboarding": None})

    outcome = await apply_chat_sync(db, "u1", "jwt", NEW)

    assert outcome is SyncOutcome.updated
    db.update_interest_vector_if_count.assert_awaited_once_with("u1", NEW, None)


async def test_early_syncs_use_the_anchor_weights() -> None:
    count = settings.SYNC_ANCHOR_COUNT - 1
    db = _db({"interest_embedding": CURRENT, "interest_sync_count_after_onboarding": count})

    await apply_chat_sync(db, "u1", "jwt", NEW)

    expected = ai_service.blend_interest_weighted(
        CURRENT, NEW, settings.INTEREST_ANCHOR_WEIGHT_CURRENT, settings.INTEREST_ANCHOR_WEIGHT_NEW
    )
    db.update_interest_vector_if_count.assert_awaited_once_with("u1", expected, count)


async def test_later_syncs_blend_organically() -> None:
    count = settings.SYNC_ANCHOR_COUNT
    db = _db({"interest_embedding": CURRENT, "interest_sync_count_after_onboarding": count})

    await apply_chat_sync(db, "u1", "jwt", NEW)

    expected = ai_service.update_interest_organically(CURRENT, NEW)
    db.update_interest_vector_if_count.assert_awaited_once_with("u1", expected, count)


async def test_a_concurrent_sync_makes_it_reread_and_retry() -> None:
    # Vector and counter are written together, guarded by the counter it read:
    # if another sync got in first, blend again on top of that one.
    first = {"interest_embedding": CURRENT, "interest_sync_count_after_onboarding": 7}
    second = {"interest_embedding": NEW, "interest_sync_count_after_onboarding": 8}
    db = _db(first, second, cas=[False, True])

    outcome = await apply_chat_sync(db, "u1", "jwt", NEW)

    assert outcome is SyncOutcome.updated
    assert db.get_profile.await_count == 2
    assert db.update_interest_vector_if_count.await_args.args[2] == 8


async def test_gives_up_after_repeated_conflicts() -> None:
    row = {"interest_embedding": CURRENT, "interest_sync_count_after_onboarding": 1}
    db = _db(row, row, row, cas=[False, False, False])

    assert await apply_chat_sync(db, "u1", "jwt", NEW) is SyncOutcome.conflict


async def test_missing_profile() -> None:
    db = _db(None)

    assert await apply_chat_sync(db, "u1", "jwt", NEW) is SyncOutcome.no_profile
    db.update_interest_vector_if_count.assert_not_awaited()


# ------------------------------------------------------------ db request --


def _patch_client(rows: list[dict]) -> MagicMock:
    client = MagicMock()
    client.patch = AsyncMock(
        return_value=httpx.Response(200, json=rows, request=httpx.Request("PATCH", "http://x"))
    )
    return client


@pytest.mark.parametrize(
    ("expected", "count_filter", "next_count"),
    [(None, "is.null", 1), (4, "eq.4", 5)],
)
async def test_vector_and_counter_go_in_one_guarded_patch(
    expected: int | None, count_filter: str, next_count: int
) -> None:
    client = _patch_client([{"id": "u1"}])

    ok = await DBService(client).update_interest_vector_if_count("u1", NEW, expected)

    assert ok is True
    kwargs = client.patch.await_args.kwargs
    assert kwargs["params"] == {
        "id": "eq.u1",
        "interest_sync_count_after_onboarding": count_filter,
    }
    assert kwargs["json"] == {
        "interest_embedding": NEW,
        "interest_sync_count_after_onboarding": next_count,
    }


async def test_no_row_updated_means_someone_else_won() -> None:
    client = _patch_client([])

    assert await DBService(client).update_interest_vector_if_count("u1", NEW, 3) is False
