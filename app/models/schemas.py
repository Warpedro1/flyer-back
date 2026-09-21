"""Pydantic schemas for Flyer API contracts."""

import re
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EMBEDDING_DIMENSIONS = 1536


class ChatSender(str, Enum):
    """Maps to Postgres enum `chat_sender`."""

    user = "user"
    assistant = "assistant"


class MediaType(str, Enum):
    """Maps to Postgres enum `media_type`."""

    image = "image"
    video = "video"


class RsvpStatus(str, Enum):
    """Maps to Postgres enum `rsvp_status`.

    Occupancy is `confirmed` + `called` + `admitted`; `waitlisted`, `no_show`
    and `cancelled` hold no seat.
    """

    confirmed = "confirmed"
    waitlisted = "waitlisted"
    called = "called"
    admitted = "admitted"
    no_show = "no_show"
    cancelled = "cancelled"


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
    capacity: int | None = Field(default=None, gt=0)
    waitlist_enabled: bool = True
    call_ttl_minutes: int = Field(default=10, ge=1, le=240)
    capacity_state: "EventCapacityRead | None" = None
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


class CompleteOnboardingIn(BaseModel):
    """Strict key set validated in `onboarding_contract` (422 with stable codes)."""

    model_config = ConfigDict(extra="forbid")

    answers: dict[str, Any]


class CompleteOnboardingOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    steps_indexed: int = 0
    vectorstore_skipped: bool = False


class ValidateOnboardingOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True


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


class RecurrenceMode(str, Enum):
    """How a recurring event expands into concrete occurrences."""

    count = "count"      # repeat every day/week, N times
    weekly = "weekly"    # on chosen weekdays at a time, until a date
    range = "range"      # daily between a start and end date


_HHMM_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


class EventRecurrence(BaseModel):
    """Recurrence spec; the backend expands it into multiple event rows (cap: 60)."""

    model_config = ConfigDict(extra="forbid")

    mode: RecurrenceMode

    # mode == count
    every: Literal["day", "week"] | None = None
    occurrences: int | None = Field(default=None, ge=2, le=60)

    # mode == weekly (0 = Monday … 6 = Sunday)
    weekdays: list[int] | None = None
    time_of_day: str | None = None  # "HH:MM" (24h)
    until: date | None = None

    # mode == range
    start: date | None = None
    end: date | None = None

    @field_validator("time_of_day")
    @classmethod
    def _valid_time(cls, v: str | None) -> str | None:
        if v is not None and not _HHMM_RE.fullmatch(v):
            msg = "time_of_day must be HH:MM (24h)"
            raise ValueError(msg)
        return v

    @field_validator("weekdays")
    @classmethod
    def _valid_weekdays(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return v
        if not v or any(d < 0 or d > 6 for d in v):
            msg = "weekdays must be a non-empty list of integers 0..6"
            raise ValueError(msg)
        return sorted(set(v))

    @model_validator(mode="after")
    def _require_fields_per_mode(self) -> "EventRecurrence":
        if self.mode == RecurrenceMode.count:
            if self.every is None or self.occurrences is None:
                msg = "count recurrence requires 'every' and 'occurrences'"
                raise ValueError(msg)
        elif self.mode == RecurrenceMode.weekly:
            if not self.weekdays or self.time_of_day is None or self.until is None:
                msg = "weekly recurrence requires 'weekdays', 'time_of_day' and 'until'"
                raise ValueError(msg)
        elif self.mode == RecurrenceMode.range:
            if self.start is None or self.end is None or self.time_of_day is None:
                msg = "range recurrence requires 'start', 'end' and 'time_of_day'"
                raise ValueError(msg)
            if self.end < self.start:
                msg = "range 'end' must be on or after 'start'"
                raise ValueError(msg)
        return self


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
    recurrence: EventRecurrence | None = None
    # None = no limit: everybody joins as `confirmed` and the waitlist is never used.
    capacity: int | None = Field(default=None, gt=0)
    waitlist_enabled: bool = True
    call_ttl_minutes: int = Field(default=10, ge=1, le=240)
    auto_call_next: bool = True


class RsvpRead(BaseModel):
    """A single row of `event_rsvps`, as returned to the attendee or the creator."""

    model_config = ConfigDict(extra="forbid")

    id: str
    event_id: str
    user_id: str
    status: RsvpStatus
    waitlist_position: int | None = None
    ahead_count: int | None = None
    called_at: datetime | None = None
    call_expires_at: datetime | None = None
    admitted_at: datetime | None = None
    created_at: datetime | None = None


class RsvpWithProfile(RsvpRead):
    """Queue entry with the attendee's public profile (creator-facing lists)."""

    profile: ProfileBrief


class EventCapacityRead(BaseModel):
    """Capacity snapshot embedded in `EventRead` for the requesting user."""

    model_config = ConfigDict(extra="forbid")

    capacity: int | None = None
    taken: int = Field(default=0, ge=0)
    waitlist_count: int = Field(default=0, ge=0)
    my_status: RsvpStatus | None = None
    my_waitlist_position: int | None = None


class AttendeeListRead(BaseModel):
    """Full queue view for the event creator."""

    model_config = ConfigDict(extra="forbid")

    capacity: int | None = None
    taken: int = Field(default=0, ge=0)
    confirmed: list[RsvpWithProfile] = Field(default_factory=list)
    called: list[RsvpWithProfile] = Field(default_factory=list)
    waitlist: list[RsvpWithProfile] = Field(default_factory=list)
    no_show: list[RsvpWithProfile] = Field(default_factory=list)


class RsvpQrToken(BaseModel):
    """Short-lived admission token the attendee renders as a QR code."""

    model_config = ConfigDict(extra="forbid")

    token: str
    expires_at: datetime


class ScanRequest(BaseModel):
    """Token read off an attendee's QR code by the creator's camera."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=2000)


class ScanResult(BaseModel):
    """Outcome of a successful admission."""

    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    status: RsvpStatus
    attendee: ProfileBrief


EventRead.model_rebuild()
