"""FastAPI entrypoint for Flyer backend."""

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi.middleware import SlowAPIMiddleware

from app.api import admin, auth, chat, events, plans, rsvp, social, telemetry, trophies
from app.core.config import settings
from app.core.errors import install_error_handlers
from app.core.limiter import limiter
from app.core.log_config import configure_logging
from app.core.request_context import REQUEST_ID_HEADER, RequestContextMiddleware
from app.services import error_log
from app.services.tracing import log_tracing_status

configure_logging()
log_tracing_status()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Single shared httpx client for Supabase REST (no per-request client creation)."""
    app.state.http_client = httpx.AsyncClient(
        base_url=settings.SUPABASE_URL.rstrip("/"),
        timeout=httpx.Timeout(30.0),
    )
    error_log.set_client(app.state.http_client)
    yield
    error_log.set_client(None)
    await app.state.http_client.aclose()


app = FastAPI(
    title="Flyer Backend",
    description="Secure-by-design backend for event recommendations and social gamification.",
    version="0.1.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
install_error_handlers(app)
# Order matters: the last one added is the outermost. RequestContextMiddleware sits
# inside CORS so its last-resort 500 still gets the CORS headers (#30).
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(RequestContextMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    # Custom response headers are invisible to a cross-origin fetch unless listed.
    expose_headers=[events.DISCOVERY_MODE_HEADER, REQUEST_ID_HEADER],
)

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(events.router)
app.include_router(rsvp.router)
app.include_router(plans.router)
app.include_router(social.router)
app.include_router(trophies.router)
app.include_router(telemetry.router)
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
