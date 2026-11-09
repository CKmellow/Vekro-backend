# Vekro Backend

Backend service for the Vekro escrow and dispute-resolution platform.

## Local Setup

1. Create and activate a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create and edit environment file:

```bash
cp .env.example .env
```

4. Update .env with your Neon, Daraja, and Africa's Talking credentials.

## Environment Variables

The committed .env.example includes placeholders for:

- App runtime settings
- Neon runtime and Alembic database URLs
- Daraja M-Pesa credentials and callback URL
- Africa's Talking credentials
- CORS/frontend settings
- Session and CSRF cookie settings for server-side auth sessions
- Custody mode, rail priority, and provider credential settings

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

SMS delivery settings:

- AFRICASTALKING_USERNAME: defaults to `sandbox` and uses the Africa's Talking sandbox endpoint.
- AFRICASTALKING_API_KEY: blank disables SMS delivery; notifications remain persisted with `delivery_status=not_attempted`.
- AFRICASTALKING_SENDER_ID: optional registered sender ID/shortcode.
- RUN_AFRICASTALKING_LIVE_TESTS and AFRICASTALKING_LIVE_DESTINATION_PHONE: opt-in pytest smoke controls only.

SMS delivery behavior:

- Notification SMS is attempted after the database commit, using a separate session so persistence is not blocked by provider errors.
- Africa's Talking per-recipient `statusCode` drives delivery classification; HTTP 2xx alone is not treated as success.
- Transient provider/HTTP/transport errors are retried with bounded backoff; invalid numbers and auth/sender errors are definite failures.
- Sandbox credentials are for contract and smoke checks. They do not guarantee real handset delivery without Africa's Talking-approved sender/recipient setup or a configured sandbox/test number; live credentials may send real billable SMS.

Login abuse protection settings:

- LOGIN_RATE_LIMIT_MAX_ATTEMPTS: max login attempts per client+phone within window
- LOGIN_RATE_LIMIT_WINDOW_SECONDS: rolling window length for login rate limiting
- LOGIN_LOCKOUT_MAX_ATTEMPTS: failed password attempts before account lockout
- LOGIN_LOCKOUT_SECONDS: temporary lockout duration

Custody and rail settings:

- CUSTODY_MODE: selects custody tier mode (tier_1 or tier_2)
- CUSTODY_COLLECTION_RAIL_PRIORITY: comma-separated collection rail order
- CUSTODY_PAYOUT_RAIL_PRIORITY: comma-separated payout rail order
- Routing examples referenced in project planning docs: `COLLECTION_RAIL_PRIORITY=loop,pesapal,daraja,simulated` and `PAYOUT_RAIL_PRIORITY=loop,simulated`.
- Runtime variable names in this backend are `CUSTODY_COLLECTION_RAIL_PRIORITY` and `CUSTODY_PAYOUT_RAIL_PRIORITY`.
- ALLOW_LIVE_PAYOUTS: live payout guard flag (requires production)
- PAYMENT_CALLBACK_SECRET: shared secret for `POST /transactions/payment-callback` (header `X-Payment-Callback-Secret`); without it the callback is refused in production
- LOOP_ENABLED / PESAPAL_ENABLED / INTASEND_ENABLED / ECONFIRM_ENABLED: enable provider/rail blocks
- LOOP_*, PESAPAL_*, INTASEND_*, ECONFIRM_*: required credentials when their enabled flag is true
- Pesapal routing rule: include `pesapal` in collection priority only; payout priority remains `loop,simulated`.
- Sandbox key scope note: Pesapal sandbox keys are for non-production smoke and contract verification only.

Production safety checks:

- If ENVIRONMENT=production, startup rejects insecure cookie/CORS settings.
- SESSION_COOKIE_SECURE and CSRF_COOKIE_SECURE must be true.
- FRONTEND_URL and CORS_ORIGINS must use https:// in production.
- Live payouts stay blocked unless ENVIRONMENT=production and ALLOW_LIVE_PAYOUTS=true.

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

## Architecture Overview

The backend follows a layered FastAPI architecture:

- Routers (`app/routers`): HTTP concerns only (request parsing, auth dependencies, response mapping, status codes).
- Services (`app/services`): business rules, workflow/state transitions, and domain validation.
- Schemas (`app/schemas`): request/response contracts and OpenAPI model definitions.
- Models (`app/models`): SQLAlchemy persistence entities, enums, and relational constraints.
- Core (`app/core`): cross-cutting concerns such as settings, hashing, and auth context helpers.
- DB (`app/db`): session factory and database URL normalization.

Custody contract interfaces and provider/rail DTOs are defined under `app/services/custody`.

Custody architecture decisions for provider abstraction and tier semantics are tracked in `docs/adr/custody-abstraction.md`.
Milestone 13 transition-to-ledger mapping is documented in `docs/m13_transition_ledger_mapping.md`.
Milestone 15 Pesapal collection-only contract and scope decision is documented in `docs/pesapal_collection_contract.md`.
Milestone 17 custody mode switching and capability gating guidance is documented in `docs/custody-tiers.md`.

Rail capability signal (Milestone 15):

- `loop`: supports collection and payout.
- `pesapal`: supports collection only (`supports_payout: false` equivalent behavior).
- `simulated`: supports collection and payout for local and test workflows.
- `econfirm`: supports collection and payout adapter contracts; split/partial payouts are explicitly unsupported.

### Service-Layer Principles

- State-machine transitions are implemented in services, never directly in route handlers.
- Route handlers translate service/domain exceptions into deterministic HTTP responses.
- Side effects (notifications, timeout transitions, audit context) are invoked at service transition boundaries.
- Timeout automation is status-gated and idempotent to keep re-runs safe.

## State Machine Overview

High-level escrow workflow:

- `awaiting_payment` -> `locked` -> `out_for_delivery` -> `at_door_pending_inspection`
- Non-serialized happy path: `at_door_pending_inspection` -> `released`
- Serialized review path: `at_door_pending_inspection` -> `hold_24h` -> (`released` or dispute flow)

High-level dispute and admin workflow:

- `hold_24h` -> `disputed_functional` -> `return_in_transit` -> `return_received`
- Seller action leads to `refunded_buyer` or `awaiting_buyer_reconfirmation`
- Buyer reconfirmation can resolve, retry once, then escalate to `escalated_admin_review`
- Admin force-resolve applies terminal outcomes: `resolved_refund`, `resolved_release`, or `resolved_split`

