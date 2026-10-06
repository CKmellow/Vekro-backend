# Failover Taxonomy and Chaos Coverage

This document defines how payout and collection failures are classified, when automatic failover is allowed, and where chaos behavior is tested.

## Scope

- Payout failover coverage is implemented for `loop` and `simulated` rails.
- Pesapal remains collection-only in this backend milestone and is excluded from payout failover routing.
- Failover behavior is orchestrated in `app/services/escrow_service.py` and breaker state in `app/services/custody/rail_breaker.py`.

## Rail Coverage Matrix

| Rail | Collection | Payout | Auto Failover Participation |
| --- | --- | --- | --- |
| `loop` | Yes | Yes | Yes (primary/alternate) |
| `simulated` | Yes | Yes | Yes (primary/alternate, chaos scenarios) |
| `pesapal` | Yes | No | No payout failover (collection-only) |

## Failure Taxonomy and Actions

| Failure class | Typical signal | Finality confidence | Automatic action | Failover allowed |
| --- | --- | --- | --- | --- |
| Auth/Credential failure | `auth`, `token`, `401`, `403`, `invalid client` | Definite failure on current rail | Mark current attempt `FAILED_DEFINITE`, record breaker failure, queue next rail | Yes |
| Provider definite decline | `failed_definite`, `declined` | Definite failure | Mark `FAILED_DEFINITE`, update payout status | No automatic cross-rail retry |
| Unknown timeout | `timeout` | Uncertain | Keep same provider reference, retry status probe, then exhaust and fail over | Yes (after same-reference retry budget) |
| Out-of-order callbacks | `out_of_order`, `out_of_order_pending` | Uncertain but reconcilable | Keep UNKNOWN and continue reconciliation polling | No |
| Malformed provider response | `malformed_response` | Uncertain and potentially unsafe | Keep UNKNOWN for manual/reconciliation follow-up | No |
| Duplicate confirmed collection | `duplicate`, `duplicate_confirmed` | Potentially over-collected | Queue compensating refund intent + admin metadata flag | Not a payout failover path |
| Rail unavailable at runtime | rail lookup/request error before acceptance | Usually pre-acceptance failure | Mark UNKNOWN (or auth failure if credential-related), route by policy | Conditional |

## Double-Payout Guardrails (Failover Forbidden Cases)

Automatic failover is forbidden when the original payout could still settle and duplication risk is high:

1. Out-of-order callback paths (`out_of_order*`) are reconciled on the same provider reference.
2. Malformed provider responses (`malformed_response`) are kept UNKNOWN for manual/reconciliation workflows.
3. Collection duplicate-confirmation is compensated through a targeted refund intent, not payout failover.

These restrictions reduce the chance of paying both the original rail and a fallback rail for the same obligation.

## Chaos Scenario Tests

The chaos matrix is covered by deterministic SimulatedRail scenario injection:

- Timeout: `sim:timeout`
- Malformed response: `sim:malformed`
- Out-of-order callbacks: `sim:out_of_order`

Current regression coverage:

- `tests/test_escrow_service_orchestration.py`
- `tests/test_simulated_rail.py`
- `tests/test_simulated_custody_provider.py`
