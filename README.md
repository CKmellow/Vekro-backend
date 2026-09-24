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
- POST /listings
   - Purpose: seller-only endpoint for creating listings.
   - Requires authenticated seller session and CSRF header for the request.
   - Serialized listing rules:
      - unique_id is required when is_serialized is true.
      - dispute_policy.resolution is required when is_serialized is true.
   - Validation failures for serialized listing rules return deterministic detail strings.
   - Expected response: 201 Created with listing data.
- GET /listings/{listing_id}
   - Purpose: public endpoint returning listing details by id.
   - Response includes serialized flags and dispute policy payload.
   - Missing listing ids return deterministic not-found detail.
   - Expected response: 200 OK with listing data, 404 when not found.
- POST /transactions
   - Purpose: buyer-only endpoint to create escrow transactions from listings.
   - Requires authenticated buyer session and CSRF header for the request.
   - Initializes workflow status to awaiting_payment.
   - Persists listing linkage, buyer/seller linkage, and transaction amount.
   - Expected response: 201 Created with transaction data.
- POST /transactions/payment-callback
   - Purpose: provider callback endpoint to confirm payment and lock a transaction.
   - Valid callback result_code=0 moves status awaiting_payment -> locked and sets locked_at.
   - Duplicate callback for already-locked transactions is idempotent and returns 200 safely.
   - Unsuccessful callback result codes are acknowledged safely with 202 and no transition.
   - Successful lock transition writes transaction_locked notification events for buyer and seller for timeline/audit use.
   - Expected response: 200 on transition or duplicate, 202 when callback is non-successful or state-ineligible, 404 when transaction is missing.
- POST /transactions/{transaction_id}/dispatch
   - Purpose: seller action endpoint to dispatch a locked transaction.
   - Requires authenticated seller session and CSRF header for the request.
   - Only the seller attached to the transaction can dispatch it.
   - Enforces state transition locked -> out_for_delivery only.
   - Invalid prior states are rejected with deterministic validation detail.
   - Expected response: 200 on successful dispatch, 403 for non-owner/non-seller access, 422 for invalid prior state, 404 when transaction is missing.
- POST /transactions/{transaction_id}/arrival
   - Purpose: seller action endpoint to mark delivery arrival for inspection handoff.
   - Requires authenticated seller session and CSRF header for the request.
   - Only the seller attached to the transaction can mark arrival.
   - Enforces state transition out_for_delivery -> at_door_pending_inspection and records at_door_at timestamp.
   - Generates a delivery OTP for buyer confirmation; OTP preview is included in notification payload in non-production environments.
   - Invalid prior states are rejected with deterministic validation detail.
   - Expected response: 200 on successful arrival update, 403 for non-owner/non-seller access, 422 for invalid prior state, 404 when transaction is missing.
- POST /transactions/{transaction_id}/otp-give
   - Purpose: buyer action endpoint to confirm OTP handoff after delivery arrival.
   - Requires authenticated buyer session and CSRF header for the request.
   - Correct OTP on non-serialized listings transitions at_door_pending_inspection -> released.
   - Correct OTP on serialized listings transitions at_door_pending_inspection -> hold_24h and persists hold_started_at.
   - Incorrect OTP attempts increment failure counter and return deterministic validation detail.
   - Released transactions are terminal for OTP actions and cannot be reopened through withhold flow.
   - Expected response: 200 on successful release/hold transition, 422 for invalid OTP or invalid state, 403 for unauthorized buyer access, 404 when transaction is missing.
- POST /transactions/{transaction_id}/otp-withhold
   - Purpose: buyer action endpoint to withhold OTP and trigger return-refund resolution path.
   - Requires authenticated buyer session and CSRF header for the request.
   - Transitions at_door_pending_inspection -> return_in_transit -> returned_to_seller -> refunded_buyer.
   - Captures transition history in notifications/audit payload for traceability.
   - Expected response: 200 on successful refund-path transition, 422 for invalid state, 403 for unauthorized buyer access, 404 when transaction is missing.
- POST /transactions/{transaction_id}/report-functional-issue
   - Purpose: buyer endpoint to report a functional issue during the serialized hold window.
   - Requires authenticated buyer session and CSRF header for the request.
   - Allowed only when transaction is in hold_24h and listing is serialized.
   - Category must match one of: not_working, damaged_on_arrival, missing_parts, not_as_described, other.
   - Category other routes dispute directly to admin escalation; all valid reports transition hold_24h -> disputed_functional.
   - Expected response: 200 on successful report transition, 422 for invalid state/category, 403 for unauthorized buyer access, 404 when transaction or listing is missing.
