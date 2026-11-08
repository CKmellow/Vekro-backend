# Frontend verification contract (M21 A7)

How the web app checks an order's tamper-evident record and a seller's track record **in the
browser**, without trusting Vekro's server for the verdict. The algorithms are pinned by
`tests/fixtures/anchoring/vectors.json`. The frontend test suite must pass every vector, including
every `must_fail` case.

## 1. Build-time configuration (pinned, never taken from the API)

| Variable | Meaning |
| --- | --- |
| `VITE_ANCHOR_CHAIN_ID` | 84532 (Base Sepolia), 8453 (Base), or 31337 (Anvil, development only) |
| `VITE_ANCHOR_REGISTRY_ADDRESS` | the AnchorRegistry address from `contracts/deployments/<network>.json` |
| `VITE_ANCHOR_RPC_URL` | a public JSON-RPC endpoint for that chain |

The API's `chain.registry_address` is informational only. If it differs from the pinned address,
show the record as **UNAVAILABLE** ("can't be checked right now"), never as verified. A server that
could choose the registry could choose its own roots. When the variables are unset, hide the panel
entirely.

The CSP `connect-src` must allow `VITE_ANCHOR_RPC_URL`.

## 2. Reading a root

`rootOf(uint256)` is the single call needed. JSON-RPC:

```json
{"jsonrpc":"2.0","id":1,"method":"eth_call",
 "params":[{"to":"<registry>","data":"0xdc299d69<batch_id as 32-byte big-endian hex>"},"latest"]}
```

The result is a 32-byte root, or zero when the batch is not anchored, which means pending, not
tampered. Cache roots per batch id for the page's lifetime. Also check `eth_chainId` once and treat
a mismatch as UNAVAILABLE. Other selectors: `batchCount()` `0x06f13056`, `getBatch(uint256)`
`0x5ac44282`.

## 3. Endpoints

| Endpoint | Who | Use |
| --- | --- | --- |
| `GET /transactions/{id}/verification` | buyer, seller, admin | the order's bundle (section 4) |
| `GET /sellers/{id}/track-record` | public | sale events with proofs (section 6) |
| `GET /anchoring/batches/{id}` | public | a batch's transaction hash and block, for a "view on explorer" link |

Rate limits apply to the public endpoints (HTTP 429 with `Retry-After`).

## 4. Bundle shape

```text
{ version: 1, transaction_id, reference,
  chain: { chain_id, network, registry_address },
  current: { status, amount },                 // live values, amount as "1234.00"
  events: [ Event ],                           // the order's chain, oldest first
  linked_events: [ Event ],                    // global provider events the chain links to
  server_time }
Event = { event_type, schema_version, payload,  // canonical JSON text, hash it exactly as given
          salt, prev_event_hash, leaf_hash,     // 0x-prefixed 32-byte hex
          created_at,
          anchor: null | { batch_id, seq, leaf_index, leaf_count, batch_root, batch_status,
                           tx_hash, block_number, proof: [{ sibling, side: "left"|"right" }] } }
```

## 5. Algorithm (order record)

All hashes are **keccak256** (Ethereum), not SHA3-256.

1. Walk `events` in order with `prev = 0x00…00`:
   - `payload` must equal its own canonical form (RFC 8785: sorted keys by UTF-16 code units, no
     whitespace, no floats). Otherwise the verdict is TAMPERED.
   - `payload.transaction_id` must be the bundle's `transaction_id`, or null.
   - `prev_event_hash` must equal `prev`. Otherwise TAMPERED (chain broken).
   - `leaf = keccak256(0x00 ‖ salt ‖ prev_event_hash ‖ utf8(payload))` must equal `leaf_hash`.
     Otherwise TAMPERED.
   - With no `anchor`, the event is pending. Otherwise read `root = rootOf(anchor.batch_id)`. A
     zero root means pending. Otherwise fold the proof: for each step,
     `h = keccak256(0x01 ‖ sibling ‖ h)` when the sibling is on the left, or
     `keccak256(0x01 ‖ h ‖ sibling)` when it is on the right. The sides must match the RFC 6962
     shape for `leaf_index` in a tree of `leaf_count` (vectors `trees[*].proofs`). The result must
     equal `root`; otherwise TAMPERED.
   - `prev = leaf_hash`.
2. For each `global_event_linked` event, find the `linked_events` item whose `leaf_hash` equals
   `payload.global_leaf_hash`. A missing item is TAMPERED. Check its leaf and proof the same way.
   Linked events are extra evidence and do not change the counts.
3. The last `transaction_transition.to` (or `backfill_snapshot.status`) must equal
   `current.status`. The `transaction_created.amount` (or snapshot amount) must equal
   `current.amount`. Either mismatch is TAMPERED.
4. Verdict:
   - **TAMPERED** if anything above failed.
   - **UNAVAILABLE** if the RPC failed, the chain id or registry did not match, or `events` is
     empty.
   - **PARTIALLY_PENDING** if any event is pending.
   - **VERIFIED** otherwise.

## 6. Seller track record

`GET /sellers/{id}/track-record` returns `{ seller_id, completed_sales, recorded_sales, sales: [Event] }`.
Each sale's payload is
`{ v, type: "seller_sale_completed", transaction_id: null, at, seller_id, sale_ref, outcome, amount_band }`.
Count a sale as verified only when its leaf recomputes, its proof reaches `rootOf(batch_id)`, and
`payload.seller_id` equals the seller. Sale events sit in the global chain, so `prev_event_hash`
cannot be checked against a neighbour; inclusion is what counts. Show "N verified sales". Never
show more than the verified count, and never claim "no disputes": the proofs cannot show that
nothing was left out.

## 7. Wording (no crypto language for buyers and sellers)

| Verdict | Panel |
| --- | --- |
| VERIFIED | "Tamper-evident record: every step of this order is sealed and checks out." |
| PARTIALLY_PENDING | "Sealing in progress: the latest steps are sealed within a few minutes." |
| TAMPERED | "This record doesn't match its seal. Contact Vekro support." |
| UNAVAILABLE | "We can't check the seal right now. Your order is not affected." |

Use "sealed" and "public record", not "blockchain", "hash", "wallet" or "crypto". A details sheet
may show the technical data (batch, proof length, transaction link) for people who want it, plus a
"Download receipt" that saves the bundle as JSON for `scripts/verify_transaction.py --bundle`.
