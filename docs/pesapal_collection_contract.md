# Pesapal Collection Contract (Milestone 15)

Status: approved for implementation (2026-10-03)

## Scope Decision

- Pesapal is integrated as a collection rail only.
- No Pesapal payout rail is implemented.
- No Pesapal custody provider implementation is planned in this milestone.
- IntaSend is removed from Milestone 15 scope and must not be reintroduced as a placeholder in milestone planning notes.

## Contract Summary

This integration follows Pesapal v3 sandbox contract semantics.

1. Authentication token
- Endpoint: `POST /api/Auth/RequestToken`
- Request body:
  - `consumer_key`
  - `consumer_secret`
- Response fields used defensively:
  - `token` (primary)
  - `access_token` (fallback)
  - `expiryDate` or `expires_in` (fallback parsing)
- Runtime cache behavior:
  - Token validity is treated as a 5-minute window when provider expiry metadata is absent or malformed.
  - Token refresh is proactive when less than 60 seconds remain.

2. IPN registration (one-off admin action)
- Endpoint: `POST /api/Transactions/RegisterIPN`
- Purpose: register callback URL and capture reusable `ipn_id`.
- Operational rule: registration is an admin or CLI action, not a runtime request path for each checkout.

3. Collection order submit
- Endpoint: `POST /api/Transactions/SubmitOrderRequest`
- Expected integration outputs:
  - `order_tracking_id` (persisted as provider reference)
  - `redirect_url` (returned for buyer-hosted checkout handoff)

4. Callback trigger and finality confirmation
- Callback endpoint in this codebase: `/api/webhooks/pesapal/callback`
- Callback payload is treated as a trigger only.
- Final funding state is determined server-side via status inquiry:
  - Endpoint: `GET /api/Transactions/GetTransactionStatus`
- Rule: callback payload alone must never mark funding success.

## Safety and Honesty Constraints

- Collection and payout rail priorities stay explicit and separately configured.
- Pesapal remains collection-only while payout rail priority remains `loop,simulated`.
- All ambiguous or malformed provider responses must classify as `UNKNOWN` until inquiry confirms terminal state.

## Approval Checkpoint

Approval checkpoint for Milestone 15 scope completed before adapter code merge on 2026-10-03.