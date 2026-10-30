# Anchoring design note and event inventory (M21 A1)

Status: **proposal, awaiting maintainer approval.** No code changes follow from this note until it
is approved. Section 8 lists the decisions needed.

This note covers four things. It lists every fact on `main` (at `cb16c20`) that M21 must anchor. It
names the single choke point where `record_anchor_event` will be called for each one. It fixes the
canonical payload for each event type. It lists every place where such a fact is currently updated
in place, which is a design problem for a tamper-evident log.

---

## 1. Summary

| Fact class | Table today | Written through | Append-only today? | M21 choke point |
| --- | --- | --- | --- | --- |
| Ledger postings | `ledger_entries` | `EscrowLedger.post_movement` only | Yes (no UPDATE or DELETE anywhere) | inside `post_movement` |
| Money audit | `money_audit_events` | `record_money_audit_event` only | Yes | inside `record_money_audit_event` |
| Transaction status transitions | **none** (`transactions.status` is mutable) | 25 direct `transaction.status = …` assignments in 3 files | No | new `transition_transaction(...)` helper |
| Dispute events | **none** (`disputes` row and its `evidence` JSONB are mutable) | direct field writes in 3 files | No | new `record_dispute_event(...)` helper |
| Admin decisions | `disputes.admin_decision`, `admin_reason`, `split_ratio`, `decided_by_user_id` (mutable) | `force_resolve_dispute_case` | No | inside `force_resolve_dispute_case` |

Main findings:

1. **No transition table and no dispute-event table exist.** `anchor_events` becomes the system of
   record for both, as the brief allows. Today, transition history survives only in log lines and
   in `notifications.payload.transition_history`. Notifications are mutable (`read_at`) and are
   written per recipient, so they cannot serve as the source.
2. **There is no transition function.** Status is assigned directly in 25 places across
   `transaction.py`, `dispute.py` and `dispute_escalation.py` (section 3.3). A3 must route every
   assignment through one helper. A session guard makes any missed assignment fail the test suite
   (section 5.8).
3. **Global `seq` from a `bigserial` cannot be contiguous in commit order.** A rolled-back insert
   leaves a gap. A long transaction can also commit a lower `seq` after a higher one was already
   batched. Either case breaks the contract's `firstSeq == lastSeqAnchored + 1` rule and turns
   "seq gap" into a false deletion alarm. I propose a deviation: a `bigserial` insert id, plus a
   gapless `seq` assigned by the batcher (section 5.5).
4. **eConfirm funding never reaches the ledger.** The poller sets `escrows.funded_amount` directly,
   with no ledger posting. The Daraja-style payment callback locks a transaction with no funding
   posting at all. The anchored facts for these paths are the money audit event and the transition,
   not ledger rows (problem P5).
5. **Pre-existing bug, outside M21:** the seller-received timeout escalates the transaction but
   leaves the dispute `open`. No admin path can then resolve it, and the money stays held (section 7).

---

## 2. Scope reminders

- Only 32-byte roots and seq ranges go on-chain. Payloads, salts, leaves and proofs stay in Vekro's
  database and are served to the transaction's parties.
- The event row is written in the same DB transaction as the fact. Anything that involves the chain
  is asynchronous. With `ANCHOR_ENABLED=false`, events are still recorded and nothing is submitted.
- M-Pesa, eConfirm and the simulated rail are unchanged. M21 adds no ledger postings, so money flows
  behave identically. The only behaviour change is the extra `anchor_events` insert in each
  transaction.

---

## 3. Inventory of anchorable facts

### 3.1 Ledger postings (`ledger_entries`)

The only writer is `EscrowLedger.post_movement` (`app/services/ledger.py`). It writes two rows (one
debit, one credit) per movement, and nothing else in the code base writes them. An idempotent replay
writes nothing. Rows are never updated or deleted by the app.

