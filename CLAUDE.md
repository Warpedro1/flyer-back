# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

This is a two-project monorepo for **Flyer**, an event-recommendation and social-gamification app. The two projects are independent git repositories with their own `.env`, dependencies, and tooling:

- `Flyer/` — React 18 + TypeScript + Vite frontend (Tailwind CSS v4).
- `FlyerBack/` — FastAPI (Python 3.11+) backend, talking to Supabase, OpenAI, and Pusher.

The conventions below are canonical for both projects.

## Commands

### Frontend (`Flyer/`)
- `npm run dev` — Vite dev server on port 5173 (auto-opens browser).
- `npm run build` — type-check (`tsc -b`) then production build to `dist/`.
- `npm run lint` — ESLint over the project.
- `npm run test` — Vitest (jsdom). Run a single file: `npx vitest run src/pages/ChatPage.test.tsx`. Single test: add `-t "test name"`.
- `npm run test:ui` — Vitest UI runner.

### Backend (`FlyerBack/`)
- `uvicorn app.main:app --reload` — run the API locally (default `http://127.0.0.1:8000`).
- `pytest` — run tests (`asyncio_mode = auto`, `testpaths = tests`). Single file: `pytest tests/test_onboarding_pipeline.py`. Single test: `pytest tests/test_onboarding_guard.py::test_name`.
- Dependencies: `pip install -r requirements-dev.txt` inside `.venv`. `requirements.txt` is production-only (what Vercel installs into the function, under a 250 MB limit); the dev file adds `uvicorn` and `pytest`.

Health checks: `GET /` and `GET /health` (the latter echoes the active `CHAT_MODEL`).

## Backend architecture (`FlyerBack/app/`)

Strict layering — keep route handlers thin:
- `api/` — routers, one file per domain with a URL prefix: `chat` (`/chat`), `events` (`/events`), `plans` (`/plans`), `social` (`/social`), `trophies` (`/trophies`). All registered in `main.py`.
- `services/` — business logic and external integrations (`db_service.py`, `ai_service.py`, the `onboarding_*` pipeline, taste extraction, Chroma vector store).
- `models/schemas.py` — Pydantic v2 schemas.
- `core/` — `config.py` (pydantic-settings), `security.py` (JWT), `geo.py`, `limiter.py`.

