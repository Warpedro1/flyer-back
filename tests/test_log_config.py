"""JSON log lines, once each (#25 phase 1).

The Vercel Python runtime puts its own plain-text handler on the root logger, so
every line came out twice: once as text and once as JSON.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest

from app.core import log_config


@pytest.fixture
def root() -> Iterator[logging.Logger]:
    logger = logging.getLogger()
    saved = logger.handlers[:], logger.level
    yield logger
    logger.handlers[:] = saved[0]
    logger.setLevel(saved[1])


def test_the_platform_handler_is_replaced(root: logging.Logger, monkeypatch: pytest.MonkeyPatch) -> None:
    platform = logging.StreamHandler(io.StringIO())
    root.handlers[:] = [platform]
    out = io.StringIO()
    monkeypatch.setattr(log_config.sys, "stdout", out)

    log_config.configure_logging()
    logging.getLogger("app.test").info("olá")

    assert platform.stream.getvalue() == ""
    assert json.loads(out.getvalue().strip())["msg"] == "olá"


def test_configuring_twice_keeps_one_handler(root: logging.Logger) -> None:
    root.handlers[:] = []

    log_config.configure_logging()
    log_config.configure_logging()

    assert len(root.handlers) == 1
