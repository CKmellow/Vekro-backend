# Custody Tiers and Mode Switching

This document defines how custody mode changes affect rail/provider routing without changing the escrow or dispute state machine.

## Goal

- Keep transaction/dispute statuses unchanged across tier switches.
- Move custody behavior through configuration (`CUSTODY_MODE`, rail priorities, enable flags).
- Enforce deterministic capability checks for operations that depend on provider support.

## Tier Summary

| Mode | Operational model | Provider expectation | Split decision handling |
| --- | --- | --- | --- |
| `tier_1` | Provider-led custody of funds | `holds_funds_structurally=true` | Split requires provider capability (`supports_split_payout` or `supports_partial_release`) |
| `tier_2` | App-led custody + ledger orchestration | `holds_funds_structurally=false` | Split allowed when provider declares support, or when routed through simulated non-structural payout rail |

## Deterministic Split Behavior

Split decisions are capability-gated in service logic before payout intents are created.

- If provider capabilities include split or partial release support, split is allowed.
- If provider has structural holds and lacks split/partial support, split is rejected.
- Rejection raises a split-unsupported service error that maps to HTTP 409 in admin force-resolve.

This keeps unsupported split attempts explicit and deterministic while preserving existing state-machine transitions.

## eConfirm Tier-1 Adapter Notes

- The eConfirm custody adapter is disabled by default (`ECONFIRM_ENABLED=false`).
- When enabled in `tier_1`, it exposes:
  - `holds_funds_structurally=true`
  - `supports_split_payout=false`
  - `supports_partial_release=false`
- This capability profile intentionally blocks split payouts with deterministic 409 responses until provider-side split capability exists.

## eConfirm funding flow (M20)

eConfirm is selected **per transaction** at `POST /transactions`, independently
of `CUSTODY_MODE`. With `ECONFIRM_ENABLED=false` (the default) nothing below
runs and the existing payment path is unchanged.

1. **Select.** `select_custody_provider` picks `econfirm` only when eConfirm is
   enabled, `ECONFIRM_API_KEY` and a valid `ECONFIRM_CODE_ENCRYPTION_KEY` are
   set, the `econfirm` breaker is closed, and the amount is within
   `ECONFIRM_MIN_AMOUNT`–`ECONFIRM_MAX_AMOUNT`. Otherwise the transaction is
   `simulated` and uses the existing gateway path.
2. **Open.** An escrow row is written first, flagged
   `econfirm_needs_reconciliation`, so a crash mid-call is never mistaken for a
   clean state. The open call then:
   - succeeds: stores eConfirm's id and the encrypted `confirmation_code`; or
   - is a definite rejection (4xx): falls back to `simulated` honestly, because no
     eConfirm call has succeeded yet; or
   - is UNKNOWN: stays on eConfirm, flagged for reconciliation. No STK is sent and
     the API returns `202` with `X-Payment-Initiation-Status: failed`.
3. **Fund.** eConfirm sends the STK prompt (`POST /payments/stk-push`) to the
   buyer's M-Pesa phone, which is stored as `econfirm_payer_phone` for reversals.
   A sent prompt is not funding. After a successful open, the transaction never
   falls back to simulated, even if the STK is rejected.
4. **Confirm.** Only `run_econfirm_funding_reconciliation` confirms funding, by
   `GET /transactions/{id}` reporting `Escrow Funded`. It then sets the escrow's
   funded amount and moves the transaction `awaiting_payment` → `locked` with the
   usual notifications. `POST /transactions/payment-callback` ignores eConfirm
   transactions.
5. **Pay out.** Release and refund intents for an eConfirm escrow always route
   to the `econfirm` payout rail (sticky). They execute only when `econfirm` is in
   `CUSTODY_PAYOUT_RAIL_PRIORITY` and live payouts are allowed
   (`ENVIRONMENT=production`, `ALLOW_LIVE_PAYOUTS=true`); otherwise the attempt
   stays UNKNOWN (`rail_unavailable`) until they are.

The frontend contract is unchanged. The create response's `payment` instruction
is an `stk_prompt` whose `checkout_request_id` is eConfirm's transaction id. A
failed open or a rejected STK reads `failed_to_initiate`, and the poller marks
the prompt `succeeded` when it locks the transaction.
`POST /transactions/{id}/payment/resend` re-prompts through eConfirm, never the
Daraja gateway. It is refused (422) while the escrow awaits reconciliation.
`custody_provider` on `TransactionResponse` and `TransactionDetailResponse`
tells the UI whether the money is held by eConfirm or simulated.