Key cross-cutting patterns:
- **No ORM.** Supabase is accessed exclusively via the PostgREST REST API using `httpx`. A single shared `httpx.AsyncClient` is created in the FastAPI `lifespan` (`main.py`) and injected via `HttpClientDep` / `DbServiceDep` (`api/deps.py`) — never create per-request clients.
- **Auth.** `decode_access_token` (`core/security.py`) validates Supabase JWTs: ES256/RS256/EdDSA via JWKS first, falling back to HS256 with `SUPABASE_JWT_SECRET`. Routes get the user via the `get_current_user` / `get_current_user_with_token` dependencies.
- **Authorization lives in the backend, not in RLS** (issue #7, option B). Every PostgREST call runs with the `service_role` key — `DBService._user_headers` does not forward the user's JWT — so Row Level Security never applies on the API path. Clients have no privileges on `public` tables, sequences or functions (migration `20261009000000_revoke_client_table_access`): the publishable key only reaches Auth and Storage. The RLS policies stay in the schema but are unreachable today; do not count on them. New objects created by migrations no longer get `anon`/`authenticated` grants by default, and new functions are not granted to `PUBLIC` — `service_role` keeps its default grants. Never grant a `public` table or function to `anon`/`authenticated` without a design change. Consequences:
  - Any read or write of user-scoped data must filter by the `user_id` from the validated token, or go through `app/api/authz.py` (`require_owner`, `require_event_creator`, `require_media_paths_owned`, `require_place`, `require_trophy_icon`). Add new ownership rules there rather than re-implementing them per route.
  - Every route must depend on `get_current_user` / `get_current_user_with_token`. `tests/test_route_auth.py` walks the route table and fails otherwise; the only exceptions are the public list (`/`, `/health`, `*/health`, `GET /plans/`) and `/admin/*`, which uses a shared secret instead: `X-Admin-Token`, or `X-Apify-Webhook-Secret` on the ingestion webhook.
  - `profiles.is_private` today only decides whether a follow starts as `pending`. The RLS also hides private users' events, trophies and profile data from non-followers, but the backend deliberately does not apply that (no UI sets `is_private` yet); revisit when privacy settings ship.
  - Never expose the service-role key to clients.
- **Async everywhere.** Use `async`/`await` and explicit return type annotations on all endpoints and service methods.
- **Pydantic.** Use `ConfigDict(extra="forbid")`. Money/score fields use `decimal.Decimal`; dates use `datetime`.
- **Rate limiting.** SlowAPI is wired globally in `main.py`; apply `@limiter.limit(...)` decorators to mutation endpoints (POST/PUT). Counters live in the shared store from `RATE_LIMIT_STORAGE_URI` (Upstash Redis; `REDIS_URL`/`KV_URL` also read) so limits hold across serverless instances; without it they fall back to per-instance memory. Requests with a valid token are counted per user, others per client IP (`x-forwarded-for`, which Vercel overwrites) — see `core/limiter.py`.
- **`SECURITY DEFINER` functions.** Revoke `EXECUTE` explicitly from `public`, `anon` and `authenticated`, then grant it to `service_role`. Supabase grants `anon` and `authenticated` through default privileges, separately from `PUBLIC`, so `REVOKE ... FROM PUBLIC` alone leaves the function callable at `/rest/v1/rpc/<name>` with the publishable key. Also pin `search_path`. Check with the Supabase security advisors after applying.
- **Catalogue (events and places).** `events` holds both: `kind = 'event'` (has a date, or was created in the app) and `kind = 'place'` (somewhere permanent to go: restaurant, gym, beach…, no date, `price_level` 1–4 and `opening_hours`). An event may point at its place (`place_id`). `category` is a slug from `EventCategory` in `models/schemas.py`, mirrored by `Flyer/src/utils/categories.ts`; renaming a slug needs a data migration. `price = 0` is free, `price = null` is unknown. Discovery filters (kind, category, date, distance, price) live in the `match_events` SQL function; places always pass the date and price filters.
- **Ingestion (`services/etl/`).** Apify actors defined in `etl/sources.py` (Google Maps places, Sympla events), each with a charge cap. `POST /admin/ingest` only starts the run (scraping outlasts a 60 s function); Apify calls `POST /admin/ingest/apify-webhook` when it ends, which loads the dataset: deterministic transform (`etl/transform.py`, `etl/taxonomy.py`, no LLM, coordinates only from the source), batched embeddings, upsert on `(source, source_event_id)`, then event→place links.
- **Trophies.** Every event gives a trophy at check-in. The creator may customise it in `EventCreate.trophy` (name, description, icon = an image they uploaded to `event-media`); a recurring event gets one `trophy_templates` row per occurrence. Without one, `POST /trophies/checkin` creates the default "Presença: <título>" on the first check-in (`trophy_templates.event_id` is unique, so concurrent check-ins share it). Icons are Storage paths, signed when read; a signing failure gives `icon_url = null`, never the raw path.
- **Check-in window** (`services/checkin_window.py`, decisions 2026-10-12). Events created in the app must have a start and an end (`event_date`, `ends_at`). Check-in opens at `checkin_opens_at` (the start by default, or the creator's scheduled time, or `POST /events/{id}/checkin/open`) and closes at `ends_at`, or earlier with `POST /events/{id}/checkin/close` (`checkin_closed_at`). Imported events open at the start and close at the source's end (start + 4 h when it has none). Places follow their opening hours in Recife time; no readable hours = always open. Times without a timezone are Recife time (`core/tz.py`, fixed UTC−03:00). Discovery hides an event only once it has ended, not when it starts.
- **Editing.** `GET /events/{id}/edit` and `PUT /events/{id}`: creator only, everything but the recurrence (a series is edited one date at a time); imported events and places are not editable. `PUT` replaces the media and always upserts the trophy (the default name follows a new title).
- **Login by e-mail and the second factor** (decisions 2026-10-12). Besides Google, accounts with e-mail and password (Supabase Auth: sign-up confirmed by a 6-digit code, reset by e-mail link). A session opened with a password (`amr` has `password`) only reaches the API after the 6-digit code Supabase e-mails to the account: `deps._authenticated_payload` answers 403 `email_code_required` until `POST /auth/email-code/verify` records its `session_id` in `login_second_factor` (backend-only table). Google, confirmation and recovery sessions pass. The `/auth/email-code/*` routes use `get_current_user_pending_code` (token only); nowhere else may. The code always goes to the e-mail in the token.
- **CPF and phone** (decisions 2026-10-12). Every account has a CPF and a Brazilian mobile, both unique (`profiles.cpf`, `profiles.phone` in E.164; unique indexes and `is_valid_cpf` check in the database too). Sign-up by e-mail goes through the backend, `POST /auth/signup` (public, rate limited): it validates (`core/br_documents.py`), says plainly which of e-mail/CPF/phone is taken, then creates the user in Supabase Auth with them in the metadata (the `handle_new_user` trigger copies them). Accounts without them (Google) get 403 `profile_incomplete` everywhere until `PUT /auth/profile/identity`; the `/auth/profile/*` routes use `get_current_user_pending_profile`. CPF and phone are only ever returned to their owner, masked (`GET /auth/profile/status`); never add them to `ProfileBrief`/`UserRead`.
- **Sign-up code tied to its tab** (decision 2026-10-14). The sign-up e-mail carries only the 6-digit code (template "Confirm signup", no link). Supabase codes can be redeemed with the publishable key, so the code alone must not open the account (a mistyped e-mail would hand someone else an account with your CPF): `POST /auth/signup` answers a secret `ticket` (only its sha256 is stored, in `signup_tickets`), the profile starts `signup_pending` (trigger, `flyer_signup` in the metadata), and `deps._authenticated_payload` answers 403 `signup_pending` until `POST /auth/signup/activate` gets that ticket from a session of the same account. The app keeps the ticket in `sessionStorage` (that tab only). A sign-up whose e-mail/CPF/phone only clash with pending accounts deletes them and goes on; only active accounts give 409. `POST /auth/signup/activate` uses `get_current_user_pending_profile`, like `/auth/profile/*`.
- **Onboarding pipeline.** `onboarding_pipeline.py` orchestrates guard → taste extraction → embedding → profile → Chroma upsert. AI/embedding behavior and the Chroma vector store are optional and gated on env config.

## Frontend architecture (`Flyer/src/`)

- **Routing** in `App.tsx`: `/login` is standalone; all other pages render inside `AppLayout` (sidebar on desktop, bottom-nav on mobile).
- **API access.** All backend calls go through `services/flyerApi.ts`, which uses the pre-configured Axios instance in `lib/apiClient.ts`. The request interceptor pulls the Supabase session and injects `Bearer <access_token>`. Note: the response interceptor deliberately does **not** sign out on 401 (a backend JWT-check failure should not eject a user with a valid Supabase session).
- **Auth/state.** Supabase (`lib/supabase.ts` + `@supabase/supabase-js`) manages session/JWT, exposed through `contexts/AuthProvider.tsx` + `hooks/useAuth.ts`. No global state library (Redux/Zustand/etc.) — this is prohibited.
- **Types.** `types/index.ts` must mirror the backend Pydantic schemas exactly. Decimal fields (money, ratings) are typed as `string | null` on the UI side.
- **Styling.** Tailwind CSS v4 only (via `@tailwindcss/vite`). Do not add Bootstrap, MUI, or any prebuilt component library. Accent color is `red-600`; prefer `rounded-2xl`/`rounded-3xl`. Root container is `h-screen overflow-hidden`; only `<main>` scrolls (`overflow-y-auto`).

### Frontend conventions
- **TDD:** write/update the `*.test.tsx` (or `*.test.ts`) file before changing a component or hook.
- Tests use Vitest + `@testing-library/react` + `userEvent`. Test behavior, not internal state. **Never make real API calls** — intercept with MSW (`src/mocks/handlers.ts`, `src/mocks/server.ts`).
- TypeScript strict mode; `any` is forbidden. Functional components only, `PascalCase` filenames. Always write `useEffect` cleanup functions for listeners/timeouts/subscriptions.

## Environment

Both projects require their own `.env` (copy from each `.env.example`). The frontend needs at least `VITE_API_BASE_URL` (`apiClient.ts` throws on startup if missing) plus Supabase keys. The backend needs `SUPABASE_URL`, `SUPABASE_KEY` (a `sb_secret_...` key, or the legacy `service_role` JWT until Supabase retires it at the end of 2026), `SUPABASE_JWT_SECRET`, and optionally `OPENAI_API_KEY`/`OPENAI_KEY`, Pusher (`PUSHER_*`), `CHAT_MODEL`, and `EMBEDDING_MODEL`. SQL migrations live in `FlyerBack/supabase/migrations/` (timestamped, applied in order); `FlyerBack/migrations/003_create_plans.sql` is legacy.
