# Incident Runbooks

Operator procedures for custody incidents. They describe the behaviour of the code on `main`.

## Shared tooling

The payout executor and reconciliation are **not scheduled** by the app. Run them by hand (or from cron) with the configured `.env`:

```bash
# Process queued/UNKNOWN payout attempts (inquiry first if a provider reference exists)
.venv/bin/python -c "from app.db.session import _get_session_factory; \
from app.services.escrow_service import run_payout_executor_once; \
db=_get_session_factory()(); print(run_payout_executor_once(db)); db.close()"

# Raise admin alerts for stale UNKNOWN payouts and terminal-status mismatches
.venv/bin/python -c "from app.db.session import _get_session_factory; \
from app.services.escrow_service import run_payout_reconciliation; \
db=_get_session_factory()(); print(run_payout_reconciliation(db, unknown_age_minutes=30)); db.close()"
```

Admin read surfaces (admin session required):

| Endpoint | Use |
| --- | --- |
| `GET /admin/rails/health` | Custody mode, `holds_funds_structurally`, rail priorities, breaker state per rail |
| `GET /admin/audit/money-events?transaction_id=…&provider_reference=…&rail_name=…&action=…` | Money-movement history with `txn=…\|ref=…\|rail=…` correlation IDs |
| `GET /notifications` | Reconciliation alerts addressed to admins (`SYSTEM_TIMEOUT` events) |

Logs: grep `app.audit.money` for the correlation ID, `app.custody.loop.webhook` / `app.custody.pesapal.webhook` for callback rejections and replays.

Golden rule for all incidents: **never create a second payout for a purpose that already has an UNKNOWN or SUCCEEDED attempt.** Purposes (`tx-release:<txn>`, `admin-refund:<dispute>`, …) are the idempotency keys; a duplicate attempt is the only way to pay twice.

---

## RB-1: Rail outage

**Symptoms**

- `GET /admin/rails/health` shows `breaker_state: open` (or `half_open`) with a rising `consecutive_failures` and a `last_error_code`.
- Money audit shows `payout_*_unknown` events with `details.failure_code` of `rail_unavailable` or `rail_request_error`.
- LOOP/Pesapal webhooks return `503` when the rail is not configured at runtime.

**How the system reacts**

- Breaker opens after 3 consecutive failures, cools down for 300 s, then goes `half_open`. One success closes it; a failure in `half_open` reopens it.
- New payout intents are routed with the breaker applied: open rails are skipped in `CUSTODY_PAYOUT_RAIL_PRIORITY` order.
- Attempts already created keep their `rail_name`. The executor retries them on the same rail; they are **not** re-routed automatically.
- Live rails (`loop`, `intasend`, `econfirm`) never receive payouts unless `ENVIRONMENT=production` and `ALLOW_LIVE_PAYOUTS=true`.

**Steps**

1. Confirm the outage on the provider's status page or sandbox portal. Note the start time.
2. Check `GET /admin/rails/health` for the affected rail and the remaining healthy rails in the priority list.
3. If the outage is short, do nothing: the breaker handles new intents, and UNKNOWN attempts are retried by later executor runs.
4. If the outage is long, change `CUSTODY_PAYOUT_RAIL_PRIORITY` (or set the rail's `*_ENABLED=false`) and restart. This affects new intents only.
5. After recovery, run the executor and confirm `payout_*_succeeded` events in the money audit for the backlog.
6. Treat any attempt still UNKNOWN after recovery under RB-2.

---

## RB-2: UNKNOWN payout resolution

**Meaning**: the rail did not give a definite answer (timeout, malformed response, unavailable rail, or funded balance too low). The money may or may not have moved.

**Detection**

- `run_payout_reconciliation` notifies all active admins with `alert_key: payout-unknown:<attempt_id>` once the attempt is older than `unknown_age_minutes`. Alerts are deduped per attempt.
- `GET /admin/audit/money-events?action=payout_release_unknown` (or `payout_refund_unknown`).

**Steps**

1. Read the audit event: note `provider_reference`, `rail_name`, `amount`, and `details.failure_code`.
2. Branch on `failure_code`:
   - `insufficient_funded_balance`: the rail accepted the payout, but the escrow has no recorded funding. Confirm the buyer's collection with the provider, record funding through the collection path (webhook or admin simulated force-complete in non-production), then rerun the executor. The ledger settles once.
   - `rail_unavailable` / `rail_request_error`: follow RB-1, then rerun the executor.
   - Otherwise (timeout, pending, malformed): rerun the executor. It inquires with the **same** `provider_reference` and never sends a new payout.
3. If the attempt stays UNKNOWN, look up `provider_reference` in the provider portal:
   - **Provider shows paid**: escalate to an engineer. There is no admin API to force-close an attempt. The engineer must set the attempt to `succeeded`, post the ledger movement with idempotency key `outbox:<attempt_id>`, and insert a `money_audit_events` row with the reason and evidence.
   - **Provider shows failed/never received**: escalate to an engineer to mark the attempt `failed_definite` with an audit row. Only after that can a new attempt be queued for the same purpose.
4. Never retry by queuing a new purpose or editing `provider_reference`.

Non-production only: provider-created simulated attempts (`sim-release:` / `sim-refund:` idempotency keys) can be advanced with `POST /admin/simulated-custody/payouts/{provider_reference}/progress`. Executor outbox attempts (`outbox:` keys) cannot.

---

## RB-3: Reconciliation mismatch

**Meaning**: a transaction reached a payout-requiring terminal state (`released`, `refunded_buyer`, `resolved_release`, `resolved_refund`, `resolved_split`) but `payout_status` is still `not_required`, so no payout intent exists.

**Detection**: admin notification with `alert_key: payout-mismatch:<transaction_id>:<status>`.

**Likely causes**: the transition happened while the outbox tables were unavailable (the queue step is skipped when they are missing), or data was edited by hand.

**Steps**

1. Confirm there is no `payout_*_queued` audit event for the transaction:
   `GET /admin/audit/money-events?transaction_id=<id>`.
2. Confirm there is no `payout_attempts` row for the transaction's escrow.
3. Queue the missing intent with the canonical purpose for that transition. Queueing is idempotent per purpose, so re-running is safe:

   ```bash
   .venv/bin/python -c "import uuid; from app.db.session import _get_session_factory; \
   from app.models.transaction import Transaction; from app.services.escrow_service import EscrowService; \
   db=_get_session_factory()(); t=db.get(Transaction, uuid.UUID('<transaction_id>')); \
   EscrowService(db, audit_reason='RB-3 mismatch repair').queue_release_full(t, purpose=f'tx-release:{t.id}'); \
   db.commit(); db.close()"
   ```

   Use `queue_refund_full` with the matching refund purpose for refund states. For `resolved_split`, use the dispute's `admin-split-release:<dispute_id>` and `admin-split-refund:<dispute_id>` purposes via `queue_split_payout`.
4. Run the executor, then confirm `payout_status` and the audit trail.
