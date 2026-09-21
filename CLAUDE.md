# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

This is a two-project monorepo for **Flyer**, an event-recommendation and social-gamification app. The two projects are independent git repositories with their own `.env`, dependencies, and tooling:

- `Flyer/` — React 18 + TypeScript + Vite frontend (Tailwind CSS v4).
- `FlyerBack/` — FastAPI (Python 3.11+) backend, talking to Supabase, OpenAI, and Pusher.

Cursor rules in `.cursor/rules/{frontend,backend}.mdc` define the canonical conventions; they are authored in Portuguese and the key constraints are summarized below.

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
- Dependencies: `pip install -r requirements.txt` inside `.venv`.

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
- **RLS pass-through vs. admin.** For user-scoped data, forward the user's JWT to Supabase so Row Level Security applies. For global/system operations (e.g. listing plans), use the `service_role` admin header. Never expose the service-role key to clients.
- **Async everywhere.** Use `async`/`await` and explicit return type annotations on all endpoints and service methods.
- **Pydantic.** Use `ConfigDict(extra="forbid")`. Money/score fields use `decimal.Decimal`; dates use `datetime`.
- **Rate limiting.** SlowAPI is wired globally in `main.py`; apply `@limiter.limit(...)` decorators to mutation endpoints (POST/PUT).
- **Onboarding pipeline.** `onboarding_pipeline.py` orchestrates guard → taste extraction → embedding → profile → Chroma upsert. AI/embedding behavior and the Chroma vector store are optional and gated on env config.

## Frontend architecture (`Flyer/src/`)

- **Routing** in `App.tsx`: `/login` is standalone; all other pages render inside `AppLayout` (sidebar on desktop, bottom-nav on mobile).
- **API access.** All backend calls go through `services/flyerApi.ts`, which uses the pre-configured Axios instance in `lib/apiClient.ts`. The request interceptor pulls the Supabase session and injects `Bearer <access_token>`. Note: the response interceptor deliberately does **not** sign out on 401 (a backend JWT-check failure should not eject a user with a valid Supabase session).
- **Auth/state.** Supabase (`lib/supabase.ts` + `@supabase/supabase-js`) manages session/JWT, exposed through `contexts/AuthProvider.tsx` + `hooks/useAuth.ts`. No global state library (Redux/Zustand/etc.) — this is prohibited.
- **Types.** `types/index.ts` must mirror the backend Pydantic schemas exactly. Decimal fields (money, ratings) are typed as `string | null` on the UI side.
- **Styling.** Tailwind CSS v4 only (via `@tailwindcss/vite`). Do not add Bootstrap, MUI, or any prebuilt component library. Accent color is `red-600`; prefer `rounded-2xl`/`rounded-3xl`. Root container is `h-screen overflow-hidden`; only `<main>` scrolls (`overflow-y-auto`).

### Frontend conventions (from `.cursor/rules/frontend.mdc`)
- **TDD:** write/update the `*.test.tsx` (or `*.test.ts`) file before changing a component or hook.
- Tests use Vitest + `@testing-library/react` + `userEvent`. Test behavior, not internal state. **Never make real API calls** — intercept with MSW (`src/mocks/handlers.ts`, `src/mocks/server.ts`).
- TypeScript strict mode; `any` is forbidden. Functional components only, `PascalCase` filenames. Always write `useEffect` cleanup functions for listeners/timeouts/subscriptions.

## Environment

Both projects require their own `.env` (copy from each `.env.example`). The frontend needs at least `VITE_API_BASE_URL` (`apiClient.ts` throws on startup if missing) plus Supabase keys. The backend needs `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_JWT_SECRET`, and optionally `OPENAI_API_KEY`/`OPENAI_KEY`, Pusher (`PUSHER_*`), `CHAT_MODEL`, and `EMBEDDING_MODEL`. SQL migrations live in `FlyerBack/migrations/`.
