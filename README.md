# FlyerBack

Backend API for Flyer, built with FastAPI and integrated with Supabase, OpenAI, and Pusher.

## Tech stack

- Python
- FastAPI
- Pydantic v2
- httpx
- Supabase (REST / PostgREST)
- SlowAPI (rate limiting)

## Project structure

```text
FlyerBack/
  app/
    api/        # API routes
    core/       # config, security, geo, limiter
    models/     # Pydantic schemas
    services/   # business logic and external integrations
    main.py     # FastAPI entrypoint
  migrations/   # SQL migrations
  requirements.txt
  .env.example
```

## Requirements

- Python 3.11+ recommended
- Supabase project credentials
- Optional: OpenAI key for AI features
- Optional: Pusher credentials for realtime features

## Setup

1. Create and activate a virtual environment:

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create your local environment file:

```bash
cp .env.example .env
```

On Windows PowerShell, if `cp` is unavailable:

```powershell
Copy-Item .env.example .env
```

4. Fill `.env` with your real values.

## Environment variables

Main variables used by the app:

- `SUPABASE_URL`
- `SUPABASE_KEY`
- `SUPABASE_JWT_SECRET`
- `OPENAI_API_KEY` or `OPENAI_KEY`
- `PUSHER_APP_ID`
- `PUSHER_KEY`
- `PUSHER_SECRET`
- `PUSHER_CLUSTER`
- `CHAT_MODEL`
- `EMBEDDING_MODEL`

Use `.env.example` as the full reference.

## Run locally

```bash
uvicorn app.main:app --reload
```

Default local URL: `http://127.0.0.1:8000`

## Health endpoints

- `GET /`
- `GET /health`

## Notes

- Keep `.env` out of Git (already expected in `.gitignore`).
- Do not expose service-role keys on frontend/client code.
- This backend is designed to use async endpoints and service layers to keep route handlers thin.