| Caller | Idempotency key | Movement |
| --- | --- | --- |
| `SimulatedCustodyProvider.request_funding` | `sim-funding:<ref>` (provider format) | ESCROW_HELD / BUYER_CLEARING |
| `SimulatedCustodyProvider._request_payout` | `sim-release:` / `sim-refund:` | SELLER_PAYABLE or BUYER_REFUNDABLE / ESCROW_HELD |
| `simulated_admin.progress_simulated_collection_scenario` | `sim-funding:<ref>` | ESCROW_HELD / BUYER_CLEARING |
| `simulated_admin.progress_simulated_payout_scenario` | `sim-<kind>:<ref>` | payable / ESCROW_HELD |
| `run_payout_executor_once` | `outbox:<attempt_id>` | payable / ESCROW_HELD |

**Choke point:** inside `post_movement`, after both rows are built and before its `commit()`. Each
of the two rows gets its own event, so a verifier can rebuild either row independently. `post_movement`
does not set `LedgerEntry.id`, which is otherwise generated at flush. A3 sets it explicitly so the
event's `source_id` is known before flush.

Note: `post_movement` commits by itself, mid-operation, in every caller. The caller's later writes
(escrow totals, the payout audit row, `payout_status`) land in a second commit. The ledger rows and
their events are atomic with each other, which is what anchoring needs. I am not changing this
commit boundary in M21 (problem P7).

### 3.2 Money audit events (`money_audit_events`)

The only writer is `record_money_audit_event` (`app/services/audit.py`). Rows are never updated or
deleted. Current actions:

| Action | Written by | `transaction_id` set? |
| --- | --- | --- |
| `payout_release_queued`, `payout_refund_queued` | `EscrowService._enqueue_payout_intent` | yes |
| `payout_<kind>_<succeeded|failed_definite|unknown>` | `_audit_payout_attempt` (executor) | yes |
| `admin_force_resolve_<refund|release|split>` | `force_resolve_dispute_case` | yes |
| `simulated_collection_forced`, `simulated_collection_progressed`, `simulated_payout_<kind>_progressed` | `simulated_admin` | yes, when the escrow exists |
| `econfirm_open_succeeded`, `econfirm_open_unknown_needs_reconciliation`, `econfirm_open_rejected_fallback_simulated`, `econfirm_stk_requested`, `econfirm_funding_confirmed` | `econfirm_orchestration` | yes |
| `funding_confirmed` (LOOP, Pesapal) | `loop_webhook`, `pesapal_webhook` | **no**, only `provider_reference` |

**Choke point:** inside `record_money_audit_event`, right after the `MoneyAuditEvent` is added.
`occurred_at` is otherwise filled in at flush, so A3 sets it explicitly before building the payload.

When the audit table is missing (some unit tests build partial schemas), the function returns `None`
without writing. The anchor helper follows the same rule: no source row means no event.

LOOP and Pesapal `funding_confirmed` rows have no `transaction_id`, so their events go into the
**global** chain (`transaction_id = NULL`). They are still anchored and covered by the full audit,
but they do not appear in a single transaction's verification. Neither rail is enabled today. I do
not propose resolving `provider_reference` to a transaction at write time, because it would put a
derived value in a payload that is meant to be rebuilt from the source row.

### 3.3 Transaction status transitions (no table today)

Every place that changes `transactions.status`:

| # | Function | From → to | Actor | Reason code | Money effect in the same transaction |
| --- | --- | --- | --- | --- | --- |
| T1 | `create_transaction` | ∅ → `awaiting_payment` | user (buyer) | `created` | eConfirm escrow row |
| T2 | `confirm_payment_callback` | `awaiting_payment` → `locked` | provider | `payment_callback_confirmed` | none |
| T3 | `lock_transaction_after_custody_funding` | `awaiting_payment` → `locked` | system | `custody_funding_confirmed` | `econfirm_funding_confirmed` audit |
| T4 | `dispatch_transaction` | `locked` → `out_for_delivery` | user (seller) | `seller_dispatched` | none |
| T5 | `mark_transaction_arrived` | `out_for_delivery` → `at_door_pending_inspection` | user (seller) | `seller_arrived` | none |
| T6 | `confirm_buyer_delivery_otp` | `at_door_pending_inspection` → `hold_24h` or `released` | user (buyer) | `otp_confirmed` | release queued (non-serialized) |
| T7 | `withhold_buyer_delivery_otp` | `at_door_pending_inspection` → `return_in_transit` → `returned_to_seller` → `refunded_buyer` (three steps, one instant) | user (buyer) | `otp_withheld` | refund queued |
| T8 | `report_functional_issue` | `hold_24h` → `disputed_functional` | user (buyer) | `functional_issue_reported` | dispute created |
| T9 | `mark_buyer_sent_back` | `disputed_functional` → `return_in_transit` | user (buyer) | `buyer_sent_back` | none |
| T10 | `mark_seller_received` | `return_in_transit` → `return_received` | user (seller) | `seller_received` | none |
| T11 | `apply_seller_resolution_action` | `return_received` → `refunded_buyer` or `awaiting_buyer_reconfirmation` | user (seller) | `seller_resolution_<action>` | refund queued (refund_issued) |
| T12 | `submit_buyer_reconfirmation` | `awaiting_buyer_reconfirmation` → `resolved_release`, `return_received` or `escalated_admin_review` | user (buyer) | `buyer_reconfirmed_accepted`, `buyer_reconfirmed_rejected`, `reconfirm_retry_limit` | release queued (accepted) |
| T13 | `escalate_dispute` | open case → `escalated_admin_review` | user (buyer or seller) | `party_escalated` | none |
| T14 | `force_resolve_dispute_case` | `escalated_admin_review` → `resolved_refund`, `resolved_release` or `resolved_split` | admin | `admin_decision` | refund, release or split queued |
| T15 | `run_timeout_jobs` (at door) | as T7, three steps | system | `at_door_timeout_1h` | refund queued |
| T16 | `run_timeout_jobs` (no dispatch) | `locked` → `refunded_buyer` | system | `locked_no_dispatch_timeout_48h` | refund queued |
| T17 | `run_timeout_jobs` (hold) | `hold_24h` → `released` | system | `hold_24h_auto_release_24h` | release queued |
| T18 | `run_timeout_jobs` (no send-back) | `disputed_functional` → `released` | system | `dispute_buyer_sent_back_timeout_3d` | release queued |
| T19 | `run_timeout_jobs` (no receipt) | `return_in_transit` → `escalated_admin_review` | system | `seller_received_timeout_3d` | none |

`initiated`, `dispatched` and `returned_to_seller` (as a resting state) are never set outside T7/T15.
Each step of T7 and T15 becomes its own transition event, so the anchored chain shows all three.

**Choke point:** a new `transition_transaction(db, transaction, to, *, actor_type, actor_id, reason,
at)` in `app/services/transitions.py`. It checks that `transaction.status` is the expected `from`,
sets the status and records the `transaction_transition` event. It does not commit. The 25
assignments above are rewritten to call it. The guards around them (state checks, role checks,
OTP checks) stay where they are. T1 records `transaction_created` (section 4) instead of a transition
from ∅.

### 3.4 Dispute events (no table today)

The `disputes` row is created once (T8) and then mutated in place. Events to record:

| Event type | Where | Facts |
| --- | --- | --- |
| `dispute_opened` | T8 `report_functional_issue` | dispute id, type, category, route (`seller_resolution` or `admin_escalation`), opener |
| `dispute_return_sent` | T9 `mark_buyer_sent_back` | buyer marked the item sent back |
| `dispute_return_received` | T10 `mark_seller_received` | seller confirmed receipt |
| `dispute_seller_resolution` | T11 `apply_seller_resolution_action` | action, notes |
| `dispute_buyer_reconfirmation` | T12 `submit_buyer_reconfirmation` | accepted, rejection count after this answer, notes |
| `dispute_escalated` | T8 (category `other`), T12 (retry limit), T13, T19 | by (`buyer`, `seller`, `system`), reason |
| `dispute_resolution_confirmed` | `submit_buyer_resolution_confirmation`, `submit_seller_resolution_confirmation` | party, both flags after this answer, closed |
| `dispute_auto_closed` | T18 (buyer never sent it back) | reason |
| `admin_decision` | T14 `force_resolve_dispute_case` | see 3.5 |

