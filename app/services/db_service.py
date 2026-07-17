"""Supabase PostgREST access via shared httpx.AsyncClient (singleton from app lifespan)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.core.config import settings
from app.models.schemas import ChatSender, EventMediaRead, FollowStatus, MediaType

REST_PREFIX = "/rest/v1"
STORAGE_PREFIX = "/storage/v1"

POSTGRES_UNIQUE_VIOLATION = "23505"


class UniqueViolationError(Exception):
    """Raised when PostgreSQL reports unique constraint violation (code 23505)."""


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


class DBService:
    """Thin async wrapper around Supabase REST. Inject shared httpx client from lifespan."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    def _user_headers(self, jwt: str) -> dict[str, str]:  # noqa: ARG002
        """Build PostgREST headers using the service_role key.

        The backend already validates the user JWT (ES256 via JWKS) before
        any DB call, so we use the service_role key here to bypass RLS.
        The ``jwt`` parameter is kept in the signature for API stability.
        """
        return self._admin_headers()

    def _admin_headers(self) -> dict[str, str]:
        token = settings.SUPABASE_KEY
        return {
            **_json_headers(),
            "apikey": token,
            "Authorization": f"Bearer {token}",
        }

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

    async def get_or_create_chat(self, user_id: str, jwt: str) -> str:
        """Return the single ai_chats.id for this user (UNIQUE user_id)."""
        r = await self._client.get(
            f"{REST_PREFIX}/ai_chats",
            params={"user_id": f"eq.{user_id}", "select": "id"},
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if isinstance(rows, list) and rows:
            row_id = rows[0].get("id")
            if row_id:
                return str(row_id)

        ins = await self._client.post(
            f"{REST_PREFIX}/ai_chats",
            json={"user_id": user_id},
            headers={
                **self._user_headers(jwt),
                "Prefer": "return=representation",
            },
        )
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

    async def increment_interest_sync_count(self, user_id: str, jwt: str) -> int:
        """Atomically increment via RPC when migration is applied; otherwise R-M-W fallback."""
        r = await self._client.post(
            f"{REST_PREFIX}/rpc/increment_interest_sync_count",
            json={"p_user_id": user_id},
            headers=self._admin_headers(),
        )
        if r.status_code == 404:
            return await self._increment_interest_sync_count_fallback(user_id, jwt)
        await self._raise_for_supabase(r)
        val = r.json()
        if isinstance(val, int):
            return val
        if isinstance(val, str) and val.lstrip("-").isdigit():
            return int(val)
        return -1

    async def _increment_interest_sync_count_fallback(self, user_id: str, jwt: str) -> int:
        """Compare-and-swap increment when the RPC is unavailable.

        PostgREST cannot express ``col = col + 1``, so we read the value and PATCH
        only the row whose count is still ``cur`` (filtered update), retrying when a
        concurrent writer wins the race. This avoids the lost-update bug of a plain
        read-modify-write under concurrent syncs.
        """
        for _ in range(5):
            prof = await self.get_profile(user_id, jwt)
            if prof is None:
                return -1
            cur = int(prof.get("interest_sync_count_after_onboarding") or 0)
            nxt = cur + 1
            r = await self._client.patch(
                f"{REST_PREFIX}/profiles",
                params={
                    "id": f"eq.{user_id}",
                    "interest_sync_count_after_onboarding": f"eq.{cur}",
                },
                json={"interest_sync_count_after_onboarding": nxt},
                headers={**self._user_headers(jwt), "Prefer": "return=representation"},
            )
            await self._raise_for_supabase(r)
            updated = r.json()
            if isinstance(updated, list) and updated:
                return nxt
            # Another writer advanced the counter between read and write; retry.
        return -1

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
        query_embedding: list[float],
        user_lat: float,
        user_lng: float,
        radius_km: float,
    ) -> list[dict[str, Any]]:
        r = await self._client.post(
            f"{REST_PREFIX}/rpc/match_events",
            json={
                "query_embedding": query_embedding,
                "user_lat": user_lat,
                "user_lng": user_lng,
                "radius_km": radius_km,
            },
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

    async def get_event_coords(self, event_id: str, jwt: str) -> tuple[float, float] | None:
        r = await self._client.get(
            f"{REST_PREFIX}/events",
            params={"id": f"eq.{event_id}", "select": "lat,long"},
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if not (isinstance(rows, list) and rows):
            return None
        row = rows[0]
        lat = row.get("lat")
        lng = row.get("long")
        if lat is None or lng is None:
            return None
        return float(lat), float(lng)

    async def get_trophy_template_by_event(self, event_id: str) -> str | None:
        r = await self._client.get(
            f"{REST_PREFIX}/trophy_templates",
            params={
                "event_id": f"eq.{event_id}",
                "select": "id",
                "limit": "1",
            },
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if isinstance(rows, list) and rows:
            tid = rows[0].get("id")
            if tid:
                return str(tid)
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
            params={"id": f"in.({joined})", "select": "id,name,email"},
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
                    "follower_email": prof.get("email"),
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
                "email": profiles.get(pid, {}).get("email"),
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
                "email": profiles.get(pid, {}).get("email"),
            }
            for pid in ids
        ]

    async def get_trophy_template_full(self, template_id: str) -> dict[str, Any] | None:
        r = await self._client.get(
            f"{REST_PREFIX}/trophy_templates",
            params={"id": f"eq.{template_id}", "select": "*"},
            headers=self._admin_headers(),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if isinstance(rows, list) and rows:
            return rows[0]
        return None

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
        r = await self._client.post(
            f"{REST_PREFIX}/event_media",
            json=rows,
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)

    async def get_event_by_id(
        self,
        event_id: str,
        jwt: str,
    ) -> dict[str, Any] | None:
        """Return a single event row or None."""
        r = await self._client.get(
            f"{REST_PREFIX}/events",
            params={"id": f"eq.{event_id}", "select": "*", "limit": "1"},
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        if isinstance(rows, list) and rows:
            return rows[0]
        return None

    async def get_events_by_creator(
        self,
        creator_id: str,
        jwt: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return events created by a specific user."""
        r = await self._client.get(
            f"{REST_PREFIX}/events",
            params={
                "creator_id": f"eq.{creator_id}",
                "select": "*",
                "order": "created_at.desc",
                "limit": str(limit),
                "offset": str(offset),
            },
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        return rows if isinstance(rows, list) else []

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
                "select": "id,template_id,acquired_at,trophy_templates(name,description,icon_url)",
                "order": "acquired_at.desc",
            },
            headers=self._user_headers(jwt),
        )
        await self._raise_for_supabase(r)
        rows = r.json()
        return rows if isinstance(rows, list) else []
