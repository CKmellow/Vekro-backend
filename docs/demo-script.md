# Demo Script

Covers the four capstone scenarios (normal flow, failover, unknown outcome, duplicate funding), which run on simulated custody (Tier 2, application-enforced) in a non-production environment. Scenario 5 covers eConfirm custody.

What to say about custody: simulated custody is enforced by this application and is not regulated. eConfirm is a third-party escrow service. Do not describe Vekro or eConfirm as licensed, regulated, or insured. Point to the `custody_provider` field instead: it says who holds the money.

## Setup

```bash
cp .env.example .env            # ENVIRONMENT=development, CUSTODY_*_RAIL_PRIORITY=simulated
.venv/bin/alembic upgrade head
make run                        # http://127.0.0.1:8000
API=http://127.0.0.1:8000
```

Session helper: each role uses its own cookie jar; state-changing calls send the CSRF cookie back as `X-CSRF-Token`.

```bash
csrf() { awk '$6=="vekro_csrf"{print $7}' "$1"; }
```

Accounts (admins cannot self-register; promote one account in the database):

```bash
curl -s $API/auth/register -H 'Content-Type: application/json' \
  -d '{"name":"Sam Seller","phone":"+254700000101","role":"seller","password":"demo-pass-123"}'
curl -s $API/auth/register -H 'Content-Type: application/json' \
  -d '{"name":"Bea Buyer","phone":"+254700000102","role":"buyer","password":"demo-pass-123"}'
curl -s $API/auth/register -H 'Content-Type: application/json' \
  -d '{"name":"Ada Admin","phone":"+254700000103","role":"seller","password":"demo-pass-123"}'
psql "$DATABASE_URL" -c "UPDATE users SET role='admin' WHERE phone='+254700000103';"

for who in seller:101 buyer:102 admin:103; do
  curl -s -c ${who%%:*}.jar $API/auth/login -H 'Content-Type: application/json' \
    -d "{\"phone\":\"+254700000${who##*:}\",\"password\":\"demo-pass-123\"}" > /dev/null
done
```

