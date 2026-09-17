# Vekro Backend

Backend service for the Vekro escrow and dispute-resolution platform.

## Local Setup

1. Create a virtual environment:
   python -m venv .venv
2. Activate it:
   source .venv/bin/activate
3. Install dependencies:
   pip install -r requirements.txt
4. Create environment file:
   cp .env.example .env
5. Update .env with your Neon, Daraja, and Africa's Talking credentials.

## Environment Variables

The committed .env.example includes placeholders for:

- App runtime settings
- Neon runtime and Alembic database URLs
- Daraja M-Pesa credentials and callback URL
- Africa's Talking credentials
- CORS/frontend settings

Minimum required variables for startup validation are:

- DATABASE_URL
- ALEMBIC_DATABASE_URL

## Project Structure

The baseline MVC-style layout is:

```text
app/
   __init__.py
   models/
      __init__.py
   schemas/
      __init__.py
   routers/
      __init__.py
   services/
      __init__.py
   core/
      __init__.py
   db/
      __init__.py
```

## Run (after app entrypoint is added)

uvicorn app.main:app --reload

## API

- GET /health
   - Purpose: service health check.
   - Expected response: 200 OK with JSON payload {"status": "ok"}.

## Repository Hygiene

- .gitignore excludes virtual envs, caches, local DB files, and IDE files.
- .env.example is committed with non-secret placeholders.
- Dependencies are tracked in requirements.txt.

## Progress Log

- 2026-09-17: Milestone 0 Issue [M0] Add repository hygiene files started.
- 2026-09-17: Milestone 0 Issue [M0] Configure Neon Postgres environment settings started with full .env template expansion and local credential wiring.
- 2026-09-17: Milestone 0 Issue [M0] Initialize MVC folder structure completed.
- 2026-09-17: Milestone 0 Issue [M0] Create FastAPI entrypoint and health route completed.
- 2026-09-17: Milestone 0 Issue [M0] Configure Neon Postgres environment settings completed with startup env validation.
