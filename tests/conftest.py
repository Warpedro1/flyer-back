"""Pytest bootstrap: minimal env so `app.core.config` can load in CI without a real `.env`."""

from __future__ import annotations

import os

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_KEY", "test-service-role-key")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("PUSHER_APP_ID", "1")
os.environ.setdefault("PUSHER_KEY", "test-pusher-key")
os.environ.setdefault("PUSHER_SECRET", "test-pusher-secret")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-key-for-ci")