- POST /transactions/{transaction_id}/buyer-sent-back
   - Purpose: buyer endpoint to confirm return shipment in functional-dispute flow.
   - Requires authenticated buyer session and CSRF header for the request.
   - Allowed only when transaction is in disputed_functional.
   - Transitions disputed_functional -> return_in_transit.
   - Expected response: 200 on successful transition, 422 for invalid state, 403 for unauthorized buyer access, 404 when transaction is missing.
- POST /transactions/{transaction_id}/seller-received
   - Purpose: seller endpoint to confirm receipt of returned item in dispute flow.
   - Requires authenticated seller session and CSRF header for the request.
   - Allowed only when transaction is in return_in_transit.
   - Transitions return_in_transit -> return_received.
   - Expected response: 200 on successful transition, 422 for invalid state, 403 for unauthorized seller access, 404 when transaction is missing.
- POST /transactions/{transaction_id}/seller-resolution-action
   - Purpose: seller endpoint to apply dispute resolution actions after return receipt.
   - Requires authenticated seller session and CSRF header for the request.
   - Allowed only when transaction is in return_received.
   - Supported actions: refund_issued, repair_shipped, replacement_shipped.
   - refund_issued transitions to refunded_buyer immediately; repair_shipped/replacement_shipped transition to awaiting_buyer_reconfirmation.
   - Action acceptance is gated by listing dispute_policy allowed_seller_actions when configured.
   - Expected response: 200 on successful transition, 422 for invalid state/action/policy-ineligible action, 403 for unauthorized seller access, 404 when transaction or listing is missing.
- POST /transactions/{transaction_id}/buyer-reconfirmation
   - Purpose: buyer endpoint to reconfirm seller remediation outcome.
   - Requires authenticated buyer session and CSRF header for the request.
   - Allowed only when transaction is in awaiting_buyer_reconfirmation.
   - accepted=true transitions to resolved_release.
   - accepted=false transitions to return_received on first rejection, and escalated_admin_review on second rejection (one-retry cap).
   - Expected response: 200 on successful transition, 422 for invalid state, 403 for unauthorized buyer access, 404 when transaction is missing.
- POST /transactions/{transaction_id}/confirm-resolved-buyer
   - Purpose: buyer mutual-confirmation endpoint for resolved-state closure.
   - Requires authenticated buyer session and CSRF header for the request.
   - Tracks buyer_confirmed_resolved independently and does not allow unilateral closure.
   - Expected response: 200 on successful confirmation update, 422 for invalid state, 403 for unauthorized buyer access, 404 when transaction is missing.
- POST /transactions/{transaction_id}/confirm-resolved-seller
   - Purpose: seller mutual-confirmation endpoint for resolved-state closure.
   - Requires authenticated seller session and CSRF header for the request.
   - Tracks seller_confirmed_resolved independently; closure is only considered complete when both buyer and seller confirmations are true.
   - Expected response: 200 on successful confirmation update, 422 for invalid state, 403 for unauthorized seller access, 404 when transaction is missing.

## Payment Abstraction

- Payment transport is abstracted behind a service interface in `app/services/payment.py`.
- Current implementation uses a stubbed M-Pesa STK gateway for development and milestone testing.
- Transaction/state-machine logic remains decoupled from provider transport and can swap to Daraja integration later.
- Callback transition logic remains provider-agnostic and keyed by transaction id payload while Daraja webhook mapping is pending full transport integration.

## Timeout Jobs

- Timeout execution is implemented in service layer via `run_timeout_jobs` in `app/services/transaction.py`.
- At-door timeout rule (~1 hour): auto-applies withheld OTP refund path when buyer takes no action in at_door_pending_inspection.
- No-dispatch timeout rule (~48 hours from locked): auto-refunds buyer when seller never dispatches.
- Hold auto-release rule (~24 hours from hold_started_at): auto-releases hold_24h transactions to released when no report/dispute transition has occurred.
- Timeout sweeps are idempotent by status-gated eligibility queries so already-transitioned records are skipped on subsequent runs.
- Timeout transitions emit SYSTEM_TIMEOUT notifications with transition history payload for audit traceability.
- Dispute sent-back timeout rule (~3 days from disputed_functional): auto-cancels dispute and releases escrow to seller when buyer never marks sent-back.
- Seller-received timeout rule (~3 days from return_in_transit): auto-escalates dispute to escalated_admin_review when seller never confirms receipt.

