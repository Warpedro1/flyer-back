"""Pydantic schemas for Flyer API contracts."""

import re
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Self, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.br_documents import normalize_cpf, normalize_phone
from app.core.tz import to_local_naive

EMBEDDING_DIMENSIONS = 1536

# Taxonomy of things to do (decision 2026-10-11). Slugs are stored in `events.category`
# and mirrored by the app (`src/utils/categories.ts`); renaming one needs a data
# migration. Labels live in the app.
EventCategory = Literal[
    "comida_bebida",
    "vida_noturna",
    "musica_show",
    "arte_cultura",
    "teatro_cinema",
    "palestra_conhecimento",
    "cursos_oficinas",
    "estudo_leitura",
    "debate_comunidade",
    "voluntariado",
    "desporto",
    "academia_exercicio",
    "bem_estar",
    "praia_natureza",
    "turismo_passeio",
    "jogos",
    "compras_lojas",
    "feira_mercado",
    "familia_criancas",
    "religiao_espiritualidade",
    "tecnologia_negocios",
    "outros",
]
EVENT_CATEGORIES: tuple[str, ...] = get_args(EventCategory)

# `event` has a date (or was created in the app); `place` is somewhere permanent to go.
EventKind = Literal["event", "place"]

# Whether check-in is possible right now (app/services/checkin_window.py).
CheckinStatus = Literal["open", "not_yet", "closed", "no_schedule"]


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


class OpeningHours(BaseModel):
    """One line of a place's opening hours, as the source gives it."""

    model_config = ConfigDict(extra="forbid")

    day: str = Field(max_length=40)
    hours: str = Field(max_length=120)


class EventRead(BaseModel):
    """Event row aligned with DDL `events`, plus nested media."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: EventKind = "event"
    # Where the event happens, when it is at a known place (kind = place).
    place_id: str | None = None
    creator_id: str | None = None
    title: str
    description: str | None = None
    category: str | None = None
    location_name: str | None = None
    lat: float = Field(ge=-90.0, le=90.0)
    long: float = Field(ge=-180.0, le=180.0)
    event_date: datetime | None = None
    # Check-in window (events): opens at `checkin_opens_at`, closes at `checkin_closes_at`
    # (the end, or earlier if the creator closed it). See app/services/checkin_window.py.
    ends_at: datetime | None = None
    checkin_opens_at: datetime | None = None
    checkin_closes_at: datetime | None = None
    checkin_status: CheckinStatus | None = None
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
    # Places only: opening hours and price level from 1 ($) to 4 ($$$$).
    opening_hours: list[OpeningHours] | None = None
    price_level: int | None = Field(default=None, ge=1, le=4)
    # Ingested items: the page at the source and its public cover image.
    source_url: str | None = None
    cover_url: str | None = None


class DiscoverRequest(BaseModel):
    """Input contract for hybrid/geolocation event discovery.

    Filters are optional. Places have no date nor exact price, so they always pass
    the date and price filters; events with an unknown price do not pass a price
    filter (see the `places_catalog` migration).
    """

    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    radius_km: float = Field(default=10.0, gt=0, le=100)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=50)
    kinds: list[EventKind] | None = Field(default=None, min_length=1, max_length=2)
    categories: list[EventCategory] | None = Field(default=None, min_length=1, max_length=22)
    date_from: datetime | None = None
    date_to: datetime | None = None
    # 0 = free only.
    max_price: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _dates_in_order(self) -> "DiscoverRequest":
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to.")
        return self


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


class TrophyCustomization(BaseModel):
    """The trophy an event gives at check-in, as its creator customised it.

    Every field is optional: what is left out uses the default trophy
    ("Presença: <título>", no description, the Flyer icon).
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=300)
    # Path of an image the creator uploaded to the event-media bucket ({user_id}/...).
    icon_url: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("name", "description", mode="before")
    @classmethod
    def _blank_is_default(cls, v: Any) -> Any:
        # "   " from a client that does not trim would replace the default name.
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v


