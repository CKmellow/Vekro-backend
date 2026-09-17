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
5. Update .env with your Neon database credentials.

## Run (after app entrypoint is added)

uvicorn app.main:app --reload

## Repository Hygiene

- .gitignore excludes virtual envs, caches, local DB files, and IDE files.
- .env.example is committed with non-secret placeholders.
- Dependencies are tracked in requirements.txt.

## Progress Log

- 2026-09-17: Milestone 0 Issue [M0] Add repository hygiene files started.