## Custody Model and Limitations

Read this before describing Vekro as "escrow" to anyone.

- **Simulated custody is application-enforced and not regulated.** In the default configuration (`CUSTODY_MODE=tier_2`, simulated rails) no regulated custodian holds buyer funds. "Held in escrow" means the backend's ledger and state machine refuse to release money until the workflow allows it. It is not a trust account, a licensed escrow service, or a safeguarded e-money balance.
- `holds_funds_structurally` is reported by `GET /admin/rails/health`. It is `false` for every Tier 2 setup and must be disclosed in user-facing copy whenever it is false.
- Tier 2 with live rails (LOOP collections and payouts, Pesapal collections) moves real money directly between parties. Custody guarantees are still only as strong as this application, its database, and its operators.
- **eConfirm (Tier 1) is a third-party escrow service, not a Vekro licence.** With `ECONFIRM_ENABLED=true`, eligible new transactions (KES `ECONFIRM_MIN_AMOUNT`–`ECONFIRM_MAX_AMOUNT`, breaker closed, keys configured) are opened as escrows on eConfirm's v1 API, and the buyer funds them through eConfirm's M-Pesa prompt. Vekro makes no claim that eConfirm or Vekro is licensed, regulated, or insured; say "held by eConfirm", not "regulated escrow". The integration is built from observed API behaviour (`docs/econfirm-api-observed.md`) and contract/failure-matrix tests; the live funding and reversal round trip is an opt-in smoke test (below) and is disabled by default.
- Every transaction response carries `custody_provider` (`econfirm` or `simulated`). The UI must show simulated custody as simulated and never present it as money held by a custodian.
- Live payouts are blocked unless `ENVIRONMENT=production` and `ALLOW_LIVE_PAYOUTS=true`.
- The payment callback (`POST /transactions/payment-callback`) advances the state machine. It records escrow funding (a balanced ESCROW_HELD / BUYER_CLEARING ledger posting and `funded_amount`, in the same commit as the lock) **only on the simulated rail outside production**, because no provider confirms the callback. In production it never records funding, so the payout executor's funded-balance guard still refuses to settle a payout for that escrow. With `DARAJA_ENABLED=true` (#224), STK pushes go through the real Daraja API. Safaricom's unsigned callback (`POST /payments/mpesa/callback`) is only a trigger: Vekro sends an STK query for its own prompt, and only `ResultCode` 0 locks the order and posts the funding, in production too (`callback_funding_confirmed`, rail `daraja`). A callback that claims a different amount is refused and audited. A pending or unreachable query leaves the order waiting, and `python -m app.cli confirm-mpesa-payments` settles open prompts from the last day. Otherwise, escrow funding is recorded from collection rails (webhooks confirmed by server-side inquiry), the eConfirm funding poller, or, outside production, the admin simulated force-complete control. The callback never locks an eConfirm transaction: eConfirm funding is confirmed only by polling eConfirm (`run_econfirm_funding_reconciliation`), which posts the funding to the ledger in the same commit as the lock.
- **Every order says how its money was collected** (#257): `collection_rail` (`simulated`, `daraja`, `pesapal`, `econfirm`) and `payment_mode` (`sandbox` when no real money moves: Daraja `DARAJA_ENV=sandbox`, Pesapal's `cybqa` sandbox, simulated; otherwise `live`), recorded at creation and anchored with the funding event. The UI shows sandbox payments as tests and never calls Daraja or Pesapal money third-party escrow.
- **Card payments (Pesapal) are collection, not custody** (#251). Card money collected through Pesapal's hosted checkout lands in the merchant's Pesapal account. Vekro records the order as funded only after its own status query reports COMPLETED for the exact amount, currency and reference; the IPN and the buyer's return are triggers only. Pesapal refunds need merchant approval and Pesapal's manual processing, so a card order's refund is queued for support (`GET /admin/refunds/manual`, anchored `manual_refund_required`), refunded in Pesapal, then completed with `POST /admin/refunds/{attempt_id}/complete` (anchored `manual_refund_completed`; `docs/runbooks.md` RB-11). It is never paid out over M-Pesa. Payouts to sellers stay on the existing payout rails.
- **Daraja collects; it does not hold money in custody.** In the class-demo profile (`DARAJA_ENABLED=true`, M22) the buyer's M-Pesa payment lands in the configured shortcode's account (the sandbox test shortcode 174379 in the demo), and Vekro's ledger records it as held. No third party holds it in escrow. `python -m app.cli payment-rails` prints which rail serves each payment method; `docs/demo-modes.md` has the table per profile.
- The payout executor, reconciliation, the eConfirm funding poller and the other periodic jobs run in one scheduler process (`make scheduler`, M24 B4, `docs/demo-modes.md`); each can still be run by hand (see `docs/runbooks.md`).
- No KYC/AML screening, licensing, chargeback handling, or consumer-protection obligations are implemented. Any production launch needs a regulated partner and legal review.

Operational docs:

- Incident runbooks (rail outage, UNKNOWN payout resolution, reconciliation mismatch): `docs/runbooks.md`
- Capstone demo script (normal flow, failover, unknown outcome, duplicate funding): `docs/demo-script.md`
- Tier switching, capability gating, and the eConfirm funding flow: `docs/custody-tiers.md`
- eConfirm observed API, status values, and HTTP failure mapping: `docs/econfirm-api-observed.md`
- Demo profiles, rail per payment method, tunnel and the Daraja fallback: `docs/demo-modes.md`
- Daraja observed sandbox behaviour: `docs/daraja-observed.md`

eConfirm test layers (all offline unless noted):

- Contract tests replaying the sanitised E1 captures: `tests/test_econfirm_contract.py`
- Shared provider conformance over the real httpx transport: `tests/test_custody_provider_conformance.py`
- Failure matrix (HTTP/network condition × operation → outcome and breaker health): `tests/test_econfirm_failure_matrix.py`
- Funding-flow wiring (eConfirm, simulated fallback, Daraja default): `tests/test_econfirm_funding_flow.py`
- Live smoke, opt-in: `RUN_ECONFIRM_LIVE_TESTS=1 .venv/bin/pytest -m live -k econfirm -s`. Without further variables only `ping` runs. The KES 100 fund-and-reverse round trip moves real money and also needs `ECONFIRM_LIVE_MONEY_CONFIRM=KES100`, `ECONFIRM_LIVE_PAYER_PHONE`, `ECONFIRM_LIVE_RECEIVER_PHONE`, and `ECONFIRM_FALLBACK_EMAIL`.

## Verifiable Escrow Ledger (M21)

Vekro holds the money; a public blockchain holds Vekro to account. Every money fact (ledger
postings, payouts, funding, status changes, dispute steps, admin decisions) is hash-chained per
order in the same DB transaction, batched into keccak Merkle roots, and anchored on an immutable
`AnchorRegistry` on Base (`contracts/`). Buyers and sellers verify their order in the browser
against the chain, with no wallet and no crypto, and sellers' completed sales are publicly
countable. With `ANCHOR_GATE_PAYOUTS=true`, no payout leaves escrow until its authorisation is
anchored and its proof matches the on-chain root, so a database edit cannot move money. The money
itself stays in M-Pesa and eConfirm.

- How it works, threat model and honest limits: `docs/anchoring.md`
- Design decisions and event inventory: `docs/anchoring-design.md`
- Browser verifier contract and golden vectors: `docs/frontend-verification-contract.md`,
  `tests/fixtures/anchoring/vectors.json`
- Runbooks RB-7 to RB-10 (`docs/runbooks.md`) and demo Scenario 6 (`docs/demo-script.md`)

## Local Runbook

### Start API server

```bash
make run
```

Equivalent direct command:

```bash
uvicorn app.main:app --reload
```

### Lint, format, and tests

```bash
make lint
make format
.venv/bin/pytest
make rails-selftest
```

Optional live Pesapal auth verification (opt-in):

```bash
RUN_PESAPAL_LIVE_AUTH_SELFTEST=1 \
PESAPAL_BASE_URL=https://cybqa.pesapal.com/pesapalv3 \
PESAPAL_CONSUMER_KEY=<sandbox-key> \
PESAPAL_CONSUMER_SECRET=<sandbox-secret> \
make rails-selftest
```

One-off Pesapal IPN registration (persists `PESAPAL_IPN_ID` into `.env`):

```bash
make pesapal-register-ipn
```

Opt-in live Pesapal sandbox smoke tests:

```bash
RUN_PESAPAL_LIVE_TESTS=1 \
PESAPAL_ENABLED=true \
PESAPAL_BASE_URL=https://cybqa.pesapal.com/pesapalv3 \
PESAPAL_CONSUMER_KEY=<sandbox-key> \
PESAPAL_CONSUMER_SECRET=<sandbox-secret> \
PESAPAL_CALLBACK_URL=https://<your-ngrok-subdomain>.ngrok-free.app/api/webhooks/pesapal/callback \
.venv/bin/pytest -m live -k pesapal -q
```

Opt-in Africa's Talking SMS smoke tests:

```bash
RUN_AFRICASTALKING_LIVE_TESTS=1 \
AFRICASTALKING_USERNAME=sandbox \
AFRICASTALKING_API_KEY=<sandbox-key> \
AFRICASTALKING_LIVE_DESTINATION_PHONE=+2547XXXXXXXX \
.venv/bin/pytest -m live -k africastalking -q
```

Use an Africa's Talking-approved sandbox/test number for sandbox credentials. If you replace
`AFRICASTALKING_USERNAME=sandbox` with a live username, the smoke test may send a real SMS.

### Migration workflow

Create a migration revision:

```bash
.venv/bin/alembic revision -m "describe change"
```

Apply migrations:

```bash
.venv/bin/alembic upgrade head
```

Check migration status:

```bash
.venv/bin/alembic current
```

Migration notes:

- Runtime URL uses `DATABASE_URL`.
- Alembic uses `ALEMBIC_DATABASE_URL`.
- `postgresql://` values are normalized to `postgresql+psycopg://` for sync usage.

## API

- GET /health
   - Purpose: service health check.
   - Expected response: 200 OK with JSON payload {"status": "ok"}.
- POST /auth/register
   - Purpose: register buyer/seller accounts.
   - Validates role (buyer/seller only), phone format, and password length.
   - Persists users with Passlib Argon2 hashing (with legacy PBKDF2 compatibility).
   - Expected response: 201 Created with user profile (no password hash).
- POST /auth/login
   - Purpose: authenticate user and create a server-side session record.
   - Sets two cookies:
      - session cookie (httpOnly) carrying session token.
      - csrf cookie (readable by client) carrying CSRF token.
   - Login is protected by rate limiting and temporary account lockout.
   - Session/CSRF cookie behavior is controlled by env vars in .env.example.
- GET/PATCH /me/profile, PUT/DELETE /me/profile/logo, GET /sellers/{id}/logo
   - Purpose: the seller's shop name, area, description and logo (S3, S13). Logos are stored in the database (JPEG, PNG or WebP, 1 MB).
- POST /me/mpesa-number/request-code and PUT /me/mpesa-number
   - Purpose: change where payouts go. A new number needs the SMS code sent to it once the seller has orders (before the first order no money can move). The previous number gets an SMS that it changed. Codes are scoped by purpose, so a sign-in code can't confirm a payout number.
- POST /auth/password/change, POST /auth/password/reset/request-code, POST /auth/password/reset
   - Purpose: change the password (signs out other devices) or reset it with an SMS code (signs out every device). The reset request always answers 202, so it doesn't reveal which numbers have accounts.
- GET /auth/sessions, DELETE /auth/sessions/{id}, POST /auth/sessions/revoke-others
   - Purpose: signed-in devices (a label from the User-Agent and a last-seen time; no IP addresses), and signing them out.
- GET/PUT /me/notification-preferences
   - Purpose: a seller's in-app and SMS choice per category (payment received, delivery, returns, support). Support decisions always show in the app; buyers can't mute anything (one message carries the delivery code). Honoured by SMS delivery and the in-app list and count.
- GET /seller/analytics?range=7d|30d|90d
   - Purpose: the seller's scoreboard (S12): KPIs against the previous period (sales volume, orders, average order value, completion rate, dispute rate, time to dispatch), sales over time in EAT (weekly at 90 days), outcomes per week, dispute categories and top listings. Only the seller's own paid orders.
- GET /public/orders/{key}
   - Purpose: what an SMS order link shows before the buyer verifies their phone.
   - `key` is the order id or reference; returns reference, item title, amount, a coarse state (unpaid, paid, closed) and the listing status. No names, phones or ids.
   - 30 lookups per minute per client (429 with Retry-After).
- POST /auth/phone/request-code and POST /auth/phone/verify
   - Purpose: passwordless buyer sign-in with a one-time 6-digit SMS code.
   - Codes are random, single-use, expire after 5 minutes, and are stored only as HMAC hashes.
   - A resend waits 30 seconds; at most 5 codes per phone per hour; 5 wrong tries lock the phone for 5 minutes (429 with Retry-After).
   - Verify finds or creates the buyer and sets the same session and CSRF cookies as /auth/login; seller and admin numbers get 409 (password only).
   - PHONE_CODE_DEV_ECHO=true returns the code in the response for local development and e2e; refused in production.
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
- GET /notifications
   - Purpose: authenticated endpoint to fetch notification records for the current user.
   - Returns only notifications belonging to the authenticated user.
   - Supports basic pagination through `limit` and `offset` query parameters.
   - Uses deterministic ordering (newest first) for stable client polling.
   - Expected response: 200 on success, 401 without authentication.
- POST /transactions
   - Purpose: buyer-only endpoint to create escrow transactions from listings.
   - Requires authenticated buyer session and CSRF header for the request.
   - Initializes workflow status to awaiting_payment.
   - Persists listing linkage, buyer/seller linkage, and transaction amount.
   - The amount is always the listing price; an `amount` in the request that differs from the listing price is rejected with 422.
   - Assigns a random, non-sequential `reference` (for example `VK-7K3P9Q`) returned with the transaction.
   - Writes transaction_created notification records for buyer and seller to initialize event timeline/audit context.
   - Expected response: 201 Created with transaction data.
- POST /transactions/payment-callback
   - Purpose: provider callback endpoint to confirm payment and lock a transaction.
   - Authentication (#164): when `PAYMENT_CALLBACK_SECRET` is set, the `X-Payment-Callback-Secret` header must match it (constant-time compare), else 401. With no secret configured the callback is refused (401) in production and open outside production, for the local simulated rail and end-to-end tests.
   - eConfirm transactions are never locked here; their funding is confirmed by polling eConfirm.
   - Valid callback result_code=0 moves status awaiting_payment -> locked and sets locked_at.
   - Duplicate callback for already-locked transactions is idempotent and returns 200 safely.
   - Unsuccessful callback result codes are acknowledged safely with 202 and no transition.
   - Successful lock transition writes transaction_locked notification events for buyer and seller for timeline/audit use.
   - Expected response: 200 on transition or duplicate, 202 when callback is non-successful or state-ineligible, 401 when authentication fails, 404 when transaction is missing.
- GET /api/webhooks/pesapal/callback
   - Purpose: Pesapal callback trigger endpoint for collection confirmation.
   - Callback fields are treated as trigger-only and never trusted as final funding status.
   - Finality is always confirmed via server-side Pesapal status inquiry.
   - Expected response: 200 when callback is accepted and recorded, 503 when Pesapal rail is unavailable.
- POST /api/webhooks/pesapal/callback
   - Purpose: same trigger semantics as GET variant for providers sending JSON payload callbacks.
   - Behavior remains inquiry-first before any funding confirmation.
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
   - Generates a delivery code for buyer confirmation and sends it only to the buyer, in every environment: in the buyer's notification message (also delivered by SMS) and as `payload.delivery_code`. The seller's notification never contains it.
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
- GET /admin/disputes/escalation-queue
   - Purpose: admin-only escalation queue endpoint listing disputes awaiting review.
   - Requires authenticated admin session.
   - Returns only disputes in escalated_admin_review state.
   - Includes triage summary context (dispute, transaction, listing, buyer/seller, reason, description, timestamps).
   - Expected response: 200 on success, 403 for non-admin access, 401 without authentication.
- GET /admin/disputes/{dispute_id}/timeline
   - Purpose: admin-only dispute case timeline detail endpoint.
   - Requires authenticated admin session.
   - Returns dispute context, transaction context, participant ids, and ordered timeline events.
   - Timeline includes dispute lifecycle anchors (opened/escalated/resolved when available) plus notification events for audit detail.
   - Expected response: 200 on success, 404 when dispute case is missing, 403 for non-admin access, 401 without authentication.
- POST /admin/disputes/{dispute_id}/force-resolve
   - Purpose: admin-only endpoint to force-resolve escalated dispute cases.
   - Requires authenticated admin session.
   - Requires `decision` in {refund, release, split} and a non-empty `reason`.
   - Applies decision-specific terminal transitions to both dispute and transaction records and stores admin decision context.
   - Expected response: 200 on success, 422 for invalid state/validation, 404 when dispute case is missing, 403 for non-admin access, 401 without authentication.

## Payment Abstraction

- Payment transport is abstracted behind a service interface in `app/services/payment.py`.
- `DARAJA_ENABLED=false` (default): a stubbed M-Pesa STK gateway for development and tests.
- `DARAJA_ENABLED=true`: `app/services/daraja.py` (OAuth with token reuse, STK push, STK query; sandbox or production by `DARAJA_ENV`). The Daraja webhook maps `CheckoutRequestID` to Vekro's own payment prompt and confirms by STK query before anything changes (`app/services/daraja_confirmation.py`).
- Transaction and state-machine logic stays decoupled from the transport; the callback path is shared.

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

## Runbook Reference

- See Local Runbook for the authoritative local run, lint/test, and migration workflow commands.
- See `docs/runbooks.md` for custody incident procedures and `docs/demo-script.md` for the demo walkthrough.
- Hosting (M26): one VPS with Docker Compose (Postgres, API, scheduler, web app, Caddy for HTTPS), nightly backups and a restore drill. Files in `deploy/` and `Dockerfile`; runbook in `docs/hosting.md`.

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
- 2026-09-24: Milestone 7 Issue [M7] Build admin escalation queue endpoint completed with admin-only access controls, escalated-only filtering, triage summary payload, and tests.
- 2026-09-24: Milestone 7 Issue [M7] Build dispute case timeline detail endpoint completed with admin-only access controls, dispute+transaction context payload, ordered lifecycle/notification timeline events, and tests.
- 2026-09-24: Milestone 7 Issue [M7] Build admin force-resolve endpoint completed with decision+reason validation, decision-driven resolved state transitions, persisted admin decision context, and tests.
- 2026-09-25: Milestone 8 Issue [M8] Hook notifications into key state transitions completed with service-layer notification hooks across transaction creation, delivery, OTP, dispute updates, and timeout sweeps, with test coverage.
- 2026-09-25: Milestone 8 Issue [M8] Add user notifications retrieval endpoint completed with authenticated user scoping, deterministic newest-first ordering, pagination support, and tests.
- 2026-09-25: Milestone 9 Issue [M9] Add automated state-machine transition tests completed with explicit valid-flow assertions, invalid-transition rejection coverage, and critical timeout-transition coverage.
- 2026-09-26: Milestone 9 Issue [M9] Review and refine OpenAPI docs completed with endpoint descriptions, explicit response documentation, enriched schema field metadata, and OpenAPI contract tests.
- 2026-09-26: Milestone 9 Issue [M9] Finalize README architecture and runbooks completed with layered architecture overview, service-layer principles, state-machine high-level guide, and consolidated local run/migration workflows.
- 2026-09-28: Milestone 10 Issue [M10] Write ADR for custody abstraction and tier model completed with Tier 1/Tier 2 semantics, capability flags, honesty disclosure constraints, and outcome taxonomy definitions (SUCCEEDED, FAILED_DEFINITE, UNKNOWN).
- 2026-09-28: Milestone 10 Issue [M10] Define custody ports, DTOs, enums, and failure taxonomy completed with protocol contracts (`CustodyProvider`, `CollectionRail`, `PayoutRail`), custody capability metadata, and enum/serialization contract tests.
- 2026-09-28: Milestone 10 Issue [M10] Add custody config, startup validation, and provider/rail registry completed with custody env matrix keys, fail-fast rail/credential validation, and live payout production guard enforcement.
- 2026-09-28: Milestone 10 Issue [M10] Implement shared phone normalization utility and integrate auth paths completed with canonical Kenyan format handling (+254...), auth request normalization, and rail-facing request phone normalization coverage.
- 2026-09-29: Milestone 11 Issue [M11] Add migration for escrows, ledger entries, and transaction payout_status completed with additive schema expansion, Numeric(12,2) money column mappings, and default NOT_REQUIRED payout status backfill for existing transactions.
- 2026-09-29: Milestone 11 Issue [M11] Add migration for collection/payout attempts, provider events, and rail health completed with partial unique payout-attempt guardrails, provider event dedupe indexing, and JSONB request/response snapshot persistence conventions.
- 2026-09-29: Milestone 11 Issue [M11] Implement EscrowLedger balanced posting service completed with enforced debit/credit pair posting, idempotent no-op replay by idempotency_key, and escrow-held balance query support for orchestration logic.
- 2026-09-29: Milestone 11 Issue [M11] Add invariant/property-style tests for ledger safety completed with randomized journal-balance validation, escrow-held lifecycle invariant checks through payout completion, append-only row growth assertions, and duplicate-post idempotency preservation of row counts/totals.
- 2026-09-30: Milestone 12 Issue [M12] Implement SimulatedRail with deterministic scenario injection completed with configurable default scenarios, trigger-prefix-driven deterministic test injection, and taxonomy-aligned SUCCEEDED/FAILED_DEFINITE/UNKNOWN outcomes across collection and payout APIs.
- 2026-09-30: Milestone 12 Issue [M12] Implement SimulatedCustodyProvider composing ledger and rails completed with open/fund/release/refund/status methods, non-structural capability reporting, and auditable collection/payout attempt persistence backed by EscrowLedger + SimulatedRail.
- 2026-09-30: Milestone 12 Issue [M12] Add dev-only admin controls for simulated custody demos completed with admin-protected simulation endpoints, production-environment guardrails, and idempotent deterministic progression controls that persist attempt-table audit state.
- 2026-09-30: Milestone 12 Issue [M12] Add full-flow simulated custody integration tests completed with offline payment-callback-through-resolution scenarios, deterministic simulated provider funding/release/refund verification, and escalated admin decision payout-intent assertions across refund/release/split outcomes.
- 2026-10-02: Milestone 14 Issue [M14] Implement LOOP token manager and signing helper with self-test completed with pre-expiry token refresh cache behavior, token endpoint variant fallback support, deterministic HMAC signing vector validation, and `make rails-selftest` failure-on-mismatch enforcement.
- 2026-10-02: Milestone 14 Issue [M14] Implement LoopCollectionRail prompt and inquiry adapters completed with body-`statusCode` outcome classification, inquiry `finalState` terminal/pending handling, malformed-response UNKNOWN fallback, and redacted warning logs.
- 2026-10-02: Milestone 14 Issue [M14] Implement LOOP collection webhook handler with idempotent processing completed with callback signature verification, transaction-reference dedupe storage in provider_events, inquiry-first funding confirmation, and redacted payload/signature-validity audit snapshots.
- 2026-10-02: Milestone 14 Issue [M14] Implement LoopPayoutRail and contract/live-sandbox tests completed with deterministic retry provider references plus fresh auth signatures, duplicate-response-as-success idempotent handling, fixture-based success/decline/unknown coverage under tests/fixtures/loop, and opt-in `pytest -m live` smoke checks.
- 2026-10-03: Milestone 15 Issue [M15] Add Pesapal fixtures/tests and collection routing docs completed with fixture-driven auth/submit-order/get-status/callback contracts under tests/fixtures/pesapal, opt-in `pytest -m live -k pesapal` smoke gating, `.env.example` Pesapal variable templates, and explicit collection-only routing/capability documentation (`PAYOUT_RAIL_PRIORITY` remains `loop,simulated`).
- 2026-10-14: Milestone 16 Issue [#129] Stacked PR merge-order closeout completed: PR #126, PR #127, and PR #128 are merged; issue #87 and issue #88 closure/linkage verified; Milestone 16 board cards for issues #86-#88 remain in Done; issue #129 now tracks the final project-board/linked-PR closeout.
- 2026-10-07: Milestone 17 Issue [#89] Add disabled-by-default eConfirm custody adapter completed with mocked HTTP open/fund/status/release/reversal contracts, explicit no-split capability signaling, and registry/settings wiring behind `ECONFIRM_ENABLED`.
- 2026-10-07: Milestone 17 Issue [#90] Add reusable provider conformance suite completed with shared open/fund/release/refund/idempotency checks and taxonomy assertions (`SUCCEEDED`, `FAILED_DEFINITE`, `UNKNOWN`) for both simulated and eConfirm adapters.
- 2026-10-07: Milestone 17 Issue [#91] Add custody tier mode-switching documentation and capability-aware split gating completed with deterministic unsupported split rejection behavior mapped to HTTP 409.
- 2026-10-09: Milestone 18 Issue [#92] Enforce payout execution guardrails completed with fail-closed live payout gating for eConfirm/Loop rails, non-production-only simulated payout progression controls, and OpenAPI security assertions that no arbitrary user-triggered payout execution endpoint exists.
- 2026-10-10: Milestone 18 Issue [#93] Webhook authenticity hardening completed: LOOP callbacks with missing/invalid signatures or timestamps outside a 5-minute window are rejected with 401 and audited in `provider_events` under a separate `rejected:` key space (forged callbacks cannot pre-occupy a legitimate dedupe key), replays are deduped and logged, and snapshots/logs store only a 12-char signature fingerprint plus redacted error messages. Pesapal callbacks remain unsigned by provider design; authenticity is enforced by mandatory server-side status inquiry.
- 2026-10-10: Milestone 18 Issue [#94] Money-movement audit trail completed with a `money_audit_events` table (migration `f3a91c7d2b10`), audit records for payout queueing/execution outcomes, confirmed webhook funding, admin force-resolve decisions, and admin simulated-custody controls (who/what/when/reason plus transaction, escrow, attempt, provider reference, rail, amount), `money_audit` log lines carrying a `txn=...|ref=...|rail=...` correlation ID, and an admin-only `GET /admin/audit/money-events` query endpoint with transaction/reference/rail/action filters.
- 2026-10-10: Milestone 18 Issue [#95] Custody honesty and runbooks completed with a README "Custody Model and Limitations" section (simulated custody is application-enforced and non-regulated; Tier 1 eConfirm is mocked and disabled by default), incident runbooks in `docs/runbooks.md` (rail outage, UNKNOWN payout resolution, reconciliation mismatch), and `docs/demo-script.md` covering normal flow, failover, unknown outcome, and duplicate funding.
- 2026-10-11: Milestone 19 Issue [#117] `AfricasTalkingSmsSender` completed in `app/services/sms/africastalking.py`: config-driven from `AFRICASTALKING_USERNAME` / `AFRICASTALKING_API_KEY` / optional `AFRICASTALKING_SENDER_ID` (sandbox endpoint when username is `sandbox`), recipients normalized with the shared phone utility, per-recipient outcomes (`sent`, `failed_definite`, `failed_transient`) from Africa's Talking `statusCode` so HTTP 2xx alone is never treated as success, and bounded retry (3 attempts, 0.5s/1.0s backoff) that resends only transient recipients after 5xx/429/transport errors or provider status 500-502.
- 2026-10-12: Milestone 19 Issue [#118] SMS delivery wired into the notification lifecycle: additive migration `a7c4e2d91f35` adds `notifications.delivery_status` (`not_attempted`, `sent`, `failed`), session hooks on the app session factory send each committed notification by SMS via `AfricasTalkingSmsSender` to the recipient's phone after commit (on a separate session, so row persistence is never blocked and delivery errors never propagate), `delivered_at` is set on success, status stays `not_attempted` when `AFRICASTALKING_API_KEY` is blank, and `GET /notifications` now returns `delivery_status`.
- 2026-10-13: Milestone 19 Issue [#119] Africa's Talking SMS contract coverage completed with fixture-backed send-response tests under `tests/fixtures/africastalking`, a skipped-by-default `pytest -m live -k africastalking` smoke test gated by `RUN_AFRICASTALKING_LIVE_TESTS`, `.env.example` smoke variable notes, and README documentation for post-commit SMS delivery plus sandbox/test-number limitations.
- 2026-10-12: Issue [#139] Frontend integration fixes: `POST /transactions` now always charges the listing price and rejects a mismatched client `amount` with 422 (`amount` is optional); the delivery code is sent only to the buyer in every environment (notification message, SMS, `payload.delivery_code`) and never to the seller; transactions get a random non-sequential `reference` such as `VK-7K3P9Q` (migration `b8d15e3f7a42`, unique, backfilled for existing rows) returned in `TransactionResponse`.
- 2026-10-17: Milestone 20 E1–E3 (#146–#148): eConfirm discovery probe with sanitised fixtures and `docs/econfirm-api-observed.md`; real `httpx` transport with a browser User-Agent (Cloudflare), request ids, log redaction, and breaker classification (infra failures vs 4xx validation); adapter aligned to the real v1 API (`POST /transactions`, `POST /payments/stk-push`, `GET /transactions/{id}`, exact status strings).
- 2026-10-18: Milestone 20 E4 (#149): eConfirm release/reversal with the one-time `confirmation_code` stored Fernet-encrypted (`ECONFIRM_CODE_ENCRYPTION_KEY`, rotatable, startup guard) and decrypted only for the payout call; additive escrow custody columns.
- 2026-10-19: Milestone 20 E5 (#150): honest custody selection (enabled, keys, breaker, amount band), sticky `escrows.custody_provider`, open-on-eConfirm with UNKNOWN → reconciliation and definite rejection → simulated fallback, and the budgeted funding poller.
- 2026-10-20: Milestone 20 E7 (#156): custody selection wired into `POST /transactions` behind `ECONFIRM_ENABLED`. eConfirm transactions get an eConfirm escrow and STK prompt and lock only when the poller confirms funding; the Daraja callback ignores them. `transactions.custody_provider` (migration `e2b7c4d91f08`, chained after the payment-prompt migration `b5e8f1c2a904`) is returned in `TransactionResponse` and `TransactionDetailResponse`. eConfirm transactions record payment prompts like the gateway path, and resend re-prompts through eConfirm.
- 2026-10-20: Milestone 20 E6 (#151): fixture contract tests, shared conformance over the real transport, the eConfirm failure matrix, the opt-in live smoke (`pytest -m live -k econfirm`, KES 100 round trip behind an extra confirmation variable), and custody-honesty updates to this README, the demo script, the custody-tier doc, and the runbooks.
- 2026-10-20: Issue [#164] `POST /transactions/payment-callback` is authenticated: with `PAYMENT_CALLBACK_SECRET` set, the `X-Payment-Callback-Secret` header must match (constant-time) or the request is rejected with 401 before the transaction is read; with no secret it is refused in production and stays open outside production for the simulated rail and frontend end-to-end tests.
- 2026-11-08: M22 #239 Daraja sandbox round trip: STK `AccountReference` is the order reference; `payment-rails` command and startup log state the collection rail per payment method (`docs/demo-modes.md`); `DARAJA_CALLBACK_URL` must be https; `make tunnel` (ngrok static domain) and `make confirm-mpesa`; opt-in `pytest -m live -k daraja`; `docs/daraja-observed.md` (CheckoutRequestID embeds the payer's number, so logs mask it; the sandbox STK query answers HTTP 429 often).
- 2026-11-08: M22 #241 provider references stay private: `money_audit_events`, `correlation_id`, log lines and every anchored payload carry the keyed `provider_ref` (HMAC with the `ANCHOR_ADMIN_REF_SECRET` keys) instead of raw provider references (Daraja's CheckoutRequestID ends with the payer's number); webhook dedupe keys as `dedupe_ref`; simulated ledger idempotency keys use a digest; the TRANSACTION_LOCKED notifications no longer carry provider identifiers; admin search by the raw reference still works. A suite-wide guard (`tests/pii_guard.py`) fails any test whose anchored payload contains a phone number or e-mail. Pre-#241 dev rows: reset (`docs/anchoring.md` 4a).
- 2026-11-08: M22 #243 Daraja STK query backoff: the callback stays the primary path; `confirm-mpesa-payments --watch [SECONDS]` (`make confirm-mpesa-watch`) keeps confirming open prompts, each queried about 5s after it is first seen and then at doubling intervals up to 30s with jitter, never two queries within 2s; a 429 or pending answer only backs off. The opt-in live test now funds one order per run by `DARAJA_LIVE_MODE=callback` (no test-side queries) or `fallback` (the watcher).
- 2026-11-08: M22 #245 Daraja STK query: only known final codes (1032, 1037, 2001, 1) fail a payment; any other `ResultCode` stays pending and is logged, since nothing re-checks a failed prompt. Resend cooldown 60s (Daraja refuses a second prompt to the same phone within a minute: "a similar transaction being underway"). The watcher leaves prompts older than 10 minutes alone. Live run: a KES 1 order funded by the callback (`tests/fixtures/daraja/stk_callback_paid.json`).
- 2026-11-08: M22 #247 Daraja live fixtures: the fallback watcher funded a KES 1 order with the tunnel stopped (429, backoff, then `ResultCode` 0); push-accepted, query-paid and rate-limited (`SpikeArrestViolation`, 30/min, burst 3) bodies captured and replayed in tests. Both payment paths are now proven live.
- 2026-11-08: M22 #249 Pesapal discovery (`docs/pesapal-observed.md`, `scripts/pesapal_probe.py`, `tests/fixtures/pesapal/observed/`): the hosted page offers Visa/Mastercard/Amex with published sandbox test cards. Adapter fixes: IPN registration uses `/api/URLSetup/RegisterIPN` (the old path answered 404); status uses the documented `status_code` (INVALID / "Pending Payment" and unknown codes are pending, not failed) and returns the paid amount and currency; no undocumented `reference` field; no missing-IPN warning from a disabled rail.
- 2026-11-08: M22 #251 card payments through Pesapal hosted checkout: `POST /transactions` takes `payment_method` (`mpesa` default, `card`); card answers a `REDIRECT` instruction (422 when not configured) and records `transactions.payment_method` (migration c3f1a8d2e6b4). `/payments/pesapal/ipn` (GET/POST, Pesapal's documented answer) and `POST /transactions/{id}/payment/refresh` are triggers; funding only after the status query reports COMPLETED for the exact amount, currency and reference; mismatches are refused and audited (`card_payment_mismatch`), declined cards close the checkout (`card_payment_failed`, reason `declined`); all anchored. A card order keeps its open checkout on resend. `confirm-card-payments` command; `payment-rails` shows card. Opt-in live smoke.
- 2026-11-08: M22 #253 manual card refunds: a refund of a card-funded order is queued to `manual_card_refund` (the payout executor never sends it) with an anchored `manual_refund_required`; `GET /admin/refunds/manual` and `POST /admin/refunds/{attempt_id}/complete` (settles through the executor's own path, `settle_succeeded_payout`, respects the anchor-before-payout gate, anchors `manual_refund_completed`; idempotent); runbook RB-11. `PESAPAL_CALLBACK_URL` is now required only when Pesapal is a custody collection rail.
- 2026-11-08: M22 #255 the card return URL is `FRONTEND_URL/pay/return?order=<id>&ref=<reference>`, so the return page can read the order and call `payment/refresh`; the result always comes from the API.
- 2026-11-08: M22 #257 `transactions.collection_rail` and `payment_mode` (migration d7e2b5c9a1f3 with backfill from eConfirm custody, card orders and Daraja checkout ids), set at creation (and on eConfirm fallback), exposed on the API, and anchored in the funding events' details (`callback_funding_recorded`, `callback_funding_confirmed`, `econfirm_funding_confirmed`).
- 2026-11-08: M24 #259 Merkle sum tree for proof of liabilities (`app/services/anchoring/sum_tree.py`): salted leaves commit to a buyer-recomputable order commitment and an amount in cents; nodes commit to both children and their sum (uint64, range-checked); golden vectors `tests/fixtures/anchoring/sum_tree_vectors.json` with must-fail cases (tampered amount, omitted escrow, understated total, negative or overflowing sums, wrong side).
- 2026-11-08: M24 #261 liabilities snapshot: `liabilities_snapshots` / `liabilities_leaves` (migration e5a9c3d1f7b2), `python -m app.cli liabilities-snapshot` anchors the root and total held as a global `liabilities_snapshot` event (no per-order data), `GET /transactions/{id}/liabilities-proof` (buyer or admin) serves the leaf, salt, sum proof and anchored event; `docs/anchoring.md` 4b says what it proves and what it does not.
- 2026-11-08: M23 #263 USSD for buyers and sellers: `POST /ussd/africastalking/{USSD_CALLBACK_TOKEN}` (form-encoded, `python-multipart` pinned; optional `USSD_ALLOWED_IPS`), screens of at most 160 characters, sessions in `ussd_sessions` (migration f1b8d4e6a2c9), a hashed USSD PIN with its own lockout (`POST /account/ussd-pin`), a stale-menu guard, and every action through the existing services; discovery in `docs/africastalking-ussd-observed.md` with fixtures.
- 2026-11-09: M23 #265 `python -m app.cli seed-ussd-demo` (demo buyer and seller on the simulator numbers, USSD PIN, orders waiting for payment, paid, on the way, at the door and in the 24-hour hold; never in production); USSD "My orders" lists the orders that need the caller first; `docs/ussd.md` (setup, menu tree, security limits, type +254 in the simulator) and demo Scenario 7.
- 2026-11-09: M24 #269 reserves reconciliation: `python -m app.cli reserves-reconcile` anchors, per custody provider, liability against attested total (`reserves_reconciliation`; eConfirm per-escrow status, Daraja and Pesapal unavailable, simulated not applicable); a shortfall is an anchored `reserves_shortfall` audit event and shows on `GET /admin/anchoring/status` with the latest liabilities snapshot; `docs/anchoring.md` 4c (provider answers are unsigned, so Vekro could misreport them), runbook RB-12. #267: CI installs Foundry from the pinned, checksum-verified release with retries.
- 2026-11-09: M24 #271 anchoring coverage: tests that card funding, card failure and mismatch, manual refunds, USSD actions and Daraja funding are anchored and that the payout gate holds card-funded releases; the full audit now rebuilds each liabilities snapshot from its stored leaves (`liabilities_mismatch`) and checks each reserves result (`reserves_inconsistent`).
- 2026-11-09: M24 #273 scheduler: `python -m app.jobs.scheduler` (`make scheduler`) runs the anchoring worker, payouts, eConfirm poller, M-Pesa and card confirmation, timeouts, liabilities snapshot and reserves reconciliation, each on its own interval and only when configured, under a PostgreSQL advisory lock per job; `DEMO_FAST_TIMEOUTS` (refused in production) shortens the state windows to minutes for a class demo.

## Operator commands

- `VEKRO_ADMIN_PASSWORD=... python -m app.cli create-admin --phone 07XXXXXXXX --name "Vekro Support"`: create a support admin, or promote an existing account. The password (12+ characters) comes from the environment, never a flag.
- `python -m app.cli repair-return-cases [--apply]`: list return cases that the timeout jobs left stuck before #221 (the transaction escalated or released while its dispute stayed open). Without `--apply`, nothing changes. With it, each case is escalated or closed and recorded as `return_case_repaired` in the money audit. Since #227 it also escalates functional issues reported as `other` whose transaction stayed `disputed_functional` (`escalate_transaction`), and lists any such case the timeout already released for review. Any other mismatch is listed for review and never changed. See `docs/runbooks.md` RB-5.
- `python -m app.cli repair-funding-ledger [--apply]`: list funded escrows whose funding is missing from the ledger (the eConfirm poller set `funded_amount` without a posting before #222). Without `--apply`, nothing changes. With it, the missing ESCROW_HELD / BUYER_CLEARING movement is posted and recorded as `funding_ledger_repaired` in the money audit. An escrow with more on the ledger than `funded_amount` is listed for review and never changed. See `docs/runbooks.md` RB-6.
- `python -m app.cli anchor-backfill [--resume]`: create anchor events for history written before M21 (ledger rows, money audit rows, transactions), in original order and marked `"backfilled": true`, plus one snapshot of each transaction's state. Run it once, right after the M21 migration. **History before the backfill is attested only from the moment of the backfill.** See `docs/runbooks.md` RB-7 and `docs/anchoring-design.md`.
- `python -m app.jobs.anchoring run-once|run-loop` (M21 A5): batches committed anchor events (gapless `seq`, keccak RFC 6962 Merkle root) and anchors each batch on the AnchorRegistry (`contracts/`). There is one batch in flight at a time. The signed transaction is recorded before it is broadcast, the registry is checked before every submission, stuck transactions get a fee bump at the same nonce, and a confirmed batch that a reorg drops is resubmitted. When the chain holds a different root, the worker HALTS (exit code 3) and records an `anchoring_halted` audit event. It needs `ANCHOR_ENABLED=true` and the chain settings in `.env.example`. `GET /admin/anchoring/status` shows progress, never the key. An unreachable RPC never affects escrows: events wait for the next run.
- Verification (M21 A6): `GET /transactions/{id}/verification` (buyer, seller or admin) returns the order's events with canonical payloads, salts, hashes and Merkle proofs, plus the live status and amount. It returns data only, no verdict. `GET /anchoring/batches/{id}` is public and rate-limited. `scripts/verify_transaction.py` reads each root from the registry and prints VERIFIED, PARTIALLY_PENDING, TAMPERED or UNAVAILABLE. `scripts/audit_anchoring.py` is the operator's full audit from the source tables: batches, seq gaps, chains, source rows, statuses, admin decisions and escrow totals. `scripts/demo_tamper.py` (refuses production, needs `--i-understand-this-corrupts-data`) edits a ledger row, deletes a middle or last event, or changes a status, to show the verifiers catching each one.
- Anchor before payout (M21 A8, `ANCHOR_GATE_PAYOUTS`): the payout executor sends nothing to a rail until the payout's `payout_<kind>_queued` authorisation is in a confirmed batch and its Merkle proof matches the root read from the AnchorRegistry. The authorisation binds the attempt, amount, transaction and an HMAC of the destination phone. A payout edited after it was anchored (amount, destination) or a root that differs on-chain **halts** that payout, with one `payout_<kind>_halted_anchor_mismatch` audit event. An unreadable chain or a reorged batch only makes it wait. `GET /admin/anchoring/status` shows payouts waiting and halted.
- Verified seller track record (M21 A9): each completed sale (`released` or `resolved_release`) records a `seller_sale_completed` event in the global chain. It holds the seller, the outcome, an amount band and an unlinkable HMAC `sale_ref`, with no buyer and no order. `GET /sellers/{id}/track-record` (public, rate-limited) lists them with Merkle proofs, so anyone can count a seller's sales against the AnchorRegistry. The full audit checks every sale against a real completed sale of that seller. `python -m app.cli anchor-sales-backfill` records sales from before A9 (idempotent).
