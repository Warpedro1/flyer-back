"""`get_or_create_chat` survives a concurrent insert (#30).

Seen in production: ``GET /chat/history`` answered 500 because the ``INSERT`` into
``ai_chats`` hit the ``UNIQUE(user_id)`` (409) after the ``SELECT`` found nothing:
two requests at once. The second one now reads the row the first created.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.core.errors import ApiError
from app.services.db_service import DBService


def _response(status: int, body: Any = None) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("GET", "http://x"))


def _client(gets: list[httpx.Response], post: httpx.Response) -> MagicMock:
    client = MagicMock()
    client.get = AsyncMock(side_effect=gets)
    client.post = AsyncMock(return_value=post)
    return client


async def test_an_existing_chat_is_returned() -> None:
    client = _client([_response(200, [{"id": "chat-1"}])], _response(201, []))

    assert await DBService(client).get_or_create_chat("user-1", "jwt") == "chat-1"
    client.post.assert_not_called()


async def test_a_new_chat_is_created() -> None:
    client = _client([_response(200, [])], _response(201, [{"id": "chat-2"}]))

    assert await DBService(client).get_or_create_chat("user-1", "jwt") == "chat-2"


@pytest.mark.parametrize(
    "conflict",
    [
        _response(409, {"code": "23505", "message": "duplicate key"}),
        _response(409, {"code": "PGRST", "message": "conflict"}),
    ],
)
async def test_a_concurrent_insert_reads_the_chat_again(conflict: httpx.Response) -> None:
    client = _client([_response(200, []), _response(200, [{"id": "chat-3"}])], conflict)

    assert await DBService(client).get_or_create_chat("user-1", "jwt") == "chat-3"
    assert client.get.await_count == 2


async def test_a_conflict_without_a_chat_means_the_account_is_gone() -> None:
    # 409 from the foreign key: the user was deleted while the session lived.
    client = _client(
        [_response(200, []), _response(200, [])],
        _response(409, {"code": "23503", "message": "violates foreign key"}),
    )

    with pytest.raises(ApiError) as caught:
        await DBService(client).get_or_create_chat("user-1", "jwt")

    assert caught.value.status_code == 401
    assert caught.value.code == "session_invalid"
