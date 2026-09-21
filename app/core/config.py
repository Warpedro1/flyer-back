"""Application settings loaded from environment variables."""

import os
from functools import lru_cache
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized configuration for Flyer backend."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Supabase
    SUPABASE_URL: str
    SUPABASE_KEY: str
    SUPABASE_JWT_SECRET: str

    # OpenAI (use OPENAI_API_KEY or OPENAI_KEY; latter fills API key if former is empty)
    OPENAI_API_KEY: str = ""
    OPENAI_KEY: str = ""

    # Pusher
    PUSHER_APP_ID: str
    PUSHER_KEY: str
    PUSHER_SECRET: str
    PUSHER_CLUSTER: str = "eu"

    # Local data / RAG tooling (optional paths)
    DATA_PATH: str = ""
    CDC_PATH: str = ""
    LGPD_PATH: str = ""

    # Flyer AI defaults (AsyncOpenAI, embeddings)
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSIONS: int = 1536
    CHAT_MODEL: str = "gpt-4o"
    CHAT_MAX_TOKENS: int = 500
    USER_INPUT_MAX_LENGTH: int = 1000

    # Event media storage (private Supabase bucket; backend signs read URLs)
    MEDIA_BUCKET: str = "event-media"
    MEDIA_SIGNED_URL_TTL_SECONDS: int = Field(default=3600, ge=60, le=86400)

    # ETL ingestion (scrape real events → LLM standardize → upsert). Opt-in.
    APIFY_TOKEN: str = ""
    APIFY_EVENTS_ACTOR: str = "johnvc~google-events-api---access-google-events-data"
    # Locale passed to the actor: hl = language (ISO 639-1), gl = country (ISO 3166-1).
    APIFY_EVENTS_LANG: str = "pt"
    APIFY_EVENTS_COUNTRY: str = ""
    # Optional canonical Google location ("Lisbon,Lisbon,Portugal"). Anything else aborts
    # the actor run, so when empty the city is only named in the search query.
    APIFY_EVENTS_LOCATION: str = ""
    ETL_TRANSFORM_MODEL: str = "gpt-4o-mini"
    ETL_INGEST_BATCH_SIZE: int = Field(default=20, ge=1, le=100)
    # Shared secret required in the X-Admin-Token header to trigger POST /admin/ingest.
    # When empty, the ingest endpoint is disabled (503).
    ADMIN_INGEST_TOKEN: str = ""

    # LangChain / LangGraph style aliases (default to Flyer models when unset)
    EMBEDDINGS_MODEL: str = ""
    LLM_MODEL: str = ""

    # LangSmith tracing (optional; pushed to os.environ for LangChain SDKs)
    LANGSMITH_TRACING: bool = False
    LANGCHAIN_TRACING_V2: bool = False
    LANGSMITH_ENDPOINT: str = "https://api.smith.langchain.com"
    LANGSMITH_API_KEY: str = ""
    LANGSMITH_PROJECT: str = ""

    # Geolocation and check-in constants
    CHECKIN_MAX_DISTANCE_METERS: float = 200.0
    LATITUDE_MIN: float = Field(default=-90.0)
    LATITUDE_MAX: float = Field(default=90.0)
    LONGITUDE_MIN: float = Field(default=-180.0)
    LONGITUDE_MAX: float = Field(default=180.0)

    # Interest update weights
    INTEREST_WEIGHT_CURRENT: float = 0.7
    INTEREST_WEIGHT_NEW: float = 0.3
    # Stronger preservation for the first K successful syncs after onboarding (Option 2 anchor)
    INTEREST_ANCHOR_WEIGHT_CURRENT: float = 0.88
    INTEREST_ANCHOR_WEIGHT_NEW: float = 0.12
    SYNC_ANCHOR_COUNT: int = Field(default=5, ge=0, le=100)

    # Onboarding guard / taste pipeline
    # Gates ONLY the optional LLM judge stage; heuristics + moderation always run.
    ENABLE_ONBOARDING_GUARDS: bool = True
    GUARD_JUDGE_MODEL: str = ""
    TASTE_EXTRACTOR_MODEL: str = ""
    GUARD_MAX_INPUT_CHARS: int = Field(default=8000, ge=1000, le=50000)
    ONBOARDING_ANSWER_MAX_CHARS: int = Field(default=400, ge=50, le=2000)
    ONBOARDING_ANSWERS_TOTAL_MAX_CHARS: int = Field(default=2200, ge=200, le=10000)
    ONBOARDING_ANSWER_ELABORATE_MIN_CHARS: int = Field(default=40, ge=10, le=500)
    GUARD_JUDGE_TIMEOUT_SECONDS: float = Field(default=45.0, ge=5.0, le=120.0)
    TASTE_EXTRACT_TIMEOUT_SECONDS: float = Field(default=45.0, ge=5.0, le=120.0)
    MODERATION_TIMEOUT_SECONDS: float = Field(default=15.0, ge=3.0, le=60.0)
    # Number of per-step taste docs retrieved and injected into the chat system prompt.
    CHAT_TASTE_MATCH_COUNT: int = Field(default=4, ge=1, le=20)

    @model_validator(mode="after")
    def resolve_openai_and_model_aliases(self) -> Self:
        """Prefer OPENAI_API_KEY; if empty, use OPENAI_KEY. Fill EMBEDDINGS_MODEL / LLM_MODEL."""
        api_key = (self.OPENAI_API_KEY or "").strip()
        alt_key = (self.OPENAI_KEY or "").strip()
        if not api_key and alt_key:
            object.__setattr__(self, "OPENAI_API_KEY", alt_key)

        embeddings = (self.EMBEDDINGS_MODEL or "").strip()
        if not embeddings:
            object.__setattr__(self, "EMBEDDINGS_MODEL", self.EMBEDDING_MODEL)

        llm = (self.LLM_MODEL or "").strip()
        if not llm:
            object.__setattr__(self, "LLM_MODEL", self.CHAT_MODEL)

        return self


@lru_cache
def get_settings() -> Settings:
    """Cache settings instance to avoid repeated .env parsing."""

    return Settings()


def configure_tooling_env() -> None:
    """
    Mirror LangSmith / LangChain settings into os.environ.

    LangChain and related tools read tracing and API keys from the environment
    at import/runtime; this keeps a single source of truth in Settings.
    """
    s = get_settings()
    os.environ["LANGCHAIN_TRACING_V2"] = "true" if s.LANGCHAIN_TRACING_V2 else "false"
    os.environ["LANGSMITH_TRACING"] = "true" if s.LANGSMITH_TRACING else "false"
    if s.LANGSMITH_ENDPOINT:
        os.environ["LANGSMITH_ENDPOINT"] = s.LANGSMITH_ENDPOINT
    if s.LANGSMITH_API_KEY:
        os.environ["LANGSMITH_API_KEY"] = s.LANGSMITH_API_KEY
        # Some LangChain versions still read LANGCHAIN_API_KEY
        os.environ["LANGCHAIN_API_KEY"] = s.LANGSMITH_API_KEY
    if s.LANGSMITH_PROJECT:
        os.environ["LANGSMITH_PROJECT"] = s.LANGSMITH_PROJECT


settings = get_settings()
configure_tooling_env()
