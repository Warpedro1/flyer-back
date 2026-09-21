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

## Rodar o Flyer inteiro (backend + frontend)

O Flyer são **dois processos**: esta API e o frontend (`Flyer/`). O browser abre o
frontend; é o frontend que fala com esta API. Abrir `http://127.0.0.1:8000`
diretamente só mostra o health check.

**Terminal 1 — backend (este repositório):**

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip install -r requirements.txt
cp .env.example .env                        # e preencher
.venv/bin/uvicorn app.main:app --reload     # Windows: .venv\Scripts\uvicorn app.main:app --reload
```

**Terminal 2 — frontend (`Flyer/`):**

```bash
npm install
npm run dev
```

Depois abrir **http://localhost:5173**. O `VITE_API_BASE_URL` no `.env` do
frontend tem de apontar para `http://127.0.0.1:8000`.

Confirmar que o backend está de pé antes de abrir a app:

```bash
curl http://127.0.0.1:8000/health      # {"status":"ok","chat_model":"gpt-4o"}
```

Se a app abre mas todos os pedidos falham com 500, o backend está a arrancar bem
mas não chega ao Supabase — verificar `SUPABASE_URL` / `SUPABASE_KEY` e o acesso
de rede ao projeto Supabase.

## Health endpoints

- `GET /`
- `GET /health`

## Notes

- Keep `.env` out of Git (already expected in `.gitignore`).
- Do not expose service-role keys on frontend/client code.
- This backend is designed to use async endpoints and service layers to keep route handlers thin.
