"""Signed-URL minting for private event media (mocked httpx client)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.db_service import DBService


def _ok_response(payload: object) -> MagicMock:
    resp = MagicMock()
    resp.is_success = True
    resp.json.return_value = payload
    return resp


@pytest.mark.asyncio
async def test_sign_media_paths_signs_and_passes_through_http() -> None:
    client = MagicMock()
    client.post = AsyncMock(
        return_value=_ok_response(
            [{"path": "u/a.png", "signedURL": "/object/sign/event-media/u/a.png?token=tok"}]
        )
    )
    db = DBService(client)

    out = await db.sign_media_paths(["u/a.png", "http://x/y.jpg"])

    # Path is signed into an absolute Storage URL...
    assert out["u/a.png"] == "http://localhost/storage/v1/object/sign/event-media/u/a.png?token=tok"
    # ...and an already-absolute (legacy) URL passes through untouched.
    assert out["http://x/y.jpg"] == "http://x/y.jpg"
    client.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_sign_media_paths_empty_makes_no_request() -> None:
    client = MagicMock()
    client.post = AsyncMock()
    db = DBService(client)

    assert await db.sign_media_paths([]) == {}
    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_sign_media_paths_only_absolute_urls_makes_no_request() -> None:
    client = MagicMock()
    client.post = AsyncMock()
    db = DBService(client)

    out = await db.sign_media_paths(["https://cdn/x.jpg"])

    assert out == {"https://cdn/x.jpg": "https://cdn/x.jpg"}
    client.post.assert_not_awaited()
