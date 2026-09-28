# ADR: Custody Abstraction and Tier Model

- Status: Accepted
- Date: 2026-09-28
- Owners: Backend team
- Related issue: #60

## Context

Vekro's escrow workflow currently models transaction and dispute state transitions in service-layer finite state machine logic. Milestone 10 introduces a custody architecture that must support two operational tiers:

- Tier 1: regulated or structurally custodial providers.
- Tier 2: application-enforced custody where rails move money directly but business rules emulate escrow behavior.

The architecture must allow new providers and rails without changing FSM logic each time integration details change.

## Decision

1. FSM and dispute workflows depend only on a `CustodyProvider` port (and related domain DTOs), never directly on rail SDKs or HTTP clients.
2. Provider-specific integrations are implemented behind adapters and selected by configuration.
3. Capabilities are explicit and machine-readable so business logic can guard unsupported operations.
4. Money movement outcomes use a constrained taxonomy used consistently across rails and providers.

## Tier Model and Honesty Constraints

### Tier 1 (structural custody)

- Funds are held by a regulated or structurally custodial system.
- Providers in this tier report `holds_funds_structurally = true`.

### Tier 2 (application-enforced custody)

- Funds are coordinated by application logic, ledger controls, and rail orchestration.
- Providers in this tier report `holds_funds_structurally = false`.

### Disclosure requirement

If `holds_funds_structurally = false`, product and operational documentation must clearly disclose that custody is application-enforced and not a regulated custodial product. This value must be inspectable through admin/operator surfaces and reflected in user-facing honesty language.

## Capability Flags

Each provider exposes capabilities so orchestration can fail safely instead of guessing support.

Baseline capability set:

- `holds_funds_structurally`: true when custody is structurally held by provider.
- `supports_split_payout`: true when provider can execute split payout natively.
- `supports_partial_release`: true when provider supports releasing part of held value.
- `supports_webhook_auth`: true when callbacks can be authenticated with provider signatures.

Capabilities are treated as contracts. Business flows must return deterministic errors when required capability is unavailable.

## Outcome Taxonomy

All collection and payout operations must classify outcomes into one of three terms:

- `SUCCEEDED`: final success is confirmed.
- `FAILED_DEFINITE`: terminal failure is confirmed (invalid request, explicit decline, irreversible rejection).
- `UNKNOWN`: final state is not yet provable (timeout, malformed response, transport ambiguity, out-of-order callback).

`UNKNOWN` is safety-critical and must trigger reconciliation/inquiry before any failover that could risk duplicate debit or payout.

## Rationale

- Keeps state machine and dispute logic stable while adding new rails/providers.
- Prevents tight coupling between core business rules and vendor payloads.
- Enables honest custody semantics without changing endpoint contracts.
- Reduces double-pay risk via mandatory `UNKNOWN` handling semantics.

## Consequences

- New provider integrations must implement the custody interfaces and capability metadata.
- FSM transitions remain provider-agnostic and invoke custody operations through service ports.
- Documentation and runbooks must preserve explicit disclosure for Tier 2 operation.

## Non-goals

- This ADR does not define vendor-specific payload schemas.
- This ADR does not alter existing transaction/dispute statuses.
- This ADR does not authorize direct user-triggered payout endpoints.