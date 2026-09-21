"""FastAPI entrypoint for Flyer backend."""

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api import admin, chat, events, plans, social, trophies
from app.core.config import settings
from app.core.limiter import limiter


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Single shared httpx client for Supabase REST (no per-request client creation)."""
    app.state.http_client = httpx.AsyncClient(
        base_url=settings.SUPABASE_URL.rstrip("/"),
        timeout=httpx.Timeout(30.0),
    )
    yield
    await app.state.http_client.aclose()


app = FastAPI(
    title="Flyer Backend",
    description="Secure-by-design backend for event recommendations and social gamification.",
    version="0.1.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(events.router)
app.include_router(plans.router)
app.include_router(social.router)
app.include_router(trophies.router)
app.include_router(admin.router)


@app.get("/", tags=["health"])
@limiter.limit("30/minute")
async def root(request: Request) -> dict[str, str]:
    """Simple health endpoint for deployment checks."""

    return {"message": "Flyer backend is running."}


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Health probe with minimal runtime metadata."""

    return {"status": "ok", "chat_model": settings.CHAT_MODEL}