## Security Hardening

- Middleware validates server-side sessions on requests with session cookies.
- Session checks enforce not revoked, not expired, and active user status.
- Expired/invalid session cookies are cleared.
- State-changing requests with session cookies require CSRF cookie + header validation.
- Reusable role dependencies are available for buyer/seller/admin route guards.
- Password hashing uses Passlib with Argon2 for new hashes; legacy PBKDF2 hashes are still verified and upgraded after successful login.
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
- 2026-09-19: Milestone 2 Issue [M2] Add password hashing integration completed using Passlib Argon2 with legacy hash upgrade-on-login behavior.
- 2026-09-20: Milestone 3 Issue [M3] Implement seller create-listing endpoint completed with seller-only auth checks, serialized-item validation rules, deterministic validation messages, and endpoint tests.
- 2026-09-20: Milestone 3 Issue [M3] Implement public get-listing endpoint completed with id-based listing retrieval, serialized/dispute-policy response fields, deterministic 404 behavior, and endpoint tests.
- 2026-09-20: Milestone 3 Issue [M3] Implement create-transaction to AWAITING_PAYMENT completed with buyer-only auth checks, participant linkage persistence, awaiting_payment initialization, and endpoint tests.
- 2026-09-20: Milestone 3 Issue [M3] Add M-Pesa STK service interface stub completed with payment gateway abstraction, stubbed STK initiation path, and transport-decoupling tests.
- 2026-09-21: Milestone 4 Issue [M4] Implement payment confirmation to LOCKED completed with callback endpoint, idempotent duplicate handling, safe invalid-callback handling, transition audit logging, tests, and live endpoint verification.
- 2026-09-21: Milestone 4 Issue [M4] Implement seller dispatch to OUT_FOR_DELIVERY completed with seller ownership enforcement, strict locked-to-out_for_delivery transition checks, clear invalid-state errors, tests, and live endpoint verification.
- 2026-09-21: Milestone 4 Issue [M4] Implement delivery arrival to AT_DOOR_PENDING_INSPECTION completed with seller authorization checks, strict out_for_delivery precondition enforcement, at_door_at timestamp capture, tests, and live endpoint verification.
- 2026-09-21: Milestone 4 Issue [M4] Implement non-serialized OTP-give to RELEASED completed with OTP validation, failed-attempt handling, terminal release-state guardrails, and tests.
- 2026-09-21: Milestone 4 Issue [M4] Implement OTP-withhold return-refund path completed with at-door withheld flow, return/refund terminal transition, audit transition-history payload capture, and tests.
- 2026-09-21: Milestone 4 Issue [M4] Implement scheduled timeout jobs (1h and 48h) completed with service-layer sweep logic, timeout-triggered state transitions, audit notifications, and tests.
- 2026-09-22: Milestone 5 Issue [M5] Implement serialized OTP-give to HOLD_24H completed with hold_started_at persistence, non-serialized release path preservation, and tests.
- 2026-09-22: Milestone 5 Issue [M5] Implement 24-hour HOLD_24H auto-release job completed with idempotent eligibility sweep, transition-history notifications, and tests.
- 2026-09-22: Milestone 5 Issue [M5] Implement report-functional-issue endpoint completed with hold-window/serialized gating, category validation with other admin-escalation routing, disputed_functional transition, and tests.
- 2026-09-23: Milestone 6 Issue [M6] Implement buyer sent-back and 3-day auto-cancel completed with dispute sent-back endpoint, timeout-driven auto-release fallback, and tests.
- 2026-09-23: Milestone 6 Issue [M6] Implement seller received and 3-day auto-escalate completed with seller receipt endpoint, timeout-driven admin escalation fallback, and tests.
- 2026-09-23: Milestone 6 Issue [M6] Implement seller resolution action endpoints completed with policy-gated seller actions, refund immediate close path, reconfirmation transitions, and tests.
- 2026-09-23: Milestone 6 Issue [M6] Implement buyer reconfirmation with retry cap completed with accept/retry/escalate paths and tests.
- 2026-09-23: Milestone 6 Issue [M6] Implement mutual-confirmation resolution gating completed with independent buyer/seller confirmation flags and bilateral closure enforcement.