Executor and reconciliation commands are in [runbooks.md](runbooks.md#shared-tooling) (`run_payout_executor_once`, `run_payout_reconciliation`).

---

## Scenario 1: Normal flow

1. Seller lists an item; buyer opens a transaction (`awaiting_payment`):

   ```bash
   LISTING=$(curl -s -b seller.jar $API/listings -H "X-CSRF-Token: $(csrf seller.jar)" \
     -H 'Content-Type: application/json' -d '{"title":"Phone","price":"2000.00"}' | jq -r .id)
   TXN=$(curl -s -b buyer.jar $API/transactions -H "X-CSRF-Token: $(csrf buyer.jar)" \
     -H 'Content-Type: application/json' -d "{\"listing_id\":\"$LISTING\",\"amount\":\"2000.00\"}" | jq -r .id)
   ```

   The response has `"custody_provider": "simulated"`.

2. Payment confirmation locks the transaction:

   ```bash
   # Add -H "X-Payment-Callback-Secret: $PAYMENT_CALLBACK_SECRET" if the secret is set.
   curl -s $API/transactions/payment-callback -H 'Content-Type: application/json' \
     -d "{\"transaction_id\":\"$TXN\",\"result_code\":0}"
   ```

3. Seller dispatches and marks arrival. The arrival creates a delivery code that is sent only to the buyer (notification message, SMS, and `payload.delivery_code`):

   ```bash
   for step in dispatch arrival; do
     curl -s -b seller.jar -X POST $API/transactions/$TXN/$step -H "X-CSRF-Token: $(csrf seller.jar)"
   done
   OTP=$(curl -s -b buyer.jar $API/notifications | jq -r '[.[].payload.delivery_code // empty][0]')
   curl -s -b buyer.jar $API/transactions/$TXN/otp-give -H "X-CSRF-Token: $(csrf buyer.jar)" \
     -H 'Content-Type: application/json' -d "{\"otp_code\":\"$OTP\"}"
   ```

   The transaction becomes `released` and a `tx-release:<txn>` payout intent is queued.

4. Record funding. The payment callback only moves the state machine; it does not post to the escrow ledger. In simulation, fund the escrow with the admin control. The escrow id comes from the queued audit event:

   ```bash
   ESCROW=$(curl -s -b admin.jar "$API/admin/audit/money-events?transaction_id=$TXN&action=payout_release_queued" \
     | jq -r '.items[0].escrow_id')
   curl -s -b admin.jar $API/admin/simulated-custody/collections/force-complete \
     -H "X-CSRF-Token: $(csrf admin.jar)" -H 'Content-Type: application/json' \
     -d "{\"escrow_reference\":\"sim-escrow-$ESCROW\",\"amount\":\"2000.00\",\"phone_number\":\"+254700000102\"}"
   ```

5. Run the executor, then show the trail:

   ```bash
   curl -s -b admin.jar "$API/admin/audit/money-events?transaction_id=$TXN" | jq '.items[] | {action, actor_type, rail_name, provider_reference, correlation_id}'
   ```

   Expect `payout_release_queued`, `simulated_collection_forced`, and `payout_release_succeeded`, all sharing the transaction in their correlation IDs.

---

## Scenario 2: Failover (rail breaker)

Main ships breaker-based routing: an open rail is skipped for new payout intents. Demonstrate it with the deterministic tests, then show the operator view:

```bash
.venv/bin/pytest tests/test_rail_breaker.py -v
curl -s -b admin.jar $API/admin/rails/health | jq '{custody_mode, holds_funds_structurally, payout_priority, rails}'
```

Talking points:

- 3 consecutive failures open the breaker; after 300 s it is `half_open`; one success closes it.
- `test_open_breaker_is_excluded_from_payout_routing` shows `loop,simulated` routing to `simulated` while `loop` is open.
- Live rails stay blocked outside production even if they are first in priority (`tests/test_custody_settings.py -k live_payout`).
- Attempts already in flight keep their rail; recovery is described in [runbooks.md](runbooks.md#rb-1-rail-outage).

---

## Scenario 3: Unknown outcome

1. Restart the API with `SIMULATED_PAYOUT_DEFAULT_SCENARIO=timeout`, then repeat Scenario 1 steps 1–4.
2. Run the executor. The attempt is UNKNOWN with a provider reference; `payout_status` is `unknown`.
3. Run the executor again: it inquires with the **same** provider reference and does not send a second payout.
4. Wait a minute, then run reconciliation with `unknown_age_minutes=1`. The admin receives a `Payout reconciliation required` notification:

   ```bash
   curl -s -b admin.jar $API/notifications | jq '.[] | select(.payload.alert_key // "" | startswith("payout-unknown"))'
   ```

5. Show `payout_release_unknown` in the money audit and walk through [RB-2](runbooks.md#rb-2-unknown-payout-resolution).

Variant: `SIMULATED_PAYOUT_DEFAULT_SCENARIO=out_of_order` reports UNKNOWN first and settles on a later executor run, with one ledger posting.

---

## Scenario 4: Duplicate funding

1. Replay the payment callback from Scenario 1 step 2. The response has `duplicate: true` and the state does not change.
2. Replay the force-complete call from Scenario 1 step 4. The response has `idempotent_replay: true`; the escrow's funded amount is unchanged because the ledger idempotency key (`sim-funding:<provider_reference>`) is already posted.
3. Webhook replays and forgeries:

   ```bash
   .venv/bin/pytest tests/test_loop_collection_webhook.py -v -k "idempotent or forged or stale"
   ```

   A signed callback replay is deduped through `provider_events`; a forged or stale callback is rejected with 401 and cannot occupy the legitimate dedupe key.

---

## Scenario 5: eConfirm custody (Tier 1)

Offline, with no money and no network, show the routing and safety rules:

```bash
.venv/bin/pytest tests/test_econfirm_funding_flow.py tests/test_econfirm_failure_matrix.py -q
```

Talking points:

- With `ECONFIRM_ENABLED=false` (the default), nothing changes: the transaction is `simulated` and uses the existing payment path.
- When enabled, an eligible transaction (KES 100–1,000 by default) is opened on eConfirm. The buyer gets eConfirm's M-Pesa prompt, and the response says `"custody_provider": "econfirm"`.
- Only the poller marks it funded and `locked`. Replaying the Scenario 1 payment callback against an eConfirm transaction does nothing.
- If eConfirm rejects the open, the transaction falls back to simulated and says so. After a successful open, it never falls back. An unclear create is flagged for reconciliation ([RB-4](runbooks.md#rb-4-econfirm-funding-poller-and-unconfirmed-escrows-m20)) and no prompt is sent.
- Every HTTP failure is classified as definite or unknown, and as breaker-relevant or not (the failure matrix).

Live, which moves real money and needs explicit sign-off first: set `ECONFIRM_ENABLED=true` with the API key, `ECONFIRM_FALLBACK_EMAIL`, and `ECONFIRM_CODE_ENCRYPTION_KEY`, create a KES 100 listing, buy it, approve the prompt, and run the RB-4 poller until the transaction is `locked`. Or run the scripted round trip, which funds KES 100 and reverses it to the payer:

```bash
RUN_ECONFIRM_LIVE_TESTS=1 ECONFIRM_LIVE_MONEY_CONFIRM=KES100 \
ECONFIRM_LIVE_PAYER_PHONE=+2547XXXXXXXX ECONFIRM_LIVE_RECEIVER_PHONE=+2547XXXXXXXX \
.venv/bin/pytest -m live -k econfirm -s
```

## Scenario 6: Tamper-evident ledger and anchor before payout (M21)

**Shows**: every money fact sealed on a public chain. A payout waits for its seal, and tampering
by someone with database access is caught by the buyer, by the full audit, and by the payout gate.

Setup (local, no public network):

```bash
anvil --port 8545 &
make anvil-deploy                       # registry 0x5FbDB2315678afecb367f032d93F642f64180aa3
# .env: ANCHOR_ENABLED=true ANCHOR_RPC_URL=http://127.0.0.1:8545 ANCHOR_CHAIN_ID=31337
#       ANCHOR_REGISTRY_ADDRESS=0x5FbDB2315678afecb367f032d93F642f64180aa3
#       ANCHOR_PRIVATE_KEY=<anvil key 0>  ANCHOR_ADMIN_REF_SECRET=<32+ chars>
#       ANCHOR_GATE_PAYOUTS=true ANCHOR_MIN_CONFIRMATIONS=1
make run &
.venv/bin/python -m app.jobs.anchoring run-loop &
```

1. **Normal order, sealed.** Run Scenario 1 up to the buyer's delivery-code confirmation. The
   payout is queued but **held**: `GET /admin/anchoring/status` shows
   `payouts_waiting_for_anchor: 1`. Within one worker interval the batch is confirmed. The next
   payout executor run pays, and the buyer's order page shows "Tamper-evident record: every step
   of this order is sealed and checks out."
2. **Independent check.** Download the receipt from the order page, then run
   `.venv/bin/python scripts/verify_transaction.py --bundle receipt.json` → `VERIFIED`. The roots
   come from Anvil, not from Vekro.
3. **Insider edits history.** Stop the API, worker and scheduler, then
   `ANCHOR_ENABLED=false ANCHOR_GATE_PAYOUTS=false ANCHOR_REGISTRY_ADDRESS= .venv/bin/python
   scripts/demo_tamper.py edit-ledger --reference VK-… --from-db <db>
   --i-understand-this-corrupts-data`. It copies the database to `<db>_tamper_copy`, says so, and
   edits only the copy (`docs/anchoring.md` 8.3). Then `scripts/audit_anchoring.py --database
   <db>_tamper_copy` → `TAMPERED: source_row_changed`. Repeat with `change-status` and
   `--from-db <db>_tamper_copy`: the script reports `status_mismatch`. To show the order page
   saying "This record doesn't match its seal", start the API on the copy (both `DATABASE_URL` and
   `ALEMBIC_DATABASE_URL` set to it, anchoring off); the browser still reads the roots from the
   chain. Drop the copy afterwards.
4. **Insider redirects a payout.** Create a second order up to release. Before the executor runs,
   change the payout attempt's `request_snapshot.destination_phone` in the database. The executor
   **halts** the payout (`anchor_mismatch_halted`, audited once), and no money moves.
5. **Seller reputation.** Open the seller's Trust Link: "N verified sales". Each count is a sale
   whose proof the browser checked against the registry.

Talking points: only Merkle roots go on-chain, so no personal data is published. Each anchor costs
a fraction of a cent on Base. The registry cannot overwrite or reorder history. The honest limits
are in `docs/anchoring.md` section 5.

## Scenario 7: A buyer accepts at the door over USSD (M23)

Shows that a feature-phone buyer acts on the same order as the web app, through the same rules.

1. Seed the demo (non-production only), with the numbers you will type into the simulator:
   `.venv/bin/python -m app.cli seed-ussd-demo --buyer-phone +2547XXXXXXXX --seller-phone +2547YYYYYYYY --pin 1234`.
   Note the at-the-door order's reference and delivery code.
2. Run the backend and `make tunnel`. The AT sandbox channel's callback must be
   `https://<tunnel host>/ussd/africastalking/<USSD_CALLBACK_TOKEN>` (docs/ussd.md).
3. In a browser, sign in to the web app as the buyer (the seed printed the password) and open
   that order: "At your door".
4. In AT's simulator ("Launch Simulator"), **type the buyer's number with +254**. The country
   selector defaults to +93, and a number without +254 silently gets AT's generic reply. Dial
   `*384*83579#`, then: `1` My orders, the order's line, `1` Accept, the delivery code.
5. The simulator answers "Accepted. Payment goes to the seller." Within one poll (15 seconds) the
   web order page shows the order complete. The order's verified record has the same events as a
   web acceptance: USSD went through the same service.
6. Optional: dial as the seller and mark the "on the way" order arrived (PIN 1234). The buyer gets
   the delivery code by SMS, and the web page moves to "At your door".
