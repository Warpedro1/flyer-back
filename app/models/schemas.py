"""Pydantic schemas for Flyer API contracts."""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EMBEDDING_DIMENSIONS = 1536


class ChatSender(str, Enum):
    """Maps to Postgres enum `chat_sender`."""

    user = "user"
    assistant = "assistant"


class MediaType(str, Enum):
    """Maps to Postgres enum `media_type`."""

    image = "image"
    video = "video"


class FollowStatus(str, Enum):
    """Maps to Postgres enum `follow_status` (reject = DELETE row, no rejected value)."""

    pending = "pending"
    accepted = "accepted"


class UserRead(BaseModel):
    """Public-facing profile payload (table `profiles`)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str | None = None
    email: str | None = None
    plan_id: str | None = None
    social_mode_enabled: bool = False
    is_private: bool = False
    last_lat: float | None = None
    last_long: float | None = None
    updated_at: datetime | None = None


class UserUpdate(BaseModel):
    """Only fields users can update directly (anti-mass assignment)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    social_mode_enabled: bool | None = None


class EventMediaRead(BaseModel):
    """Row from `event_media`."""

    model_config = ConfigDict(extra="forbid")

    id: str
    media_url: str
    type: MediaType
    order_index: int = 0


class EventRead(BaseModel):
    """Event row aligned with DDL `events`, plus nested media."""

    model_config = ConfigDict(extra="forbid")

    id: str
    creator_id: str | None = None
    title: str
    description: str | None = None
    category: str | None = None
    location_name: str | None = None
    lat: float = Field(ge=-90.0, le=90.0)
    long: float = Field(ge=-180.0, le=180.0)
    event_date: datetime | None = None
    price: Decimal | None = None
    rating: Decimal | None = None
    attendee_count: int = Field(default=0, ge=0)
    is_boosted: bool = False
    boost_expires_at: datetime | None = None
    created_at: datetime | None = None
    media: list[EventMediaRead] = Field(default_factory=list)


class DiscoverRequest(BaseModel):
    """Input contract for hybrid/geolocation event discovery."""

    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    radius_km: float = Field(default=10.0, gt=0, le=100)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=50)


class ChatMessageIn(BaseModel):
    """User chat input with explicit anti-abuse size limits."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=1000)


class ChatMessageOut(BaseModel):
    """Chat message for API responses (user or assistant)."""

    model_config = ConfigDict(extra="forbid")

    role: ChatSender
    content: str
    created_at: datetime | None = None


class CheckInRequest(BaseModel):
    """Check-in by event; backend resolves trophy template (anti geo-spoofing bounds)."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    lat: float = Field(ge=-90.0, le=90.0)
    long: float = Field(ge=-180.0, le=180.0)


class TrophyRead(BaseModel):
    """User trophy with template metadata (`trophies` + `trophy_templates`)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    template_id: str
    name: str
    description: str | None = None
    icon_url: str | None = None
    acquired_at: datetime | None = None


class FollowRequest(BaseModel):
    """Request payload to follow another user."""

    model_config = ConfigDict(extra="forbid")

    target_user_id: str = Field(min_length=1)


class FollowRead(BaseModel):
    """Follow relationship payload."""

    model_config = ConfigDict(extra="forbid")

    follower_id: str
    following_id: str
    status: FollowStatus
    created_at: datetime | None = None


class ProfileBrief(BaseModel):
    """Minimal profile for social lists (no mass assignment)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str | None = None
    email: str | None = None


class FollowAction(BaseModel):
    """Accept or reject an incoming follow request (recipient only)."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["accept", "reject"]


class PendingFollowOut(BaseModel):
    """Pending follow request with follower profile preview."""

    model_config = ConfigDict(extra="forbid")

    follower_id: str
    following_id: str
    status: FollowStatus
    created_at: datetime | None = None
    follower: ProfileBrief


class PrivacyUpdate(BaseModel):
    """Privacy settings update payload."""

    model_config = ConfigDict(extra="forbid")

    is_private: bool


class EmbeddingVector(BaseModel):
    """Validated embedding shape for internal service use."""

    model_config = ConfigDict(extra="forbid")

    vector: list[float] = Field(min_length=EMBEDDING_DIMENSIONS, max_length=EMBEDDING_DIMENSIONS)


class PlanRead(BaseModel):
    """Public pricing plan (table `plans`)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    price: Decimal | None = None
    interval: str | None = None
    features: list[str] = Field(default_factory=list)
    is_active: bool = True


class EventMediaCreate(BaseModel):
    """Single media item attached to a new event."""

    model_config = ConfigDict(extra="forbid")

    media_url: str = Field(min_length=1)
    type: MediaType
    order_index: int = 0


class EventCreate(BaseModel):
    """Payload for creating a new event."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=100)
    location_name: str | None = Field(default=None, max_length=200)
    lat: float = Field(ge=-90.0, le=90.0)
    long: float = Field(ge=-180.0, le=180.0)
    event_date: datetime | None = None
    price: Decimal | None = Field(default=None, ge=0)
    media: list[EventMediaCreate] = Field(default_factory=list, max_length=10)
