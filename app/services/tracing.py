"""LangSmith traces from a serverless function (#25 phase 1).

The LangSmith SDK and LangChain send runs from a background thread. On Vercel the
function is frozen as soon as the request ends, so those runs never left: nothing
showed up in LangSmith. `flush_traces` sends what is pending before the request
ends, with a time limit; it never raises.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from app.core.config import settings

logger = logging.getLogger(__name__)

_FLUSH_TIMEOUT_SECONDS = 3.0


def tracing_enabled() -> bool:
    return bool((settings.LANGSMITH_TRACING or settings.LANGCHAIN_TRACING_V2) and settings.LANGSMITH_API_KEY)


def _flush_sync() -> None:
    from langchain_core.tracers.langchain import wait_for_all_tracers
    from langsmith.run_trees import get_cached_client

    wait_for_all_tracers()
    get_cached_client().flush()


async def flush_traces() -> None:
    if not tracing_enabled():
        return
    try:
        await asyncio.wait_for(asyncio.to_thread(_flush_sync), timeout=_FLUSH_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — tracing must never break a request
        logger.warning("Could not send LangSmith traces (%s)", type(exc).__name__)


def log_tracing_status() -> None:
    """One line at start-up saying whether traces go out, and where (never the key)."""
    host = urlparse(settings.LANGSMITH_ENDPOINT).netloc or settings.LANGSMITH_ENDPOINT
    logger.info(
        "LangSmith tracing %s (endpoint %s, project %s, key %s)",
        "on" if tracing_enabled() else "off",
        host,
        settings.LANGSMITH_PROJECT or "default",
        "set" if settings.LANGSMITH_API_KEY else "missing",
    )
