"""Realtime channel for the app: Pusher config and private-channel auth (2026-10-17)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.errors import ApiError
from app.core.limiter import limiter
from app.models.schemas import RealtimeAuthRequest, RealtimeAuthResponse, RealtimeConfig
from app.services import realtime

router = APIRouter(prefix="/realtime", tags=["realtime"])


@router.get("/config", response_model=RealtimeConfig)
@limiter.limit("30/minute")
async def realtime_config(
    request: Request,
    user_id: Annotated[str, Depends(get_current_user)],
) -> RealtimeConfig:
    """Pusher's public key and cluster; ``enabled=false`` means poll instead."""
    if not realtime.enabled():
        return RealtimeConfig(enabled=False)
    return RealtimeConfig(enabled=True, key=settings.PUSHER_KEY, cluster=settings.PUSHER_CLUSTER)


@router.post("/auth", response_model=RealtimeAuthResponse)
@limiter.limit("30/minute")
async def realtime_auth(
    request: Request,
    body: RealtimeAuthRequest,
    user_id: Annotated[str, Depends(get_current_user)],
) -> RealtimeAuthResponse:
    """Sign a subscription to your own channel, and only to it."""
    if not realtime.enabled():
        raise ApiError(503, "realtime_unavailable", "Avisos em tempo real indisponíveis.")
    if body.channel_name != realtime.user_channel(user_id):
        raise ApiError(403, "channel_forbidden", "Este canal não é seu.")
    return RealtimeAuthResponse(**realtime.authorize_channel(body.socket_id, body.channel_name))
