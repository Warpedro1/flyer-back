"""Supabase PostgREST access via shared httpx.AsyncClient (singleton from app lifespan)."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import session_invalid
from app.models.schemas import ChatSender, EventMediaRead, FollowStatus, MediaType

logger = logging.getLogger(__name__)

REST_PREFIX = "/rest/v1"
AUTH_PREFIX = "/auth/v1"
STORAGE_PREFIX = "/storage/v1"

POSTGRES_UNIQUE_VIOLATION = "23505"


class UniqueViolationError(Exception):
    """Raised when PostgreSQL reports unique constraint violation (code 23505)."""


class RpcError(Exception):
    """A PL/pgSQL ``RAISE`` surfaced by PostgREST.

    The RSVP functions signal domain outcomes (``event_full``, ``not_event_creator``,
    ...) by raising; the routers map ``message`` onto an HTTP status. Keyed on the
    message rather than SQLSTATE because several raises share a code.
    """

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _json_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _parse_embedding_from_rpc(payload: Any) -> list[float] | None:
    """Best-effort parse of vector from RPC JSON (array, nested row, or string)."""
    if payload is None:
        return None

    def as_float_list(value: Any) -> list[float] | None:
        if value is None:
            return None
        if isinstance(value, list) and value and isinstance(value[0], (int, float)):
            return [float(x) for x in value]
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [float(x) for x in parsed]
        return None

    direct = as_float_list(payload)
    if direct is not None:
        return direct

    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        for row in payload:
            if isinstance(row, dict):
                for val in row.values():
                    parsed = as_float_list(val)
                    if parsed is not None:
                        return parsed

    if isinstance(payload, dict):
        for val in payload.values():
            parsed = as_float_list(val)
            if parsed is not None:
                return parsed

    return None


# An event with no end counts as over 4 h after its start, like the imported ones
# (`etl/transform.py:DEFAULT_EVENT_DURATION`), for "Meus eventos" (#40).
_EVENT_WITHOUT_END_DURATION = timedelta(hours=4)


class DBService:
    """Thin async wrapper around Supabase REST. Inject shared httpx client from lifespan."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    def _user_headers(self, jwt: str) -> dict[str, str]:  # noqa: ARG002
        """Build PostgREST headers for a user-scoped call. Uses the service_role key.

        The user's JWT is NOT forwarded: every call runs as service_role and RLS
        does not apply (issue #7, option B). Authorization lives in the backend:
        the caller must filter by the ``user_id`` from the validated token, or go
        through ``app/api/authz.py``. Nothing below the route will stop a query
        that forgets to.

        ``jwt`` stays in the signature so call sites keep marking which queries are
        user-scoped, and so forwarding it later is a change in one place.
        """
        return self._admin_headers()

    def _admin_headers(self) -> dict[str, str]:
        token = settings.SUPABASE_KEY
        headers = {**_json_headers(), "apikey": token}
        # Secret keys (sb_secret_...) are not JWTs: Supabase rejects them in
        # Authorization with "Invalid JWT", so only the legacy JWT key goes there.
        if not token.startswith("sb_"):
            headers["Authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _extract_pg_code(response: httpx.Response) -> str | None:
        try:
            data = response.json()
            if isinstance(data, dict):
                code = data.get("code")
                if isinstance(code, str):
                    return code
        except Exception:
            return None
        return None

    async def _raise_for_supabase(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        code = self._extract_pg_code(response)
        if code == POSTGRES_UNIQUE_VIOLATION:
            raise UniqueViolationError from None
        response.raise_for_status()

    # ---------------------------------------------------------- app errors --

    async def record_app_error(self, row: dict[str, Any]) -> None:
        """One row in `app_errors` (#25 phase 1). Use `services/error_log.py`."""
        r = await self._client.post(
            f"{REST_PREFIX}/app_errors",
            json=row,
            headers={**self._admin_headers(), "Prefer": "return=minimal"},
        )
        await self._raise_for_supabase(r)

    # ------------------------------------------------- login second factor --

    async def send_email_code(self, email: str) -> None:
        """Ask Supabase Auth to e-mail a 6-digit code (template "Magic Link").

        `create_user: false`: this never creates an account for an unknown address.
        """
        r = await self._client.post(
            f"{AUTH_PREFIX}/otp",
            json={"email": email, "create_user": False},
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)

    async def verify_email_code(self, email: str, code: str) -> bool:
        """Check the code with Supabase Auth. True if it is valid.

        Supabase answers a valid code with a new session; this backend only needs the
        answer, so that extra session is revoked straight away.
        """
        r = await self._client.post(
            f"{AUTH_PREFIX}/verify",
            json={"type": "email", "email": email, "token": code},
            headers=self._admin_headers(),
        )
        if r.status_code in (400, 401, 403, 404, 422):
            return False
        await self._raise_for_supabase(r)
        data = r.json() if r.content else {}
        extra_token = data.get("access_token") if isinstance(data, dict) else None
        if extra_token:
            logout = await self._client.post(
                f"{AUTH_PREFIX}/logout",
                params={"scope": "local"},
                headers={"apikey": settings.SUPABASE_KEY, "Authorization": f"Bearer {extra_token}"},
            )
            if not logout.is_success:
                logger.warning("Could not revoke the session created to check an e-mail code")
        return True

    async def mark_session_verified(self, session_id: str, user_id: str) -> None:
        r = await self._client.post(
            f"{REST_PREFIX}/login_second_factor",
            params={"on_conflict": "session_id"},
            json={"session_id": session_id, "user_id": user_id},
            headers={**self._admin_headers(), "Prefer": "resolution=ignore-duplicates,return=minimal"},
        )
        await self._raise_for_supabase(r)

    async def is_session_verified(self, session_id: str) -> bool:
        r = await self._client.get(
            f"{REST_PREFIX}/login_second_factor",
            params={"session_id": f"eq.{session_id}", "select": "session_id", "limit": "1"},
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        return isinstance(rows, list) and bool(rows)

    # ------------------------------------------------ account: CPF and phone --

    async def signup_user(self, *, email: str, password: str, data: dict[str, Any]) -> str:
        """Create the account in Supabase Auth and return its id.

        Supabase e-mails the 6-digit confirmation code (template "Confirm signup", no
        link: decision 2026-10-14). The profile trigger copies `data` (name, cpf, phone,
        `flyer_signup`) into `profiles`, whose unique indexes reject a repeated CPF or
        phone even under a race, and creates the profile as pending.
        """
        r = await self._client.post(
            f"{AUTH_PREFIX}/signup",
            json={"email": email, "password": password, "data": data},
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)
        body = r.json() if r.content else {}
        user = body.get("user") if isinstance(body.get("user"), dict) else body
        user_id = user.get("id") if isinstance(user, dict) else None
        if not isinstance(user_id, str) or not user_id:
            raise RuntimeError("Supabase sign-up answered without a user id")
        return user_id

    async def delete_auth_user(self, user_id: str) -> None:
        """Delete an account in Supabase Auth (its profile and ticket go with it)."""
        r = await self._client.delete(
            f"{AUTH_PREFIX}/admin/users/{user_id}", headers=self._admin_headers()
        )
        if r.status_code == 404:
            return
        await self._raise_for_supabase(r)

    # ------------------------------------ sign-up tied to its browser (ticket) --

    async def create_signup_ticket(self, user_id: str, ticket_hash: str) -> None:
        r = await self._client.post(
            f"{REST_PREFIX}/signup_tickets",
            params={"on_conflict": "user_id"},
            json={"user_id": user_id, "ticket_hash": ticket_hash},
            headers={**self._admin_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        await self._raise_for_supabase(r)

    async def consume_signup_ticket(self, user_id: str, ticket_hash: str) -> bool:
        """Delete the ticket if it matches this account. True when it did."""
        r = await self._client.delete(
            f"{REST_PREFIX}/signup_tickets",
            params={"user_id": f"eq.{user_id}", "ticket_hash": f"eq.{ticket_hash}"},
            headers={**self._admin_headers(), "Prefer": "return=representation"},
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        return isinstance(rows, list) and bool(rows)

    async def activate_signup(self, user_id: str) -> None:
        r = await self._client.patch(
            f"{REST_PREFIX}/profiles",
            params={"id": f"eq.{user_id}"},
            json={"signup_pending": False},
            headers={**self._admin_headers(), "Prefer": "return=minimal"},
        )
        await self._raise_for_supabase(r)

    # ------------------------------------------------ account: CPF and phone --

    @staticmethod
    def _identity_filter(email: str | None, cpf: str, phone: str | None) -> str:
        filters = [f"cpf.eq.{cpf}"]
        if phone:
            filters.append(f"phone.eq.{phone}")
        if email:
            filters.append(f"email.eq.{email}")
        return f"({','.join(filters)})"

    async def find_identity_conflicts(
        self,
        *,
        email: str | None,
        cpf: str,
        phone: str | None,
        exclude_user_id: str | None = None,
        active_only: bool = False,
    ) -> set[str]:
        """Which of e-mail, CPF and phone already belong to another account.

        `active_only` leaves out sign-ups still waiting for their code, which a new
        sign-up replaces (`find_pending_signups`).
        """
        params = {
            "select": "id,cpf,phone,email",
            "or": self._identity_filter(email, cpf, phone),
            "limit": "10",
        }
        if exclude_user_id:
            params["id"] = f"neq.{exclude_user_id}"
        if active_only:
            params["signup_pending"] = "is.false"
        r = await self._client.get(
            f"{REST_PREFIX}/profiles", params=params, headers=self._admin_headers()
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        taken: set[str] = set()
        for row in rows if isinstance(rows, list) else []:
            if row.get("cpf") == cpf:
                taken.add("cpf")
            if phone and row.get("phone") == phone:
                taken.add("phone")
            if email and str(row.get("email") or "").lower() == email:
                taken.add("email")
        return taken

    async def find_pending_signups(self, *, email: str | None, cpf: str, phone: str | None) -> list[str]:
        """Ids of sign-ups still waiting for their code that use this e-mail, CPF or phone."""
        r = await self._client.get(
            f"{REST_PREFIX}/profiles",
            params={
                "select": "id",
                "or": self._identity_filter(email, cpf, phone),
                "signup_pending": "is.true",
                "limit": "10",
            },
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        return [str(row["id"]) for row in rows if row.get("id")] if isinstance(rows, list) else []

    async def get_profile_identity(self, user_id: str) -> dict[str, Any]:
        """The owner's own CPF and phone (never returned for anyone else), and whether
        the sign-up still waits for its code."""
        r = await self._client.get(
            f"{REST_PREFIX}/profiles",
            params={
                "id": f"eq.{user_id}",
                "select": "cpf,phone,signup_pending,intro_seen_at,onboarding_preferences_hash",
                "limit": "1",
            },
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if isinstance(rows, list) and rows:
            return dict(rows[0])
        return {"cpf": None, "phone": None, "signup_pending": False}

    async def mark_intro_seen(self, user_id: str) -> None:
        """Record that the introduction tour was finished or skipped (Warpedro1/flyer#23).
        Only the first time: a repeat keeps the original date."""
        r = await self._client.patch(
            f"{REST_PREFIX}/profiles",
            params={"id": f"eq.{user_id}", "intro_seen_at": "is.null"},
            json={"intro_seen_at": datetime.now(timezone.utc).isoformat()},
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)

    async def get_account_state(self, user_id: str) -> str:
        """`pending` (sign-up without its code), `incomplete` (no CPF) or `ok`.

        The phone is optional (#29, 2026-10-17)."""
        identity = await self.get_profile_identity(user_id)
        if identity.get("signup_pending"):
            return "pending"
        if not identity.get("cpf"):
            return "incomplete"
        return "ok"

    async def complete_profile_identity(self, user_id: str, cpf: str, phone: str | None) -> bool:
        """Set the CPF (and the phone, when given) once. False when the account already
        had a CPF."""
        r = await self._client.patch(
            f"{REST_PREFIX}/profiles",
            params={"id": f"eq.{user_id}", "cpf": "is.null"},
            json={"cpf": cpf, **({"phone": phone} if phone else {})},
            headers={**self._admin_headers(), "Prefer": "return=representation"},
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        return isinstance(rows, list) and bool(rows)

    async def _find_chat(self, user_id: str, jwt: str) -> str | None:
        r = await self._client.get(
            f"{REST_PREFIX}/ai_chats",
            params={"user_id": f"eq.{user_id}", "select": "id"},
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if isinstance(rows, list) and rows and rows[0].get("id"):
            return str(rows[0]["id"])
        return None

    async def get_or_create_chat(self, user_id: str, jwt: str) -> str:
        """Return the single ai_chats.id for this user (UNIQUE user_id).

        Two requests at once both miss the SELECT; the second INSERT answers 409 and
        reads the chat the first one created (#30). A 409 with no chat to read is the
        foreign key: the account was deleted while its session lived.
        """
        found = await self._find_chat(user_id, jwt)
        if found:
            return found

        ins = await self._client.post(
            f"{REST_PREFIX}/ai_chats",
            json={"user_id": user_id},
            headers={
                **self._user_headers(jwt),
                "Prefer": "return=representation",
            },
        )
        if ins.status_code == httpx.codes.CONFLICT:
            found = await self._find_chat(user_id, jwt)
            if found:
                return found
            raise session_invalid()
        await self._raise_for_supabase(ins)
        created = ins.json()
        if isinstance(created, list) and created:
            return str(created[0]["id"])
        if isinstance(created, dict):
            return str(created["id"])
        msg = "Could not create ai_chats row"
        raise RuntimeError(msg)

    async def get_recent_messages(
        self,
        chat_id: str,
        jwt: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return last `limit` messages, oldest-first (for LLM context)."""
        r = await self._client.get(
            f"{REST_PREFIX}/chat_messages",
            params={
                "chat_id": f"eq.{chat_id}",
                "select": "id,sender,content,created_at",
                "order": "created_at.desc",
                "limit": str(limit),
            },
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if not isinstance(rows, list):
            return []
        chronological = list(reversed(rows))
        return chronological

    async def save_message(
        self,
        chat_id: str,
        sender: ChatSender,
        content: str,
        jwt: str,
    ) -> None:
        resp = await self._client.post(
            f"{REST_PREFIX}/chat_messages",
            json={
                "chat_id": chat_id,
                "sender": sender.value,
                "content": content,
            },
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(resp)

    async def get_profile(self, user_id: str, jwt: str) -> dict[str, Any] | None:
        r = await self._client.get(
            f"{REST_PREFIX}/profiles",
            params={"id": f"eq.{user_id}", "select": "*"},
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if isinstance(rows, list) and rows:
            return rows[0]
        return None

    async def get_or_create_profile(self, user_id: str, jwt: str) -> dict[str, Any]:
        """Ensure a `profiles` row exists for this user (required for onboarding + embeddings)."""
        row = await self.get_profile(user_id, jwt)
        if row is not None:
            return row
        ins = await self._client.post(
            f"{REST_PREFIX}/profiles",
            json={"id": user_id},
            headers={
                **self._user_headers(jwt),
                "Prefer": "return=representation",
            },
        )
        if ins.status_code == 409:
            row2 = await self.get_profile(user_id, jwt)
            if row2 is not None:
                return row2
        await self._raise_for_supabase(ins)
        created = ins.json()
        if isinstance(created, list) and created:
            return created[0]
        if isinstance(created, dict):
            return created
        msg = "Could not create profile row"
        raise RuntimeError(msg)

    async def update_profile(
        self,
        user_id: str,
        data: dict[str, Any],
        jwt: str,
    ) -> None:
        r = await self._client.patch(
            f"{REST_PREFIX}/profiles",
            params={"id": f"eq.{user_id}"},
            json=data,
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)

    async def update_profile_embedding(self, user_id: str, vector: list[float]) -> None:
        r = await self._client.patch(
            f"{REST_PREFIX}/profiles",
            params={"id": f"eq.{user_id}"},
            json={"interest_embedding": vector},
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)

    async def update_interest_vector_if_count(
        self,
        user_id: str,
        vector: list[float],
        expected_count: int | None,
    ) -> bool:
        """Store a synced interest vector and bump the sync counter in one write.

        Guarded by the counter the caller read (compare-and-swap): returns False when
        another sync changed it first, so the caller can re-read and blend again.
        Writing both together means the counter can no longer lag behind the vector.
        """
        count_filter = "is.null" if expected_count is None else f"eq.{expected_count}"
        r = await self._client.patch(
            f"{REST_PREFIX}/profiles",
            params={"id": f"eq.{user_id}", "interest_sync_count_after_onboarding": count_filter},
            json={
                "interest_embedding": vector,
                "interest_sync_count_after_onboarding": (expected_count or 0) + 1,
            },
            headers={**self._admin_headers(), "Prefer": "return=representation"},
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        return isinstance(rows, list) and bool(rows)

    async def get_social_avg_embedding(self, user_id: str) -> list[float] | None:
        """RPC: average interest_embedding for accepted follows (server-side)."""
        r = await self._client.post(
            f"{REST_PREFIX}/rpc/get_social_avg_embedding",
            json={"user_uid": user_id},
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)
        return _parse_embedding_from_rpc(r.json())

    async def call_match_events(
        self,
        query_embedding: list[float] | None,
        user_lat: float,
        user_lng: float,
        radius_km: float,
        *,
        kinds: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        max_price: Decimal | None = None,
    ) -> list[dict[str, Any]]:
        """Event and place ids within the radius, best match first (max 200, no past events).

        ``query_embedding=None`` is the fallback for users without an interest
        vector: the same nearby items, soonest first. Filters left as None are not
        sent and the function's defaults (no filter) apply. See the
        ``places_catalog`` migration for how places meet the date and price filters.
        """
        payload: dict[str, Any] = {
            "query_embedding": query_embedding,
            "user_lat": user_lat,
            "user_lng": user_lng,
            "radius_km": radius_km,
        }
        filters: dict[str, Any] = {
            "kinds": list(kinds) if kinds else None,
            "categories": list(categories) if categories else None,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "max_price": str(max_price) if max_price is not None else None,
        }
        payload.update({k: v for k, v in filters.items() if v is not None})
        r = await self._client.post(
            f"{REST_PREFIX}/rpc/match_events",
            json=payload,
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)
        data = r.json()
        if isinstance(data, list):
            return data
        return []

    async def upsert_user_taste_documents(
        self,
        user_id: str,
        items: list[dict[str, Any]],
    ) -> int:
        """Upsert per-step taste docs (idempotent on the (user_id, step_key) unique key)."""
        if not items:
            return 0
        r = await self._client.post(
            f"{REST_PREFIX}/user_taste_documents",
            params={"on_conflict": "user_id,step_key"},
            json=items,
            headers={
                **self._admin_headers(),
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
        )
        await self._raise_for_supabase(r)
        return len(items)

    async def match_user_taste_documents(
        self,
        user_id: str,
        query_embedding: list[float],
        match_count: int = 4,
    ) -> list[dict[str, Any]]:
        """RPC: top taste docs for this user by cosine similarity to the query vector."""
        r = await self._client.post(
            f"{REST_PREFIX}/rpc/match_user_taste_documents",
            json={
                "p_user_id": user_id,
                "query_embedding": query_embedding,
                "match_count": match_count,
            },
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)
        data = r.json()
        if isinstance(data, list):
            return data
        return []

    async def sign_media_paths(self, paths: list[str]) -> dict[str, str]:
        """Map private Storage paths -> short-lived signed URLs (service_role).

        Values that are already absolute URLs (legacy `http(s)://` media) pass through
        unchanged. Paths that fail to sign are simply omitted (caller keeps the raw path).
        """
        out: dict[str, str] = {}
        to_sign: list[str] = []
        for p in paths:
            if not p:
                continue
            if p.startswith("http://") or p.startswith("https://"):
                out[p] = p
            elif p not in out and p not in to_sign:
                to_sign.append(p)
        if not to_sign:
            return out

        r = await self._client.post(
            f"{STORAGE_PREFIX}/object/sign/{settings.MEDIA_BUCKET}",
            json={"expiresIn": settings.MEDIA_SIGNED_URL_TTL_SECONDS, "paths": to_sign},
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)
        data = r.json()
        base = settings.SUPABASE_URL.rstrip("/")
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                path = entry.get("path")
                signed = entry.get("signedURL") or entry.get("signedUrl")
                if not path or not signed:
                    continue
                signed_str = str(signed)
                out[str(path)] = (
                    f"{base}{STORAGE_PREFIX}{signed_str}" if signed_str.startswith("/") else signed_str
                )
        return out

    async def batch_fetch_event_media(
        self,
        event_ids: list[str],
        jwt: str,
    ) -> dict[str, list[EventMediaRead]]:
        """Single IN query for all media rows (anti N+1)."""
        if not event_ids:
            return {}
        joined = ",".join(event_ids)
        r = await self._client.get(
            f"{REST_PREFIX}/event_media",
            params={
                "event_id": f"in.({joined})",
                "select": "*",
                "order": "order_index.asc",
            },
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if not isinstance(rows, list):
            return {}

        # Stored media_url is a private Storage path; mint short-lived signed URLs.
        paths = [str(row["media_url"]) for row in rows if row.get("media_url")]
        signed = await self.sign_media_paths(paths)

        by_event: dict[str, list[EventMediaRead]] = {}
        for row in rows:
            eid = row.get("event_id")
            if not eid:
                continue
            eid_str = str(eid)
            raw_type = row.get("type")
            if raw_type is None:
                continue
            try:
                media_type = MediaType(str(raw_type))
            except ValueError:
                continue
            raw_url = str(row["media_url"])
            item = EventMediaRead(
                id=str(row["id"]),
                media_url=signed.get(raw_url, raw_url),
                type=media_type,
                order_index=int(row.get("order_index") or 0),
            )
            by_event.setdefault(eid_str, []).append(item)
        for media_list in by_event.values():
            media_list.sort(key=lambda m: m.order_index)
        return by_event

    async def create_trophy_templates(self, rows: list[dict[str, Any]]) -> None:
        """Insert custom trophy templates (one per event occurrence). service_role."""
        if not rows:
            return
        r = await self._client.post(
            f"{REST_PREFIX}/trophy_templates",
            json=rows,
            headers={**self._admin_headers(), "Prefer": "return=minimal"},
        )
        await self._raise_for_supabase(r)

    async def upsert_trophy_template(self, row: dict[str, Any]) -> None:
        """Create or replace the trophy of one event (`event_id` is unique)."""
        r = await self._client.post(
            f"{REST_PREFIX}/trophy_templates",
            params={"on_conflict": "event_id"},
            json=row,
            headers={**self._admin_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        await self._raise_for_supabase(r)

    async def ensure_default_trophy_template(self, event_id: str, name: str) -> dict[str, Any] | None:
        """The event's trophy template, creating the default one if it has none.

        Safe when two people check in at once: `event_id` is unique, the second
        insert is ignored and the row is read back.
        """
        r = await self._client.post(
            f"{REST_PREFIX}/trophy_templates",
            params={"on_conflict": "event_id"},
            json={"event_id": event_id, "name": name},
            headers={**self._admin_headers(), "Prefer": "resolution=ignore-duplicates,return=minimal"},
        )
        await self._raise_for_supabase(r)
        return await self.get_trophy_template_by_event(event_id)

    async def get_trophy_template_by_event(self, event_id: str) -> dict[str, Any] | None:
        """The event's trophy (id, name, description, icon path), or None."""
        r = await self._client.get(
            f"{REST_PREFIX}/trophy_templates",
            params={
                "event_id": f"eq.{event_id}",
                "select": "id,name,description,icon_url",
                "limit": "1",
            },
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if isinstance(rows, list) and rows and rows[0].get("id"):
            return dict(rows[0])
        return None

    async def insert_trophy(self, user_id: str, template_id: str, jwt: str) -> dict[str, Any]:
        r = await self._client.post(
            f"{REST_PREFIX}/trophies",
            json={"user_id": user_id, "template_id": template_id},
            headers={
                **self._user_headers(jwt),
                "Prefer": "return=representation",
            },
        )
        await self._raise_for_supabase(r)
        data = r.json()
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
        return {}

    async def get_user_trophy_for_template(
        self, user_id: str, template_id: str
    ) -> dict[str, Any] | None:
        """The trophy ``user_id`` already has from ``template_id``, if any."""
        r = await self._client.get(
            f"{REST_PREFIX}/trophies",
            params={
                "user_id": f"eq.{user_id}",
                "template_id": f"eq.{template_id}",
                "select": "id,acquired_at",
                "limit": "1",
            },
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        return rows[0] if isinstance(rows, list) and rows else None

    async def create_follow(
        self,
        follower_id: str,
        following_id: str,
        status: FollowStatus,
        jwt: str,
    ) -> None:
        r = await self._client.post(
            f"{REST_PREFIX}/follows",
            json={
                "follower_id": follower_id,
                "following_id": following_id,
                "status": status.value,
            },
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)

    async def get_target_privacy(self, target_id: str, jwt: str) -> bool | None:
        r = await self._client.get(
            f"{REST_PREFIX}/profiles",
            params={"id": f"eq.{target_id}", "select": "is_private"},
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if isinstance(rows, list) and rows:
            return bool(rows[0].get("is_private", False))
        return None

    async def _fetch_profiles_batch(self, profile_ids: list[str], jwt: str) -> dict[str, dict[str, Any]]:
        if not profile_ids:
            return {}
        joined = ",".join(profile_ids)
        r = await self._client.get(
            f"{REST_PREFIX}/profiles",
            params={"id": f"in.({joined})", "select": "id,name"},
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        out: dict[str, dict[str, Any]] = {}
        if isinstance(rows, list):
            for row in rows:
                pid = str(row.get("id", ""))
                if pid:
                    out[pid] = row
        return out

    async def get_pending_requests(self, user_id: str, jwt: str) -> list[dict[str, Any]]:
        r = await self._client.get(
            f"{REST_PREFIX}/follows",
            params={
                "following_id": f"eq.{user_id}",
                "status": "eq.pending",
                "select": "follower_id,following_id,status,created_at",
            },
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return []
        follower_ids = [str(row["follower_id"]) for row in rows if row.get("follower_id")]
        profiles = await self._fetch_profiles_batch(follower_ids, jwt)
        result: list[dict[str, Any]] = []
        for row in rows:
            fid = str(row.get("follower_id", ""))
            prof = profiles.get(fid, {})
            result.append(
                {
                    "follower_id": fid,
                    "following_id": str(row.get("following_id", "")),
                    "status": row.get("status"),
                    "created_at": row.get("created_at"),
                    "follower_name": prof.get("name"),
                }
            )
        return result

    async def accept_follow(self, follower_id: str, following_id: str, jwt: str) -> None:
        r = await self._client.patch(
            f"{REST_PREFIX}/follows",
            params={
                "follower_id": f"eq.{follower_id}",
                "following_id": f"eq.{following_id}",
            },
            json={"status": FollowStatus.accepted.value},
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)

    async def delete_follow(self, follower_id: str, following_id: str, jwt: str) -> None:
        r = await self._client.delete(
            f"{REST_PREFIX}/follows",
            params={
                "follower_id": f"eq.{follower_id}",
                "following_id": f"eq.{following_id}",
            },
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)

    async def get_following_list(self, user_id: str, jwt: str) -> list[dict[str, Any]]:
        r = await self._client.get(
            f"{REST_PREFIX}/follows",
            params={
                "follower_id": f"eq.{user_id}",
                "status": "eq.accepted",
                "select": "following_id,created_at",
            },
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return []
        ids = [str(row["following_id"]) for row in rows if row.get("following_id")]
        profiles = await self._fetch_profiles_batch(ids, jwt)
        return [
            {
                "id": pid,
                "name": profiles.get(pid, {}).get("name"),
            }
            for pid in ids
        ]

    async def get_followers_list(self, user_id: str, jwt: str) -> list[dict[str, Any]]:
        r = await self._client.get(
            f"{REST_PREFIX}/follows",
            params={
                "following_id": f"eq.{user_id}",
                "status": "eq.accepted",
                "select": "follower_id,created_at",
            },
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return []
        ids = [str(row["follower_id"]) for row in rows if row.get("follower_id")]
        profiles = await self._fetch_profiles_batch(ids, jwt)
        return [
            {
                "id": pid,
                "name": profiles.get(pid, {}).get("name"),
            }
            for pid in ids
        ]

    async def batch_fetch_events_by_ids(
        self,
        event_ids: list[str],
        jwt: str,
    ) -> dict[str, dict[str, Any]]:
        if not event_ids:
            return {}
        joined = ",".join(event_ids)
        r = await self._client.get(
            f"{REST_PREFIX}/events",
            params={"id": f"in.({joined})", "select": "*"},
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        out: dict[str, dict[str, Any]] = {}
        if isinstance(rows, list):
            for row in rows:
                eid = str(row.get("id", ""))
                if eid:
                    out[eid] = row
        return out

    async def get_plans(self) -> list[dict[str, Any]]:
        """Return active plans ordered by price (nulls = free first).

        Returns an empty list when the ``plans`` table has not been created yet
        (PostgREST replies with 404 for unknown relations).
        """
        r = await self._client.get(
            f"{REST_PREFIX}/plans",
            params={
                "is_active": "eq.true",
                "select": "id,name,price,interval,features,is_active",
                "order": "price.asc.nullsfirst",
            },
            headers=self._admin_headers(),
        )
        if r.status_code == 404:
            return []
        await self._raise_for_supabase(r)
        rows = r.json()
        return rows if isinstance(rows, list) else []

    async def create_event(
        self,
        creator_id: str,
        data: dict[str, Any],
        jwt: str,
    ) -> dict[str, Any]:
        """Insert a new event row and return the created record."""
        payload = {**data, "creator_id": creator_id}
        r = await self._client.post(
            f"{REST_PREFIX}/events",
            json=payload,
            headers={
                **self._user_headers(jwt),
                "Prefer": "return=representation",
            },
        )
        await self._raise_for_supabase(r)
        created = r.json()
        if isinstance(created, list) and created:
            return created[0]
        if isinstance(created, dict):
            return created
        msg = "Could not create event row"
        raise RuntimeError(msg)

    async def create_events_bulk(
        self,
        creator_id: str,
        rows: list[dict[str, Any]],
        jwt: str,
    ) -> list[dict[str, Any]]:
        """Insert one or more event rows in a single request; return created records.

        Used for both single-create and recurrence expansion (a list of length 1..N).
        """
        if not rows:
            return []
        payload = [{**row, "creator_id": creator_id} for row in rows]
        r = await self._client.post(
            f"{REST_PREFIX}/events",
            json=payload,
            headers={
                **self._user_headers(jwt),
                "Prefer": "return=representation",
            },
        )
        await self._raise_for_supabase(r)
        created = r.json()
        if isinstance(created, list):
            return created
        if isinstance(created, dict):
            return [created]
        return []

    async def upsert_events(self, rows: list[dict[str, Any]]) -> int:
        """Idempotent upsert of ingested events (conflict on source + source_event_id).

        Uses the service_role key (global/system write, no per-user RLS scope).
        """
        if not rows:
            return 0
        r = await self._client.post(
            f"{REST_PREFIX}/events",
            params={"on_conflict": "source,source_event_id"},
            json=rows,
            headers={
                **self._admin_headers(),
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
        )
        await self._raise_for_supabase(r)
        return len(rows)

    async def list_places_near(
        self,
        min_lat: float,
        max_lat: float,
        min_lng: float,
        max_lng: float,
    ) -> list[dict[str, Any]]:
        """Places (kind = place) inside a lat/long box. ETL only: service_role."""
        r = await self._client.get(
            f"{REST_PREFIX}/events",
            params=[
                ("kind", "eq.place"),
                ("lat", f"gte.{min_lat}"),
                ("lat", f"lte.{max_lat}"),
                ("long", f"gte.{min_lng}"),
                ("long", f"lte.{max_lng}"),
                ("select", "id,title,lat,long"),
                ("limit", "5000"),
            ],
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        return rows if isinstance(rows, list) else []

    async def link_event_to_place(self, source: str, source_event_id: str, place_id: str) -> bool:
        """Set an imported event's place, unless it already has one (a manual link wins)."""
        r = await self._client.patch(
            f"{REST_PREFIX}/events",
            params={
                "source": f"eq.{source}",
                "source_event_id": f"eq.{source_event_id}",
                "place_id": "is.null",
            },
            json={"place_id": place_id},
            headers={**self._admin_headers(), "Prefer": "return=representation"},
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        return isinstance(rows, list) and len(rows) > 0

    async def insert_event_media(
        self,
        event_id: str,
        media_items: list[dict[str, Any]],
        jwt: str,
    ) -> None:
        """Bulk-insert media rows for a given event."""
        if not media_items:
            return
        rows = [
            {
                "event_id": event_id,
                "media_url": m["media_url"],
                "type": m["type"],
                "order_index": m.get("order_index", idx),
            }
            for idx, m in enumerate(media_items)
        ]
        await self.insert_event_media_rows(rows, jwt)

    async def insert_event_media_rows(
        self,
        rows: list[dict[str, Any]],
        jwt: str,
    ) -> None:
        """Bulk-insert pre-built media rows (each already carries its own event_id)."""
        if not rows:
            return
        r = await self._client.post(
            f"{REST_PREFIX}/event_media",
            json=rows,
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)

    async def update_event(
        self, event_id: str, creator_id: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update one event the user created; returns the new row (None if not theirs)."""
        r = await self._client.patch(
            f"{REST_PREFIX}/events",
            params={"id": f"eq.{event_id}", "creator_id": f"eq.{creator_id}"},
            json=payload,
            headers={**self._admin_headers(), "Prefer": "return=representation"},
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if isinstance(rows, list) and rows:
            return dict(rows[0])
        return None

    async def delete_events(self, event_ids: list[str], creator_id: str) -> None:
        """Delete events the user created (media, trophies and RSVPs go by cascade)."""
        if not event_ids:
            return
        r = await self._client.delete(
            f"{REST_PREFIX}/events",
            params={"id": f"in.({','.join(event_ids)})", "creator_id": f"eq.{creator_id}"},
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)

    async def get_event_media_rows(self, event_id: str) -> list[dict[str, Any]]:
        """Stored media of one event, with the raw Storage paths (for the edit form)."""
        r = await self._client.get(
            f"{REST_PREFIX}/event_media",
            params={
                "event_id": f"eq.{event_id}",
                "select": "media_url,type,order_index",
                "order": "order_index.asc",
            },
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        return [dict(row) for row in rows] if isinstance(rows, list) else []

    async def replace_event_media(self, event_id: str, rows: list[dict[str, Any]]) -> None:
        """Replace the media of one event with ``rows`` (each carries the event_id)."""
        r = await self._client.delete(
            f"{REST_PREFIX}/event_media",
            params={"event_id": f"eq.{event_id}"},
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)
        if not rows:
            return
        r = await self._client.post(
            f"{REST_PREFIX}/event_media",
            json=rows,
            headers={**self._admin_headers(), "Prefer": "return=minimal"},
        )
        await self._raise_for_supabase(r)

    async def set_events_embedding(self, event_ids: list[str], vector: list[float]) -> None:
        """Set the same interest embedding on the given events (for vector discovery)."""
        if not event_ids:
            return
        joined = ",".join(event_ids)
        r = await self._client.patch(
            f"{REST_PREFIX}/events",
            params={"id": f"in.({joined})"},
            json={"event_embedding": vector},
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)

    async def get_event_by_id(
        self,
        event_id: str,
        jwt: str,
        *,
        select: str = "*",
    ) -> dict[str, Any] | None:
        """Return a single event row or None (only the ``select`` columns)."""
        r = await self._client.get(
            f"{REST_PREFIX}/events",
            params={"id": f"eq.{event_id}", "select": select, "limit": "1"},
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if isinstance(rows, list) and rows:
            return rows[0]
        return None

    async def list_place_events(
        self,
        place_id: str,
        jwt: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Upcoming events at a place, soonest first."""
        r = await self._client.get(
            f"{REST_PREFIX}/events",
            params={
                "place_id": f"eq.{place_id}",
                "event_date": f"gte.{datetime.now(timezone.utc).isoformat()}",
                "select": "*",
                "order": "event_date.asc",
                "limit": str(limit),
            },
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        return rows if isinstance(rows, list) else []

    @staticmethod
    def _ended_filter(when: str) -> tuple[str, str]:
        """PostgREST `or` filter and order for events that ended (`past`) or not
        (`upcoming`). The end is `ends_at`, or the start + 4 h without one (as imported
        events close); an event with no date at all counts as upcoming."""
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        start_cut = (now - _EVENT_WITHOUT_END_DURATION).strftime("%Y-%m-%dT%H:%M:%SZ")
        if when == "past":
            return f"(ends_at.lt.{stamp},and(ends_at.is.null,event_date.lt.{start_cut}))", "desc"
        return (
            f"(ends_at.gte.{stamp},and(ends_at.is.null,event_date.gte.{start_cut}),"
            "and(ends_at.is.null,event_date.is.null))",
            "asc",
        )

    async def get_events_by_creator(
        self,
        creator_id: str,
        jwt: str,
        *,
        limit: int = 50,
        offset: int = 0,
        when: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return events created by a specific user.

        Without `when`, newest created first (the original "Meus eventos"); with it,
        only events that ended (`past`) or not (`upcoming`), by date (#40).
        """
        params = {
            "creator_id": f"eq.{creator_id}",
            "select": "*",
            "order": "created_at.desc",
            "limit": str(limit),
            "offset": str(offset),
        }
        if when is not None:
            ended, direction = self._ended_filter(when)
            params.update({"kind": "eq.event", "or": ended, "order": f"event_date.{direction}"})
        r = await self._client.get(
            f"{REST_PREFIX}/events",
            params=params,
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        return rows if isinstance(rows, list) else []

    async def get_events_going(
        self, user_id: str, jwt: str, *, when: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Events you are going to (#40): RSVP confirmed, waitlisted, called or admitted,
        never cancelled nor no_show, and never places. Each row is the event with
        `_rsvp` = your `{status, waitlist_position}`."""
        params = {
            "user_id": f"eq.{user_id}",
            "status": "in.(confirmed,waitlisted,called,admitted)",
            "select": "status,waitlist_position,events!inner(*)",
            "events.kind": "eq.event",
            "limit": str(limit),
        }
        direction = "asc"
        if when is not None:
            ended, direction = self._ended_filter(when)
            params["events.or"] = ended
        params["order"] = f"events(event_date).{direction}"
        r = await self._client.get(
            f"{REST_PREFIX}/event_rsvps", params=params, headers=self._user_headers(jwt)
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        out: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            event = row.get("events")
            if isinstance(event, dict) and event.get("id"):
                rsvp = {"status": row.get("status"), "waitlist_position": row.get("waitlist_position")}
                out.append({**event, "_rsvp": rsvp})
        return out

    async def get_user_trophies(
        self,
        user_id: str,
        jwt: str,
    ) -> list[dict[str, Any]]:
        """Return trophies with template metadata for a user."""
        r = await self._client.get(
            f"{REST_PREFIX}/trophies",
            params={
                "user_id": f"eq.{user_id}",
                "select": "id,template_id,acquired_at,trophy_templates(name,description,icon_url,event_id)",
                "order": "acquired_at.desc",
            },
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        return rows if isinstance(rows, list) else []

    # ------------------------------------------------------------------ RSVP --
    # Every queue write goes through a SECURITY DEFINER function: PostgREST has no
    # multi-statement transaction, so "count the seats then decide" over two
    # requests lets two people through the last seat at once.

    @staticmethod
    def _rpc_message(response: httpx.Response) -> str | None:
        try:
            data = response.json()
        except Exception:
            return None
        if isinstance(data, dict):
            msg = data.get("message")
            if isinstance(msg, str):
                return msg
        return None

    async def _raise_for_rpc(self, response: httpx.Response) -> None:
        """Translate a failed RSVP RPC into RpcError / UniqueViolationError."""
        if response.is_success:
            return
        code = self._extract_pg_code(response)
        if code == POSTGRES_UNIQUE_VIOLATION:
            raise UniqueViolationError from None
        message = self._rpc_message(response)
        if message:
            raise RpcError(message, code) from None
        response.raise_for_status()

    @staticmethod
    def _single_rsvp(payload: Any) -> dict[str, Any] | None:
        """RPCs returning `event_rsvps` give an object, a 1-item list, or null."""
        if isinstance(payload, list):
            payload = payload[0] if payload else None
        if isinstance(payload, dict) and payload.get("id"):
            return payload
        return None

    async def _call_rsvp_rpc(
        self,
        name: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        r = await self._client.post(
            f"{REST_PREFIX}/rpc/{name}",
            json=params,
            headers=self._admin_headers(),
        )
        await self._raise_for_rpc(r)
        return self._single_rsvp(r.json())

    async def sweep_expired_calls(self, event_id: str) -> int:
        """Expire overdue calls and auto-call the next in line. Idempotent.

        There is no scheduler in this backend, so expiry is lazy: every read and
        write of the queue runs this first, which is what keeps a no-show from
        blocking everyone behind them.
        """
        r = await self._client.post(
            f"{REST_PREFIX}/rpc/rsvp_sweep_expired_calls",
            json={"p_event_id": event_id},
            headers=self._admin_headers(),
        )
        await self._raise_for_rpc(r)
        val = r.json()
        return val if isinstance(val, int) else 0

    async def rsvp_join(self, event_id: str, user_id: str) -> dict[str, Any] | None:
        """Confirm a seat, or append to the waitlist when the event is full."""
        return await self._call_rsvp_rpc(
            "rsvp_join", {"p_event_id": event_id, "p_user_id": user_id}
        )

    async def rsvp_cancel(self, event_id: str, user_id: str) -> dict[str, Any] | None:
        """Drop out; a freed seat promotes the head of the waitlist."""
        return await self._call_rsvp_rpc(
            "rsvp_cancel", {"p_event_id": event_id, "p_user_id": user_id}
        )

    async def rsvp_call_next(
        self, event_id: str, creator_id: str
    ) -> dict[str, Any] | None:
        """Call the first person in the queue. None when the queue is empty."""
        return await self._call_rsvp_rpc(
            "rsvp_call_next", {"p_event_id": event_id, "p_creator_id": creator_id}
        )

    async def rsvp_recall(
        self, event_id: str, rsvp_id: str, creator_id: str
    ) -> dict[str, Any] | None:
        """Give a no-show a fresh call window (they turned up late)."""
        return await self._call_rsvp_rpc(
            "rsvp_recall",
            {"p_event_id": event_id, "p_rsvp_id": rsvp_id, "p_creator_id": creator_id},
        )

    async def rsvp_skip(
        self, event_id: str, rsvp_id: str, creator_id: str
    ) -> dict[str, Any] | None:
        """Pass the turn of someone already called (#5): `no_show`, and the next in
        line is called when the event calls automatically."""
        return await self._call_rsvp_rpc(
            "rsvp_skip",
            {"p_event_id": event_id, "p_rsvp_id": rsvp_id, "p_creator_id": creator_id},
        )

    async def rsvp_admit(
        self, rsvp_id: str, event_id: str, jti: str
    ) -> dict[str, Any] | None:
        """Burn the QR nonce and mark the attendee admitted, atomically."""
        return await self._call_rsvp_rpc(
            "rsvp_admit",
            {"p_rsvp_id": rsvp_id, "p_event_id": event_id, "p_jti": jti},
        )

    async def get_rsvp(
        self, event_id: str, user_id: str, jwt: str
    ) -> dict[str, Any] | None:
        """Return this user's RSVP for the event, or None if never joined."""
        r = await self._client.get(
            f"{REST_PREFIX}/event_rsvps",
            params={
                "event_id": f"eq.{event_id}",
                "user_id": f"eq.{user_id}",
                "select": "*",
                "limit": "1",
            },
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if isinstance(rows, list) and rows:
            return rows[0]
        return None

    async def get_event_rsvps(
        self, event_id: str, jwt: str
    ) -> list[dict[str, Any]]:
        """Every RSVP of an event with the attendee profile, queue order first."""
        r = await self._client.get(
            f"{REST_PREFIX}/event_rsvps",
            params={
                "event_id": f"eq.{event_id}",
                "select": "*,profiles(id,name)",
                "order": "waitlist_position.asc.nullslast,created_at.asc",
            },
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        return rows if isinstance(rows, list) else []

    async def count_rsvps_by_status(
        self, event_id: str, statuses: list[str], jwt: str
    ) -> int:
        """Exact count without pulling the rows (PostgREST Prefer: count=exact)."""
        if not statuses:
            return 0
        joined = ",".join(statuses)
        r = await self._client.get(
            f"{REST_PREFIX}/event_rsvps",
            params={
                "event_id": f"eq.{event_id}",
                "status": f"in.({joined})",
                "select": "id",
            },
            headers={
                **self._user_headers(jwt),
                "Prefer": "count=exact",
                "Range-Unit": "items",
                "Range": "0-0",
            },
        )
        await self._raise_for_supabase(r)
        content_range = r.headers.get("content-range", "")
        total = content_range.rsplit("/", 1)[-1]
        return int(total) if total.isdigit() else 0

    async def count_waitlist_ahead(
        self, event_id: str, position: int, jwt: str
    ) -> int:
        """How many people are still queued ahead of `position`."""
        r = await self._client.get(
            f"{REST_PREFIX}/event_rsvps",
            params={
                "event_id": f"eq.{event_id}",
                "status": "eq.waitlisted",
                "waitlist_position": f"lt.{position}",
                "select": "id",
            },
            headers={
                **self._user_headers(jwt),
                "Prefer": "count=exact",
                "Range-Unit": "items",
                "Range": "0-0",
            },
        )
        await self._raise_for_supabase(r)
        content_range = r.headers.get("content-range", "")
        total = content_range.rsplit("/", 1)[-1]
        return int(total) if total.isdigit() else 0

    async def get_rsvp_by_id(
        self, rsvp_id: str, event_id: str, jwt: str
    ) -> dict[str, Any] | None:
        """Single RSVP scoped to its event (so an id alone cannot cross events)."""
        r = await self._client.get(
            f"{REST_PREFIX}/event_rsvps",
            params={
                "id": f"eq.{rsvp_id}",
                "event_id": f"eq.{event_id}",
                "select": "*",
                "limit": "1",
            },
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if isinstance(rows, list) and rows:
            return rows[0]
        return None

    async def get_rsvp_profile(
        self, user_id: str, jwt: str
    ) -> dict[str, Any] | None:
        """Public profile of an attendee, to name them on the scanner screen."""
        r = await self._client.get(
            f"{REST_PREFIX}/profiles",
            params={"id": f"eq.{user_id}", "select": "id,name", "limit": "1"},
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if isinstance(rows, list) and rows:
            return rows[0]
        return None
