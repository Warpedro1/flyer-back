"""Reusable API dependencies."""

from typing import Annotated

import httpx
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.security import decode_access_token
from app.services.db_service import DBService

bearer_scheme = HTTPBearer(auto_error=True)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> str:
    """Return authenticated user identifier extracted from JWT `sub`."""

    payload = decode_access_token(credentials.credentials, settings.SUPABASE_JWT_SECRET)
    return payload["sub"]


async def get_current_user_with_token(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> tuple[str, str]:
    """Return (user_id, raw_jwt) for routes that forward the JWT to Supabase (RLS)."""

    payload = decode_access_token(credentials.credentials, settings.SUPABASE_JWT_SECRET)
    return payload["sub"], credentials.credentials


async def get_http_client(request: Request) -> httpx.AsyncClient:
    """Shared AsyncClient from app lifespan (singleton)."""

    return request.app.state.http_client


HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]


def get_db_service(client: HttpClientDep) -> DBService:
    return DBService(client)


DbServiceDep = Annotated[DBService, Depends(get_db_service)]