**Choke point:** a new `record_dispute_event(db, dispute, event_type, *, actor_type, actor_id, at,
**facts)` next to `transition_transaction`. It is called beside the existing field writes, which
stay as they are. The `disputes` row remains the read model for the UI.

### 3.5 Admin decisions

The only writer is `force_resolve_dispute_case` (`app/services/dispute.py`). It already writes an
`admin_force_resolve_<decision>` audit row, which gets anchored through 3.2. It will also record
one `admin_decision` event that carries the full decision, including the split amounts the
escrow service computes, plus the T14 transition. The audit row alone is not enough: it does not
carry the release and refund amounts of a split, and it is a generic audit shape.

---

## 4. Canonical payloads (schema version 1)

Encoding rules (A2 implements them, and the golden vectors pin them):

- RFC 8785 (JCS): keys sorted, no whitespace, JCS string escaping. All keys are ASCII, so the sort
  order is plain byte order.
- **No JSON floats anywhere.** Money is a decimal string with 2 places (`"500.00"`). The split ratio
  is a string with 4 places (`"0.5000"`). Integers stay within ±2^53. Any float met in `details` is
  encoded as a decimal string (`str(Decimal(repr(x)))`), the same way at write and at rebuild.
- Timestamps are UTC ISO-8601 truncated to seconds, with a `Z` suffix (`"2026-10-31T09:15:02Z"`).
- UUIDs are lowercase with hyphens. Enum values are their stored string values. Absent optional
  values are `null`; keys are never omitted.
- Free text written by a party (dispute description, notes, escalation reason) is **not** copied
  into the payload. It is bound by `keccak256(utf8(text))` as `0x`-prefixed lowercase hex, or `null`
  when there is no text. The full audit can still detect an edited `disputes.description`, and
  verification responses do not re-serve every party's free text. The admin's reason is the
  exception and is copied verbatim, as the brief requires (see decision D4).
- Every payload has `"v": 1`, `"type"`, `"transaction_id"` (or `null` for global events) and `"at"`.
  Backfilled payloads add `"backfilled": true`. Live payloads do not carry the key.

The payload for a source-backed event (`ledger_entry`, `money_audit`) is **rebuilt from the source
row's current columns**. So editing `ledger_entries.amount` changes the rebuilt leaf even when
`anchor_events` is untouched. For events whose source is the log itself (transitions, dispute events,
admin decisions), the stored payload is the record.

