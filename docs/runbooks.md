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

1. Read the audit event: note `rail_name`, `amount` and `details.failure_code`. Its `provider_reference` is the keyed `provider_ref` (#241); the raw provider reference is on the payout attempt (`payout_attempts.provider_reference`).
2. Branch on `failure_code`:
   - `insufficient_funded_balance`: the rail accepted the payout, but the escrow has no recorded funding. Confirm the buyer's collection with the provider, record funding through the collection path (webhook, the eConfirm poller, or in non-production the admin simulated force-complete or a simulated payment callback), then rerun the executor. The ledger settles once. In production the payment callback never records funding (#222, #224).
   - `rail_unavailable` / `rail_request_error`: follow RB-1, then rerun the executor.
   - Otherwise (timeout, pending, malformed): rerun the executor. It inquires with the **same** `provider_reference` and never sends a new payout.
3. If the attempt stays UNKNOWN, look up the attempt's raw `provider_reference` in the provider portal:
   - **Provider shows paid**: escalate to an engineer. There is no admin API to force-close an attempt. The engineer must set the attempt to `succeeded`, post the ledger movement with idempotency key `outbox:<attempt_id>`, and insert a `money_audit_events` row with the reason and evidence. Since M21, do both through `EscrowLedger.post_movement` and `record_money_audit_event`, never raw SQL: the services record the anchor events in the same DB transaction, and a row written by hand has none, so the anchoring audit reports it.
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

## eConfirm confirmation-code encryption key (M20)

eConfirm returns each escrow's `confirmation_code` only once, at create, and it
is required to call release/reversal. Vekro stores it **Fernet-encrypted** in
`escrows.econfirm_confirmation_code` using `ECONFIRM_CODE_ENCRYPTION_KEY` (a
dedicated key, not `SECRET_KEY`).

- **Generate a key:** `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- **Rotate:** put the NEW key first in the comma-separated list, keep old keys
  after it (`ECONFIRM_CODE_ENCRYPTION_KEY=<new>,<old>`). New writes use the new
  key; old ciphertexts still decrypt (MultiFernet). Drop an old key only once no
  stored escrow still needs it.
- **Startup guard:** if `ECONFIRM_ENABLED=true` and the key is missing or
  invalid, the app refuses to start.
- **⚠ Key loss is unrecoverable:** losing every key in the list makes all stored
  eConfirm escrows impossible to release or reverse through the API (funds would
  then have to be handled from the eConfirm dashboard). **Back this key up
  separately from the database** — a DB backup alone cannot restore access.
- The plaintext code is decrypted only for the moment of a release/reversal
  call; it is never logged, returned in an API response, cached, or written to
  the DB in clear, and it is redacted in audit events.

## RB-4: eConfirm funding poller and unconfirmed escrows (M20)

**Funding poller.** The app does not schedule it. Run it every 10–60 s while
eConfirm transactions await payment (`next_poll_delay_seconds` gives the
jittered backoff). Each run makes at most 50 GETs and skips escrows older than
15 minutes:

```bash
.venv/bin/python -c "from app.core.settings import get_settings; \
from app.db.session import _get_session_factory; \
from app.services.custody.registry import build_econfirm_provider; \
from app.services.custody.econfirm_orchestration import run_econfirm_funding_reconciliation as run; \
db=_get_session_factory()(); print(run(db, collection_rail=build_econfirm_provider(get_settings()))); db.close()"
```

A funded escrow moves its transaction to `locked`
(`econfirm_funding_confirmed` in the money audit).

**Unconfirmed escrow (`econfirm_needs_reconciliation`).** The count is shown in
`GET /admin/rails/health`. It means eConfirm's create returned no definite
answer, or the process stopped mid-call. The escrow may exist at eConfirm. No
STK was sent, and the transaction stays `awaiting_payment`.

1. Find the transaction's `econfirm_open_unknown_needs_reconciliation` audit
   event (`GET /admin/audit/money-events?action=...`).
2. Search the eConfirm dashboard for an escrow with the description
   `Vekro escrow <transaction_id>`.
3. If one exists, an engineer records its id in `escrows.econfirm_transaction_id`
   and clears the flag, with a `money_audit_events` row. The
   `confirmation_code` cannot be recovered after create, so release and reversal
   must then be done from the eConfirm dashboard.
4. If none exists, leave the transaction to the payment timeout. Do **not**
   re-open it on eConfirm automatically, because there is no idempotency key.

## RB-5: Stuck return cases (#221)

**Meaning**: before #221, two timeouts moved the transaction without moving its dispute.

- **Seller-received timeout.** The transaction went to `escalated_admin_review`, but the dispute
  stayed `open`. Admins resolve cases from the dispute, so the case was missing from the
  escalation queue, could not be resolved, and its money stayed held.
- **Buyer-sent-back timeout.** The money was released, but the dispute stayed `open`.

Both timeouts now move the dispute too (escalated, or `canceled` with `resolved_at`).

**Steps**

1. List the cases. This is a dry run and changes nothing:
   `.venv/bin/python -m app.cli repair-return-cases`
2. Fix them: `.venv/bin/python -m app.cli repair-return-cases --apply`. Each escalated case then
   appears in the admin escalation queue and is resolved there as usual. Each repair writes a
   `return_case_repaired` money audit event (`GET /admin/audit/money-events?action=return_case_repaired`).
3. Cases listed as `review` (an open dispute behind a refunded or resolved transaction) are never
   changed automatically. Look at them by hand.

## RB-6: Funding missing from the ledger (#222)

**Meaning**: for every funded escrow, the ESCROW_HELD debits must equal `escrows.funded_amount`.
Before #222, the eConfirm funding poller set `funded_amount` without a ledger posting. Since #222,
every funding path posts the balanced ESCROW_HELD / BUYER_CLEARING movement in the same commit:

- the simulated provider and the admin simulated controls;
- the eConfirm poller, with key `econfirm-funding:<escrow_id>`;
- the payment callback, on the simulated rail outside production only, with key
  `callback-funding:<transaction_id>`.

**Steps**

1. List the escrows that are out of step. This is a dry run and changes nothing:
   `.venv/bin/python -m app.cli repair-funding-ledger`
2. Post the missing movements: `.venv/bin/python -m app.cli repair-funding-ledger --apply`. Each
   posting uses key `repair-funding:<escrow_id>`, so re-running is safe. Each writes a
   `funding_ledger_repaired` money audit event.
3. Escrows listed as `review` have more on the ledger than `funded_amount`, which means money was
   posted twice or `funded_amount` was edited. They are never changed automatically. Compare the
   ledger rows with the provider's records by hand.

## RB-7: Anchor event backfill (M21)

**When**: once, right after deploying the M21 migration (`f4c2a9e1b7d3`). From that deploy on,
every ledger row, money audit row, status transition and dispute event is recorded in
`anchor_events` in the same DB transaction as the fact itself. The backfill covers everything
written before.

**Steps**

1. `.venv/bin/python -m app.cli anchor-backfill`. It anchors every ledger row, money audit row and
   transaction that has no event yet, in original time order, with `"backfilled": true`. It then
   records one `backfill_snapshot` of each transaction's current status and dispute fields, and a
   global `backfill_completed` marker. It commits every 500 events.
2. A second run refuses. `--resume` fills only rows that still have no event (for example after an
   interrupted run) and adds fresh snapshots.

**What it can and cannot claim**: status transitions and dispute events from before M21 were never
recorded anywhere trustworthy, so none are reconstructed. A backfilled fact is attested from the
moment of the backfill, not from when it happened.

**Append-only tables**: the migration also makes `ledger_entries`, `money_audit_events` and
`anchor_events` refuse UPDATE, DELETE and TRUNCATE. Engineer repairs therefore post compensating
movements and write audit rows; they never edit rows. RB-2's "set the attempt to succeeded" step
still works, because `payout_attempts` is not guarded. An escrow can no longer be deleted once it
has ledger rows.

## RB-8: Anchoring halted (M21)

**Signal**: `python -m app.jobs.anchoring` exits with code 3, `GET /admin/anchoring/status` shows
`halted: true`, and a money audit event `anchoring_halted` exists. Nothing more is anchored until
the halt is cleared. Escrows keep working, and events keep being recorded.

**Meaning**: the registry disagrees with the database. Either it holds a different root for a batch
id, it has a different number of batches, or the previous root does not match. Possible causes,
most likely first:

1. The worker points at the wrong registry or chain (`ANCHOR_REGISTRY_ADDRESS`, `ANCHOR_CHAIN_ID`,
   or a restored database matched with a fresh registry).
2. The database was restored from a backup older than the chain.
3. Someone else holds the anchorer key and anchored their own batch (treat as a key compromise,
   RB-10).
4. Tampering: the batch's events changed after the batch was built.

**Steps**

1. Do not edit `anchor_batches` or `anchor_events`. Run `scripts/audit_anchoring.py` and keep the
   output.
2. Compare `halt_reason` with the registry: `cast call <registry> "getBatch(uint256)" <id>` and
   `batchCount()`.
3. Cause 1: fix the settings, then set the halted batch back to `built` (`UPDATE anchor_batches
   SET status='built', halt_reason=NULL WHERE id=<id>`; `anchor_batches` is working state, not
   guarded). Restart the worker.
4. Cause 2 or 3: deploy a new registry, record the switch-over, and re-anchor from batch 1 (RB-10).
5. Cause 4: an incident. Preserve the database and the audit output, and notify the maintainer.

## RB-9: Payout held or halted by the anchor gate (M21 A8)

**Signal**: a payout attempt stays `unknown` with `failure_code` set to `waiting_for_anchor`,
`anchor_check_unavailable` or `anchor_mismatch_halted`. The admin status shows
`payouts_waiting_for_anchor` or `payouts_halted`.

- **waiting_for_anchor**: normal for up to a few minutes after the decision. If the wait grows,
  check RB-8 and the worker. The payout goes out on the first executor run after its batch is
  confirmed.
- **anchor_check_unavailable**: the RPC is down. The payout waits, nothing fails. Check
  `ANCHOR_RPC_URL`.
- **anchor_mismatch_halted** (audited once as `payout_<kind>_halted_anchor_mismatch`): the payout
  no longer matches its anchored authorisation (amount, destination or order), or the proof fails
  against the registry. **Do not pay by hand.** Run `scripts/verify_transaction.py
  --transaction-id <id>` and `scripts/audit_anchoring.py`. If an engineer changed the attempt
  deliberately, the change must go through a new decision: mark the attempt `failed_definite`
  with a note, and let the service queue a fresh payout, which records and anchors a new
  authorisation. Otherwise treat it as an incident.

## RB-10: Anchorer key rotation or registry switch-over (M21)

The registry has an immutable anchorer and no owner (decision D6), so a key change means a new
registry.

1. Stop the worker (`run-loop`). Wait until `in_flight` is null or `confirmed`.
2. With the maintainer's go-ahead and a funded key, deploy:
   `CONFIRM_PUBLIC_DEPLOY=<network> ANCHORER_ADDRESS=<new address> forge script
   script/Deploy.s.sol --rpc-url <network> --broadcast --private-key <deployer key>`.
   Commit `contracts/deployments/<network>.json`, and keep the old file as
   `<network>-until-<date>.json`.
3. Record the switch-over: a `money_audit` row with action `anchoring_registry_switched` and the old
   and new addresses in `details`, written through `record_money_audit_event`. It is anchored in
   the new registry's first batch.
4. The new registry starts at batch 1 and seq 1. Existing batches stay verifiable against the old
   registry. Verifiers must know both addresses with the batch ranges, and the web app pins the
   new one.
5. Set `ANCHOR_REGISTRY_ADDRESS` and `ANCHOR_PRIVATE_KEY`. Then renumber: archive the old
   `anchor_batches` rows and assign new batch ids from 1. This is a deliberate engineering step,
   reviewed and done in a migration, never ad hoc.
6. Rotating `ANCHOR_ADMIN_REF_SECRET`: put the new key first and keep the old ones after it. Old
   `admin_ref`, `destination_ref` and `sale_ref` values still resolve.

## RB-11: Card refund waiting for support (M22, #253)

Card money is collected into the merchant's Pesapal account, so it cannot be paid back over M-Pesa,
and Pesapal refunds need the merchant's approval and Pesapal's own processing. When a card-funded
order ends in a refund (a dispute outcome, a door rejection, or the refund part of a split), the
refund is queued to `manual_card_refund`. The payout executor never sends it, and the order's
record shows `manual_refund_required`.

1. List the queue: `GET /admin/refunds/manual`. Each item has the order reference (Pesapal's
   merchant reference), the amount and the Pesapal order tracking id.
2. In the Pesapal merchant dashboard, find the payment by reference or tracking id and refund
   it. You can also call Pesapal's `RefundRequest` API (`docs/pesapal-observed.md` section 7).
   Mobile-money payments can only be refunded in full, and only once.
3. When Pesapal confirms the refund, call `POST /admin/refunds/{attempt_id}/complete` with a note
   and Pesapal's refund reference. This settles the refund like any payout: ledger
   `BUYER_REFUNDABLE` / `ESCROW_HELD`, the escrow's refunded total and the payout status. It also
   anchors `manual_refund_completed`, naming the admin only as `admin_ref`. It is idempotent.
4. A 409 means it cannot complete yet. Either the refund's approval is not anchored (with
   `ANCHOR_GATE_PAYOUTS`; wait for the next batch, see RB-9), or the escrow's held balance does
   not cover it (investigate before refunding anything in Pesapal).

Never complete a refund before Pesapal has actually paid it back.
