"""Structured logs: one JSON object per line (#25 phase 1).

Vercel keeps runtime logs for 1 hour on Hobby; while they last, each line can be
filtered by ``request_id``. What has to last goes to ``app_errors`` (30 days).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from app.core.log_scrub import scrub
from app.core.request_context import current_request_id, current_user_hash

# Fields a call can pass in `extra=` that belong in the JSON line.
_EXTRA_FIELDS = ("method", "route", "status", "duration_ms")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": scrub(record.getMessage()),
        }
        request_id = current_request_id()
        if request_id:
            line["request_id"] = request_id
        user_hash = current_user_hash()
        if user_hash:
            line["user_hash"] = user_hash
        for field in _EXTRA_FIELDS:
            if hasattr(record, field):
                line[field] = getattr(record, field)
        if record.exc_info:
            line["exc"] = scrub(self.formatException(record.exc_info))
        return json.dumps(line, ensure_ascii=False, default=str)


def configure_logging() -> None:
    """JSON lines on stdout for the app's loggers. Safe to call more than once."""
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # httpx logs every request URL at INFO, PostgREST filters included (e-mails, CPFs).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
