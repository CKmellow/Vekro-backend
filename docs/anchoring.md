# Verifiable escrow ledger (M21)

Vekro holds buyers' money. Without anchoring, the only proof of what happened to that money is
Vekro's own database, and whoever controls the database controls the story. M21 changes that:
**Vekro holds the money; a public blockchain holds Vekro to account.** The money itself stays in
M-Pesa and eConfirm. No buyer or seller needs a wallet, a token or any crypto.

Design decisions and the event inventory are in `docs/anchoring-design.md`. This document explains
how the parts fit, what they prove, and what they do not.

## 1. What is anchored

Every money-relevant fact is written to `anchor_events` in the **same DB transaction** as the fact:

| Fact | Event | Chain |
| --- | --- | --- |
| Order created | `transaction_created` | the order's |
| Every status change | `transaction_transition` (from, to, actor, reason) | the order's |
| Every ledger posting | `ledger_entry` | the order's |
| Every money audit row (payouts queued and settled, funding by M-Pesa or card, card payments that failed or did not match, admin actions) | `money_audit` | the order's, or global |
| Dispute steps | `dispute_opened`, `dispute_escalated`, … | the order's |
| Admin decisions | `admin_decision` (decision, reason, split, `admin_ref`) | the order's |
| Completed sales | `seller_sale_completed` (seller, outcome, amount band) | global (public) |

Each event is canonical JSON (RFC 8785) with a random 32-byte salt. Its leaf is
`keccak256(0x00 ‖ salt ‖ prev_event_hash ‖ payload)`, where `prev_event_hash` is the previous leaf
**of the same order**. Every order is therefore its own hash chain. Removing or editing any event
in the middle breaks every hash after it.

Party free text is stored as a keccak hash only, and admins appear only as an HMAC `admin_ref`.
Payout destinations appear as an HMAC `destination_ref`.

## 2. How it reaches the chain

```
money operation ──(same DB txn)──▶ anchor_events (append-only, hash-chained)
                                          │
              python -m app.jobs.anchoring run-loop  (one worker, one batch in flight)
                                          │  gapless seq, RFC 6962 keccak Merkle root
                                          ▼
                     AnchorRegistry.anchor(batchId, root, prevRoot, leafCount, firstSeq, lastSeq)
                                     on Base (L2); Anvil locally
```

- The **AnchorRegistry** (`contracts/src/AnchorRegistry.sol`) has no owner, no proxy, no overwrite
  and no delete. It accepts only the next batch id, the previous root, and a sequence range that
  starts right after the last one. A batch can never be replaced, skipped or reordered.
- The **worker** (`app/services/anchoring/batcher.py`) writes each signed transaction to the
  database before broadcasting it. Before every submission it checks the registry to see whether
  the batch is already there. A stuck transaction gets a fee bump at the same nonce, and a batch
  dropped by a reorg is resubmitted with identical values. If the registry ever holds a different
  root than the database, the worker **halts** (audit event plus critical log) and anchors nothing
  more until a person looks (runbook RB-8).
- Money never waits on the chain to be *recorded*. An RPC outage leaves every escrow untouched;
  events simply wait for the next batch.

## 3. Where the chain is on the money path

With `ANCHOR_GATE_PAYOUTS=true` (required in production with anchoring), **no payout or refund is
sent to a rail until the decision that authorised it is anchored and confirmed.** That decision is
the `payout_<kind>_queued` audit event. Before paying, the executor:

1. recomputes the event's leaf and checks that its payload names this attempt, order and amount,
   and that the destination matches the anchored `destination_ref`;
2. checks that the event's batch is CONFIRMED and still complete;
3. recomputes the Merkle proof and compares it with the root **read from the registry**, not the
   database.

If the event is not anchored yet, or the chain is unreachable, the payout waits. If anything
disagrees, for example an amount or destination edited after the decision, or a different root
on-chain, that payout **halts** and is audited (RB-9). An insider who edits the database cannot
move money without the edit failing against a public commitment made before it.

## 4. Who can verify what

| Who | How | Checks | Trusts |
| --- | --- | --- | --- |
| Buyer or seller | "Verified record" panel on the order page (in the browser), or a downloaded receipt and `scripts/verify_transaction.py` | their order's chain, every proof against the registry root, live status and amount against the last anchored facts | the registry address built into the web app, and a public RPC |
| Anyone | a seller's Trust Link: `GET /sellers/{id}/track-record` | each completed sale's proof against the registry | the same |
| Operators and auditors | `scripts/audit_anchoring.py` | every batch (count, gapless seq, root, on-chain root), every chain, every source row against its anchored payload, statuses, admin decisions, escrow totals against the ledger, sales against orders | the database snapshot they audit |

Verdicts: **VERIFIED**, **PARTIALLY_PENDING** (intact, some events not anchored yet), **TAMPERED**,
**UNAVAILABLE** (registry unreachable). The server never states a verdict. It serves data
(`GET /transactions/{id}/verification`), and the verifier reads each root from the chain itself.