```jsonc
// ledger_entry (one per row; source_table = ledger_entries)
{"v":1,"type":"ledger_entry","transaction_id":"…","at":"…",            // at = created_at
 "ledger_entry_id":"…","escrow_id":"…","entry_group_id":"…",
 "idempotency_key":"outbox:…","account_code":"ESCROW_HELD","side":"credit",
 "amount":"500.00","currency":"KES","description":"Payout executor release for tx-release:…"}
// transaction_id comes from escrows.transaction_id (immutable foreign key).

// money_audit (one per row; source_table = money_audit_events)
{"v":1,"type":"money_audit","transaction_id":"…"|null,"at":"…",        // at = occurred_at
 "audit_event_id":"…","action":"payout_release_queued","actor_type":"system","actor_id":null,
 "reason":"…"|null,"escrow_id":"…"|null,"attempt_id":"…"|null,"provider_reference":"…"|null,
 "rail_name":"simulated"|null,"correlation_id":"txn=…|ref=…|rail=…",
 "amount":"500.00"|null,"currency":"KES"|null,"details":{…}}

// transaction_created (genesis of every transaction chain; source_table = transactions)
{"v":1,"type":"transaction_created","transaction_id":"…","at":"…",     // at = created_at
 "reference":"VK-7K3P9Q","listing_id":"…","buyer_id":"…","seller_id":"…",
 "amount":"500.00","currency":"KES"}
// custody_provider is left out on purpose: it can change after creation (simulated fallback).

// transaction_transition (source = the log)
{"v":1,"type":"transaction_transition","transaction_id":"…","at":"…",
 "from":"at_door_pending_inspection","to":"return_in_transit",
 "actor_type":"user"|"admin"|"system"|"provider","actor_id":"…"|null,"reason":"otp_withheld",
 "door_rejection_reason":"damaged"|null}   // T7 only; null elsewhere

// dispute_* (source = the log); common fields first, then per-type fields
{"v":1,"type":"dispute_opened","transaction_id":"…","at":"…","dispute_id":"…",
 "actor_type":"user","actor_id":"…",
 "dispute_type":"functional","category":"battery","route":"seller_resolution",
 "description_keccak":"0x…"}
//  dispute_return_sent, dispute_return_received: common fields only
//  dispute_seller_resolution:    "action":"repair_shipped","notes_keccak":"0x…"|null
//  dispute_buyer_reconfirmation: "accepted":false,"rejection_count":1,"notes_keccak":"0x…"|null
//  dispute_escalated:            "by":"buyer"|"seller"|"system","reason":"party_escalated",
//                                "reason_text_keccak":"0x…"|null
//  dispute_resolution_confirmed: "party":"buyer","buyer_confirmed":true,
//                                "seller_confirmed":false,"closed":false
//  dispute_auto_closed:          "reason":"dispute_buyer_sent_back_timeout_3d"

// admin_decision (source = the log)
{"v":1,"type":"admin_decision","transaction_id":"…","at":"…","dispute_id":"…",
 "decision":"split","reason":"<admin reason, verbatim>","admin_id":"…",
 "seller_share":"0.5000"|null,"release_amount":"250.00"|null,"refund_amount":"250.00"|null}

// backfill_snapshot (backfill only, one per transaction; source_table = transactions)
{"v":1,"type":"backfill_snapshot","transaction_id":"…","at":"<backfill time>","backfilled":true,
 "status":"released","payout_status":"succeeded","amount":"500.00","currency":"KES",
 "dispute":null | {"dispute_id":"…","status":"…","admin_decision":"…"|null,"admin_reason":"…"|null,
                   "admin_id":"…"|null,"split_ratio":"0.5000"|null,
                   "seller_resolution_action":"…"|null,"resolved_at":"…"|null}}

// Global events (transaction_id null): anchoring_registry_switch (key rotation, A7),
// backfill_completed (marker written once by the backfill command).
```

Leaf: `keccak256(0x00 || salt || prev_event_hash || utf8(payload))`, as specified. The salt is 32
random bytes per event.

The JavaScript verifier never has to canonicalize anything. It hashes the payload bytes exactly as
served, so JCS matters only to the backend, which rebuilds payloads from source rows. The golden
vectors still pin the canonical text, so the frontend can parse it and render values with confidence.

---

## 5. Proposed design for A3 and the parts of A5 it constrains

### 5.1 `anchor_events` columns

As in the brief, with one change to `seq` (5.5):

| Column | Type | Notes |
| --- | --- | --- |
| `id` | `bigserial` PK | insert order; may have gaps; never on-chain |
| `seq` | `bigint` NULL, unique | gapless global order, set once by the batcher |
| `transaction_id` | UUID NULL, FK `transactions` ON DELETE RESTRICT | NULL means the global chain |
| `event_type` | varchar(48) | |
| `source_table`, `source_id` | varchar(48) NULL, varchar(64) NULL | unique together when set, so a source row is anchored at most once |
| `schema_version` | int | 1 |
| `payload` | text | canonical JSON |
| `salt`, `prev_event_hash`, `leaf_hash` | bytea(32) (`LargeBinary` on SQLite) | |
| `batch_id`, `leaf_index` | bigint NULL, int NULL | set once by the batcher |
| `created_at` | timestamptz | |

### 5.2 `record_anchor_event(...)`

