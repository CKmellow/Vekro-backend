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