## 4a. Personal data in payloads

Every payload of an order is served, with its salt, to **both** parties
(`GET /transactions/{id}/verification`). A party can only check a hash over the whole payload, so
nothing can be stripped from one party's copy. The rule is therefore that no payload names a
person except by UUID or keyed reference:

| Event type | Personal data, and how it is kept out | Buyer sees | Seller sees |
| --- | --- | --- | --- |
| `transaction_created` | buyer and seller UUIDs only | same | same |
| `transaction_transition` | actor UUID (admins as `actor_ref`); `door_rejection_reason` is a code | same | same |
| `dispute_*` | party-written text as `text_keccak` only (D4) | same | same |
| `admin_decision` | admin as `admin_ref`; the reason is the admin's own wording, shown to both parties anyway | same | same |
| `ledger_entry` | descriptions and idempotency keys use Vekro IDs; the simulated rail's references appear only as a SHA-256 digest | same | same |
| `money_audit` | payout destinations as `destination_ref`; provider references as `provider_ref`; webhook dedupe keys as `dedupe_ref` (#241) | same | same |
| `global_event_linked` | `provider_ref` (#241) | same | same |
| `seller_sale_completed` | public by design: seller, outcome, amount band; no buyer, no order id (`sale_ref`) | - | - |

**`provider_ref` (#241).** Provider references are not safe to show: Daraja's `CheckoutRequestID`
ends with the payer's subscriber number (`docs/daraja-observed.md`). `money_audit_events`, its
`correlation_id`, log lines, notifications and every payload hold
`provider_ref = hex(HMAC-SHA256(key, "provider:" + raw))` with the `ANCHOR_ADMIN_REF_SECRET` keys
(`app/services/anchoring/provider_ref.py`). The raw value stays only in the private tables that
talk to providers (`payment_prompts`, `collection_attempts`, `payout_attempts`,
`provider_events`, the escrow's custody columns). Admins can still search the money audit by the
provider's own reference: `GET /admin/audit/money-events?provider_reference=` hashes it under
every configured key.

The test suite enforces this: `tests/conftest.py` inspects every anchor event any test writes and
fails on an e-mail address, a phone number (`+254`, `254`, `07`, `01`) or any run of 9 or more digits
(`tests/pii_guard.py`).

**Rows written before #241.** `anchor_events` and `money_audit_events` are append-only (database
triggers), and that stays so: old rows are not rewritten. Nothing has been anchored on a public
chain, and every database so far is a development one, so reset instead:

```bash
# local development only: drops all data
.venv/bin/alembic downgrade base && .venv/bin/alembic upgrade head
# restart anvil (its state is in memory) and redeploy the registry
anvil --port 8545 &   # in its own terminal
make anvil-deploy
```

If a non-development database ever holds pre-#241 rows, stop and ask the maintainer: those rows
cannot be edited, and the right fix (re-anchoring, or a documented exception) is a decision.

## 4b. Proof of liabilities (M24 B2)

`python -m app.cli liabilities-snapshot` (the scheduler runs it every few minutes) puts every open
escrow into a Merkle sum tree (`app/services/anchoring/sum_tree.py`). An open escrow is one where
funded minus released minus refunded is above zero.

- Each leaf is `keccak256(0x00 || salt || order_commit || amount_cents)`, where
  `order_commit = keccak256("vekro-liability-order:" + order id)`.
- Each node commits to both children's hashes and to the sum of their amounts.
- The root and the total held are anchored as a `liabilities_snapshot` event in the global chain.
  Its payload is the snapshot id, root, total and leaf count, with no order, buyer or per-order
  amount.

Each buyer can fetch `GET /transactions/{id}/liabilities-proof`: the order's leaf, its salt, the sum
proof and the anchored event with its batch proof. The order page then says "Your KES X is included
in Vekro's published total of held money", after the browser has:

- recomputed `order_commit` from the order id, so the leaf is this buyer's and cannot be shown to
  two buyers;
- folded the proof to the root, refusing any amount or sum below zero or above 2^64 - 1. A negative
  or wrapping sibling is how a total would be quietly lowered;
- compared the root and total with the anchored payload;
- checked the event's batch proof against the root on the AnchorRegistry.

**What it proves.** The published total includes every buyer's held amount: a buyer left out, or
counted for less, finds out. Once a snapshot is anchored, its total cannot be quietly understated:
editing any amount or dropping any order changes the root, and the old root is on the chain.

**What it does not prove.**

- That the money exists. Liabilities are what Vekro owes; whether the custody provider holds that
  much is the reserves side (next section).
- That buyers who never check their proof are counted correctly. The guarantee is only as strong
  as the share of buyers who look.
- Anything between snapshots: an order paid and released between two runs never appears.

Golden vectors shared with the frontend: `tests/fixtures/anchoring/sum_tree_vectors.json`, including
must-fail cases for a tampered amount, an omitted escrow, an understated total, negative and
overflowing sums, and a wrong side.

## 4c. Reserves reconciliation (M24 B2)

`python -m app.cli reserves-reconcile` (the scheduler runs it on an interval) compares, for each
custody provider, what Vekro owes buyers with what the provider says it holds. The result is
anchored as a `reserves_reconciliation` event in the global chain: provider, method, status,
liability, attested total, difference and escrow count, with no orders or buyers.

| Provider | Method | Possible statuses |
| --- | --- | --- |
| eConfirm | no balance endpoint: each open eConfirm escrow's current status is asked for; only escrows eConfirm reports funded count | `covered`, `shortfall`, `incomplete` (some answers missing), `unavailable` (not configured) |
| Daraja (M-Pesa) | the merchant's own account; Account Balance was not verified in the sandbox | `unavailable` |
| Pesapal | the merchant's own account; Pesapal has no balance API | `unavailable` |
| Simulated | no real money | `not_applicable` |

A definite **shortfall** is an admin alert:

- an anchored `reserves_shortfall` money audit event (`GET /admin/audit/money-events`);
- a `reserves_shortfall` warning log line;
- `reserves_shortfall: true` on `GET /admin/anchoring/status`, which also shows the latest result per
  provider and the latest liabilities snapshot.

A missing or unreachable answer is never treated as a shortfall. See runbook RB-12.

**What it proves.** That on a given date Vekro published what each provider attested against what
it owed, and cannot later change either figure: both are on the chain. A shortfall cannot be
quietly hidden after the fact.

**What it does not prove: that the provider's figures are true.** Provider answers are unsigned,
so Vekro could misreport them, and nothing on the chain can tell. For Daraja and Pesapal there is
no attested figure at all, because the money sits in the merchant's own account. Closing this gap
needs a provider that signs its balance statements, or an independent auditor.

## 5. Threat model

**Caught:**

- Editing a ledger row, audit row or event payload after it was anchored (leaf, proof or full
  audit).
- Deleting an event from the middle of an order's history (the chain breaks).
- Deleting an order's latest event (seq gap and batch count; full audit).
- Changing an order's status, amount or admin decision without a recorded event (status, amount
  and decision checks).
- Rewriting history wholesale and re-anchoring it: the registry refuses to replace a batch, and
  verifiers read the old root.
- Paying out an edited or unauthorised payout (the payout gate).
- Inflating a seller's sales count (each counted sale needs a proof).

**Not caught, by design:**

- **The window before anchoring.** An event deleted or edited before its batch is anchored leaves
  no on-chain trace. This window lasts one worker interval plus confirmations (about a minute on
  Base). The payout gate closes it for money leaving escrow.
- **Lies at write time.** Anchoring proves the record was not changed afterwards. It does not prove
  the record was true when written. The M-Pesa and eConfirm confirmations and the custody controls
  cover that.
- **Omission from the seller track record.** Proofs show that each listed sale is real. They cannot
  show that none were left out, so the page speaks only of completed sales.
- **Custodian balances.** The chain does not prove the money is still at M-Pesa or eConfirm. That
  needs the custodian's own attestation (a proof-of-reserves follow-up).

The append-only triggers stop bugs and casual edits, but not a table owner or superuser. The
anchored roots are what catch those.

## 6. Why a public chain

A private log signed by Vekro proves nothing to someone who distrusts Vekro, because Vekro could
re-sign a different history. A public chain gives a timestamped commitment that nobody, Vekro
included, can withdraw or replace, and that anyone can read without asking Vekro.

- **Base** is an Ethereum L2: an anchor costs a fraction of a cent, blocks come every ~2 seconds,
  it is EVM, and its data is secured by Ethereum. Base Sepolia (84532) is used for testing, and
  Anvil (31337) locally and in CI.
- **Only roots go on-chain.** Payloads, salts and proofs stay in Vekro's database and are served
  only to the parties involved, so no personal data is ever published. The exception is completed
  sales, which are public by design and carry no personal data.

## 7. Operating it

| Task | How |
| --- | --- |
| Run the worker | `python -m app.jobs.anchoring run-loop` (exit 3 = halted) |
| See progress | `GET /admin/anchoring/status` |
| Local chain | `anvil --port 8545 && make anvil-deploy` |
| Verify one order | `scripts/verify_transaction.py --transaction-id <uuid>` |
| Full audit | `scripts/audit_anchoring.py` |
| Show tampering (never in production) | `scripts/demo_tamper.py <scenario> --reference VK-… --i-understand-this-corrupts-data` |
| Settings | `.env.example`, M21 block |
| Runbooks | `docs/runbooks.md` RB-7 to RB-10 |
| Demo | `docs/demo-script.md` Scenario 6 |
| Frontend | `docs/frontend-verification-contract.md` |

**Never deploy the registry to Base Sepolia or Base, or send any public-network transaction,
without the maintainer's explicit go-ahead and a funded key from them.**