- It is a pure DB write: one SELECT for the previous leaf hash, then one INSERT, plus an advisory
  lock (5.6). No network I/O, ever.
- It does not swallow errors. It runs inside the money transaction by design: a failed insert in
  Postgres aborts the transaction, so swallowing the error would only hide a missing event. To keep
  this from failing a money operation, the serializers are pure functions with property tests, and
  the helper raises only on programmer error.
- It skips silently when `anchor_events` does not exist, cached per session, which is the same rule
  as `record_money_audit_event`. Production always has the table after the migration. The
  completeness tests build the full schema.

### 5.3 Transition and dispute helpers

These are covered in 3.3 and 3.4. `transition_transaction` replaces each `transaction.status = X`.
The existing timestamp side-effects (`locked_at`, `released_at`, …) stay at the call sites so
the diff stays readable.

### 5.4 Immutability trigger

In Postgres, a `BEFORE UPDATE OR DELETE` row trigger and a `BEFORE TRUNCATE` statement trigger
raise unless the update only moves `seq`, `batch_id` and `leaf_index` from NULL to a value, with
every other column unchanged. SQLite gets an equivalent trigger, created through a
`DDL(...).execute_if(dialect=...)` listener on the table, so the 700+ SQLite tests exercise it too.
The migration creates the Postgres version. Its downgrade drops the triggers, the function and the
table, and the CI Postgres job checks that.

`ledger_entries` and `money_audit_events` get no triggers in M21. Their tampering is caught by
rebuilding payloads from those rows. Adding triggers there would be cheap defence in depth (D7).

### 5.5 Deviation: insert id versus anchored `seq`

The problem with a `bigserial seq`, in concrete terms:

- If transaction A takes `seq=10`, transaction B takes `seq=11` and commits first, and the batcher
  runs, then batch 1 ends at 11. A commits afterwards with `seq=10`. The next batch cannot start at
  12 without skipping 10, and it cannot include 10 without overlapping.
- A rolled-back money operation consumes a sequence value, which leaves a permanent gap. The full
  audit cannot tell that gap apart from a deleted event.

Proposal:

- `id bigserial` is insert order.
- The batcher holds `pg_advisory_xact_lock(<anchoring>)`, so only one batcher runs. It takes
  committed unbatched rows ordered by `id`, with `FOR UPDATE SKIP LOCKED`. It assigns
  `seq = last_seq + 1 …` contiguously, then `leaf_index = seq - first_seq` and `batch_id`.
- On-chain `firstSeq` and `lastSeq` are then gapless. A gap in `seq`, or a batch whose row count
  differs from the on-chain `leafCount`, really does mean deletion.
- What is lost: an event deleted *before* it was batched leaves no seq gap. The per-transaction hash
  chain still exposes it, unless it was the transaction's latest event. That gap is part of the
  "window before anchoring" the threat model already concedes.

### 5.6 Per-transaction hash chain under concurrency

`prev_event_hash` is the latest `leaf_hash` for the same `transaction_id`, or 32 zero bytes for the
first event. Two concurrent writers could otherwise fork the chain. The helper takes
`pg_advisory_xact_lock(hashtextextended(transaction_id, 0))` before reading the previous hash, so
writers to one transaction serialize until commit. This is a no-op on SQLite, which has a single
writer. A partial unique index on `(transaction_id, prev_event_hash)` is the backstop.

Several events for one transaction inside one DB transaction (T7 writes 3 transitions, a refund
queue audit row, and so on) chain correctly because the helper flushes each insert, so the next
SELECT sees it. The global chain (`transaction_id IS NULL`) uses one fixed lock key.

### 5.7 What verifiers compare against current rows

Mutable rows are not anchored. The verifiers check them against the last anchored fact:

- `transactions.status` must equal the `to` of the last anchored transition, or the `status` of a
  later `backfill_snapshot` (as the brief requires).
- `transactions.amount` must equal the `transaction_created` amount. This is a cheap extra check
  (D5).
