"""Shared SlowAPI limiter instance (avoids circular imports with routers)."""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
