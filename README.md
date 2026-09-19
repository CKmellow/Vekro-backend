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
- Session and CSRF cookie settings for server-side auth sessions

Minimum required variables for startup validation are:

- DATABASE_URL
- ALEMBIC_DATABASE_URL

Session/CSRF auth cookie settings used by login/logout:

- SESSION_COOKIE_NAME: session cookie key (default vekro_session)
- SESSION_COOKIE_SECURE: secure flag for session cookie
- SESSION_COOKIE_SAMESITE: one of lax, strict, none
- SESSION_COOKIE_MAX_AGE_SECONDS: session TTL in seconds
- CSRF_COOKIE_NAME: csrf cookie key (default vekro_csrf)
- CSRF_COOKIE_SECURE: secure flag for csrf cookie
- CSRF_COOKIE_SAMESITE: one of lax, strict, none
- CSRF_HEADER_NAME: header name clients must send for CSRF checks (default X-CSRF-Token)
- CORS_ALLOW_CREDENTIALS: must be true for browser credentialed requests

Login abuse protection settings:

- LOGIN_RATE_LIMIT_MAX_ATTEMPTS: max login attempts per client+phone within window
- LOGIN_RATE_LIMIT_WINDOW_SECONDS: rolling window length for login rate limiting
- LOGIN_LOCKOUT_MAX_ATTEMPTS: failed password attempts before account lockout
- LOGIN_LOCKOUT_SECONDS: temporary lockout duration

Production safety checks:

- If ENVIRONMENT=production, startup rejects insecure cookie/CORS settings.
- SESSION_COOKIE_SECURE and CSRF_COOKIE_SECURE must be true.
- FRONTEND_URL and CORS_ORIGINS must use https:// in production.

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
- POST /auth/register
   - Purpose: register buyer/seller accounts.
   - Validates role (buyer/seller only), phone format, and password length.
   - Persists users with PBKDF2-SHA256 password hashing.
   - Expected response: 201 Created with user profile (no password hash).
- POST /auth/login
   - Purpose: authenticate user and create a server-side session record.
   - Sets two cookies:
      - session cookie (httpOnly) carrying session token.
      - csrf cookie (readable by client) carrying CSRF token.
   - Login is protected by rate limiting and temporary account lockout.
   - Session/CSRF cookie behavior is controlled by env vars in .env.example.
   - Expected response: 200 OK with user profile and session expiration timestamp.
- POST /auth/logout
   - Purpose: invalidate active server-side session if present.
   - If a session cookie is present, request must include CSRF header matching the CSRF cookie.
   - Clears session and csrf cookies from the client.
   - Expected response: 204 No Content.
- GET /auth/me
   - Purpose: return the currently authenticated user from validated server-side session.
   - Expected response: 200 OK when session is valid, otherwise 401.
- GET /protected/buyer
   - Purpose: buyer-only role-protected sample endpoint.
   - Expected response: 200 for buyer, 403 for other authenticated roles, 401 without auth.
- GET /protected/seller
   - Purpose: seller-only role-protected sample endpoint.
   - Expected response: 200 for seller, 403 for other authenticated roles, 401 without auth.
- GET /protected/admin
   - Purpose: admin-only role-protected sample endpoint.
   - Expected response: 200 for admin, 403 for other authenticated roles, 401 without auth.

## Security Hardening

- Middleware validates server-side sessions on requests with session cookies.
- Session checks enforce not revoked, not expired, and active user status.
- Expired/invalid session cookies are cleared.
- State-changing requests with session cookies require CSRF cookie + header validation.
- Reusable role dependencies are available for buyer/seller/admin route guards.
- Security headers are added to all responses:
   - X-Content-Type-Options: nosniff
   - X-Frame-Options: DENY
   - Referrer-Policy: no-referrer
   - Content-Security-Policy: default-src 'none'; frame-ancestors 'none'; base-uri 'none'
   - Permissions-Policy: camera=(), microphone=(), geolocation=()
   - Strict-Transport-Security in production

## Migrations

Alembic is initialized in the repository root.

Run migrations with:

1. .venv/bin/alembic revision -m "your message"
2. .venv/bin/alembic upgrade head
3. .venv/bin/alembic current

Notes:

- Runtime URL uses DATABASE_URL.
- Alembic URL uses ALEMBIC_DATABASE_URL.
- If ALEMBIC_DATABASE_URL is provided as postgresql://..., the migration env normalizes it to postgresql+psycopg://...

## Development Tooling

Configured tools:

- Ruff for linting
- Black for formatting

Commands:

1. make run
2. make lint
3. make format

## Repository Hygiene

- .gitignore excludes virtual envs, caches, local DB files, and IDE files.
- .env.example is committed with non-secret placeholders.
- Dependencies are tracked in requirements.txt.

## Milestone 1 Schema Notes

- Listing dispute policy is stored as JSONB in the listings table.
- Explicit enums are used for role and workflow/event columns:
   user_role, transaction_status, dispute_type, dispute_status,
   seller_resolution_action, admin_decision, notification_event_type,
   notification_channel.
- Foreign keys across core entities:
   listings.seller_id -> users.id;
   transactions.listing_id -> listings.id;
   transactions.buyer_id/seller_id -> users.id;
   disputes.transaction_id -> transactions.id;
   disputes.opened_by_user_id -> users.id;
   notifications.user_id -> users.id;
   notifications.transaction_id -> transactions.id.

## Progress Log

- 2026-09-17: Milestone 0 Issue [M0] Add repository hygiene files started.
- 2026-09-17: Milestone 0 Issue [M0] Configure Neon Postgres environment settings started with full .env template expansion and local credential wiring.
- 2026-09-17: Milestone 0 Issue [M0] Initialize MVC folder structure completed.
- 2026-09-17: Milestone 0 Issue [M0] Create FastAPI entrypoint and health route completed.
- 2026-09-17: Milestone 0 Issue [M0] Configure Neon Postgres environment settings completed with startup env validation.
- 2026-09-17: Milestone 0 Issue [M0] Set up Alembic initialization and empty migration completed.
- 2026-09-17: Milestone 0 Issue [M0] Add lint and format tooling completed.
- 2026-09-18: Milestone 1 Issue [M1] Create User model and migration completed.
- 2026-09-18: Milestone 1 Issue [M1] Create Listing model and migration completed with JSONB dispute policy, seller FK, and validation constraints.
- 2026-09-18: Milestone 1 Issue [M1] Create Transaction model and migration completed with lifecycle status enum and foreign keys to listing, buyer, and seller.
- 2026-09-18: Milestone 1 Issue [M1] Create Dispute model and migration completed with dispute enums, transaction linkage, and flow support fields.
- 2026-09-18: Milestone 1 Issue [M1] Create Notification model and migration completed with user/transaction foreign keys and event payload support.
- 2026-09-18: Milestone 1 Issue [M1] Define explicit enums and FK constraints completed with live Neon schema audit.
- 2026-09-18: Milestone 2 Issue [M2] Implement buyer and seller registration endpoint completed with secure password hashing and endpoint tests.
- 2026-09-19: Milestone 2 Issue [M2] Build login and logout endpoints with server-side sessions completed with migration, tests, and live verification.
- 2026-09-19: Security hardening patch completed with CSRF enforcement, login abuse protection, session-validation middleware, production secure-cookie guardrails, auth audit logging, and API security headers.
- 2026-09-19: Milestone 2 Issue [M2] Add role-protected route dependencies completed with buyer/seller/admin test routes and endpoint verification.