- `disputes.admin_decision`, `split_ratio`, `admin_reason` and `decided_by_user_id` must match the
  `admin_decision` event, when one exists.
- Full audit only: `escrows.funded_amount`, `released_amount` and `refunded_amount` must equal the
  totals implied by anchored ledger rows and `econfirm_funding_confirmed` audit amounts (P4, P5).

### 5.8 Completeness enforcement (the A3 test)

- **Ledger:** a test-only `after_flush` listener asserts that every new `LedgerEntry` has an anchor
  event with a matching `source_id` in the same flush. A static test asserts that `LedgerEntry(` is
  constructed only in `ledger.py`.
- **Audit:** the same pattern for `MoneyAuditEvent`.
- **Transitions:** a `before_flush` listener on the app's session factory inspects every dirty
  `Transaction` whose `status` history changed. It checks that the last pending transition event for
  that transaction has the new status as `to`.
  - Strict mode (`ANCHOR_STRICT_EVENTS`, on for the whole test suite) raises, so all 777 existing
    tests act as the completeness test, and a new code path that skips the helper fails CI.
  - In production it never raises. It logs an error and records a fallback transition with
    `actor_type="unknown"` and `reason="unrecorded_transition"`. History stays complete, and a
    money operation never fails because of a missed hook.

### 5.9 Backfill

`python -m app.cli anchor-backfill` (the existing operator CLI) does the following, in this order:

1. For every `ledger_entries` and `money_audit_events` row that has no event yet (deduplicated by
   `source_table`/`source_id`), record a source-backed event with `"backfilled": true`. Rows are
   ordered by their original timestamp, then id.
2. For every transaction, record one `backfill_snapshot` of its current status and dispute fields.
3. Write one global `backfill_completed` marker. A second run refuses unless `--resume`.

It does **not** reconstruct individual transitions from `notifications.transition_history`. Those
payloads are per recipient, incomplete (the payment, dispatch, arrival and OTP-confirm
transitions, T2 to T6, carry none) and mutable, so presenting them as attested history would be dishonest. The snapshot
attests "this was the state when backfilled", which is all the backfill can truthfully claim.

Deployment: run the backfill right after the A3 migration. Live events recorded before it simply
come earlier in their chains. The snapshot is then the newest fact, and its status equals the
current status, so the status check holds.

### 5.10 Dependencies

- A2: `pycryptodome` for keccak256. JCS is about 60 lines in-house: inputs are restricted to str,
  int, bool, None, list and dict with ASCII keys. It is pinned by the golden vectors, which the JS
  side cross-checks with an independent implementation.
- A5: `web3` 7.x for signing, nonce and fee handling, and receipts. It is added only in A5.
- Contract (A4): I recommend a single immutable `anchorer` set in the constructor, with no owner.
  An owner adds nothing when rotation already means "deploy a new registry and record the
  switch-over" (A7 runbook), and it removes one key that could be lost (D6).

---

## 6. Facts currently updated in place (design problems)