class _EventFields(BaseModel):
    """What the creator sets on an event, both when creating and when editing.

    Start and end are required (decision 2026-10-12): check-in opens at
    `checkin_opens_at` (the start by default) and closes at the end. Times without a
    timezone are Recife time, which is what the app's date pickers send; they are kept
    as naive Recife wall-clock times here and stored with the -03:00 offset.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    category: EventCategory | None = None
    location_name: str | None = Field(default=None, max_length=200)
    lat: float = Field(ge=-90.0, le=90.0)
    long: float = Field(ge=-180.0, le=180.0)
    event_date: datetime
    ends_at: datetime
    # None = when the event starts.
    checkin_opens_at: datetime | None = None
    price: Decimal | None = Field(default=None, ge=0)
    # The place (kind = place) where this event happens, chosen by the creator.
    place_id: str | None = Field(default=None, min_length=1, max_length=64)
    # The trophy for checking in; None = the default trophy, created at first check-in.
    trophy: TrophyCustomization | None = None
    media: list[EventMediaCreate] = Field(default_factory=list, max_length=10)
    # None = no limit: everybody joins as `confirmed` and the waitlist is never used.
    capacity: int | None = Field(default=None, gt=0)
    waitlist_enabled: bool = True
    call_ttl_minutes: int = Field(default=10, ge=1, le=240)
    auto_call_next: bool = True

    @field_validator("event_date", "ends_at", "checkin_opens_at")
    @classmethod
    def _recife_wall_clock(cls, v: datetime | None) -> datetime | None:
        return to_local_naive(v) if v is not None else None

    @model_validator(mode="after")
    def _window_in_order(self) -> Self:
        if self.ends_at <= self.event_date:
            raise ValueError("O fim tem de ser depois do início.")
        if self.checkin_opens_at is not None and self.checkin_opens_at >= self.ends_at:
            raise ValueError("O check-in tem de abrir antes do fim do evento.")
        return self


class EventCreate(_EventFields):
    """Payload for creating a new event (one row per occurrence when recurring)."""

    recurrence: EventRecurrence | None = None


class EventUpdate(_EventFields):
    """Payload for editing one event (one date of a series): no recurrence."""


class EventMediaEdit(BaseModel):
    """A media item in the edit form: the stored path plus a signed preview."""

    model_config = ConfigDict(extra="forbid")

    media_url: str
    type: MediaType
    order_index: int = 0
    preview_url: str | None = None


class TrophyEditRead(BaseModel):
    """The event's trophy in the edit form: the icon path plus a signed preview."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    icon_url: str | None = None
    icon_preview_url: str | None = None


class EventEditRead(BaseModel):
    """What the edit form needs: stored values and Storage paths (creator only)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str | None = None
    category: str | None = None
    place_id: str | None = None
    location_name: str | None = None
    lat: float
    long: float
    event_date: datetime | None = None
    ends_at: datetime | None = None
    checkin_opens_at: datetime | None = None
    price: Decimal | None = None
    capacity: int | None = None
    waitlist_enabled: bool = True
    call_ttl_minutes: int = 10
    auto_call_next: bool = True
    media: list[EventMediaEdit] = Field(default_factory=list)
    trophy: TrophyEditRead | None = None


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


# ----------------------------------------------------------------- account --

# No commas, parentheses or quotes: the address goes into a PostgREST `or=(...)` filter.
_EMAIL_RE = re.compile(r'^[^@\s,()"]+@[^@\s,()"]+\.[^@\s,()"]+$')


def _valid_password(value: str) -> str:
    """8+ characters with at least one letter and one digit (decision 2026-10-12)."""
    if len(value) < 8 or not any(c.isalpha() for c in value) or not any(c.isdigit() for c in value):
        raise ValueError("A senha precisa de 8+ caracteres, com letras e números.")
    return value


def _valid_cpf(value: str) -> str:
    cpf = normalize_cpf(value)
    if cpf is None:
        raise ValueError("CPF inválido.")
    return cpf


def _valid_phone(value: str) -> str:
    phone = normalize_phone(value)
    if phone is None:
        raise ValueError("Celular inválido: usa DDD + número com 9 dígitos.")
    return phone


class SignupIn(BaseModel):
    """Sign-up by e-mail (decisions 2026-10-12): CPF, phone and e-mail are unique."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=254)
    cpf: str = Field(max_length=20)
    phone: str = Field(max_length=25)
    password: str = Field(max_length=72)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        name = v.strip()
        if not name:
            raise ValueError("Indica o teu nome.")
        return name

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        email = v.strip().lower()
        if not _EMAIL_RE.match(email):
            raise ValueError("E-mail inválido.")
        return email

    @field_validator("cpf")
    @classmethod
    def _cpf(cls, v: str) -> str:
        return _valid_cpf(v)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return _valid_phone(v)

    @field_validator("password")
    @classmethod
    def _password(cls, v: str) -> str:
        return _valid_password(v)


class ProfileIdentityIn(BaseModel):
    """CPF and phone for an account that has none yet (Google, first access)."""

    model_config = ConfigDict(extra="forbid")

    cpf: str = Field(max_length=20)
    phone: str = Field(max_length=25)

    @field_validator("cpf")
    @classmethod
    def _cpf(cls, v: str) -> str:
        return _valid_cpf(v)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return _valid_phone(v)


class SignupOut(BaseModel):
    """The ticket that ties the sign-up code to the browser that signed up (2026-10-14)."""

    model_config = ConfigDict(extra="forbid")

    ticket: str


class SignupActivateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket: str = Field(min_length=20, max_length=200)


class ProfileIdentityStatus(BaseModel):
    """Whether the account has CPF and phone (the owner's own, masked) and whether its
    sign-up still waits for the code."""

    model_config = ConfigDict(extra="forbid")

    complete: bool
    pending: bool = False
    cpf_masked: str | None = None
    phone_masked: str | None = None


class EmailCodeStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool
    verified: bool


class EmailCodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^\d{6}$")


EventRead.model_rebuild()