| # | Where | What mutates | Impact on anchoring | Proposed handling |
| --- | --- | --- | --- | --- |
| P1 | `transactions.status`, `payout_status` and timestamps | status in 25 places; `payout_status` recomputed by `refresh_transaction_payout_status` | the status itself cannot be anchored | anchor transitions (3.3). `payout_status` is derived from attempts and is covered by payout audit events, so it is not anchored |
| P2 | `disputes.status`, `escalated_at`, `resolved_at`, `seller_resolution_action/notes`, `split_ratio` (reset to NULL, then set), `admin_decision`, `admin_reason`, `decided_by_user_id` | row rewritten through the case's life | no history exists | dispute events and the admin decision event become the record (3.4, 3.5) |
| P3 | `disputes.evidence` JSONB | rewritten with `buyer_reconfirm_rejection_count`, `buyer_reconfirm_notes` (overwritten each round), `buyer_confirmed_resolved` / `seller_confirmed_resolved`, `escalation` | earlier reconfirm notes are lost today | captured per event (as hashes) from now on; `evidence` itself is not anchored |
| P4 | `escrows.funded_amount`, `released_amount`, `refunded_amount` | running totals updated next to ledger postings | totals are derived | not anchored; the full audit cross-checks them (5.7) |
| P5 | eConfirm funding | `funded_amount = amount_total` set with **no ledger posting**; the Daraja-style callback (T2) locks with no funding posting at all | for these paths the ledger is not the full money story | the anchored facts are the `econfirm_funding_confirmed` audit row and the T2/T3 transition. Adding a funding posting would change money behaviour, so it is out of scope (D8) |
| P6 | `payout_attempts`, `collection_attempts` | `outcome`, `provider_reference`, snapshots; the simulated provider even overwrites `amount` and `idempotency_key` on an existing attempt | attempts are working state | not anchored; their meaningful outcomes are anchored through payout audit rows and ledger rows |
| P7 | `EscrowLedger.post_movement` | commits mid-operation | ledger rows plus their events are atomic; the following audit row and total updates are a second commit | unchanged in M21; the full audit reports a ledger posting with no matching payout audit |
| P8 | `transactions.custody_provider`, `escrows.custody_provider` | set to simulated after a definite eConfirm rejection | not a money fact | kept out of `transaction_created`; the eConfirm audit rows anchor the selection |
| P9 | `notifications.payload.transition_history` | the only historical transition record today; per recipient, partial, and its row is mutable | cannot be trusted as a source | not used, including by the backfill (5.9) |
| P10 | `disputes.status` drift | `buyer_sent_back` and `canceled` are never set; T9, T10, T18 and T19 change the transaction without moving the dispute status | anchored dispute events will show what happened, not the drifting column | no change in M21; see section 7 |

---

## 7. Pre-existing bugs found (outside M21)

1. **Seller-received timeout strands the case (money stuck).** T19 sets the transaction to
   `escalated_admin_review` but leaves the dispute `open`. Both `force_resolve_dispute_case` and the
   admin escalation queues (`dispute.list_escalated_disputes`, `admin_cases`) require
   `dispute.status == escalated_admin_review`. No admin can resolve the case, and the escrow is never
   paid out or refunded. Proposed fix: T19 also sets the dispute to escalated with `escalated_at`.
   This should be a separate issue and PR, merged before A3 so that A3's
   `dispute_escalated` event matches the row.
2. **T18 (no send-back timeout) releases the money but leaves the dispute `open`.** The case shows
   as open forever. Lower impact, and it can go in the same fix.

---

## 8. Decisions needed before A2

| # | Question | Recommendation |
| --- | --- | --- |
| D1 | Accept the `id` (insert order) plus batcher-assigned gapless `seq` deviation (5.5)? | Yes |
| D2 | Route all 25 status writes through `transition_transaction`, with the strict `before_flush` guard in tests and the logged fallback in production (5.8)? | Yes |
| D3 | Backfill as source rows plus a per-transaction `backfill_snapshot`, with no transitions reconstructed from notifications (5.9)? | Yes |
| D4 | Party free text (dispute description, notes, escalation reason) as `keccak` only, with the admin reason verbatim? | Yes |
| D5 | Should the admin's UUID (`admin_id`) appear in payloads served to the buyer and seller? The brief says to include the admin id. It identifies staff to customers, but the UUID carries no name. | Include it, as the brief says |
| D6 | Contract: immutable `anchorer`, no `Ownable2Step` (5.10)? | Yes |
| D7 | Add immutability triggers to `ledger_entries` and `money_audit_events` too? | Not in M21; note as future work |
| D8 | Leave eConfirm funding without a ledger posting (P5)? | Yes for M21; file a follow-up if wanted |
| D9 | File and fix the section 7 bugs as a separate issue before A3? | Yes |
| D10 | LOOP/Pesapal `funding_confirmed` events in the global chain (3.2)? | Yes |
| D11 | Backfill as `python -m app.cli anchor-backfill`, and the worker as `python -m app.jobs.anchoring run-once|run-loop` (no scheduler exists; the brief's entry point is kept)? | Yes |
