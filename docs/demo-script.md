# Demo Script

Covers the four capstone scenarios: normal flow, failover, unknown outcome, and duplicate funding. All of them run on simulated custody (Tier 2, application-enforced) in a non-production environment.

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

2. Payment confirmation locks the transaction:

   ```bash
   curl -s $API/transactions/payment-callback -H 'Content-Type: application/json' \
     -d "{\"transaction_id\":\"$TXN\",\"result_code\":0}"
   ```

3. Seller dispatches and marks arrival. The arrival creates a delivery OTP; outside production it appears as `delivery_otp_preview` in the buyer's notifications:

   ```bash
   for step in dispatch arrival; do
     curl -s -b seller.jar -X POST $API/transactions/$TXN/$step -H "X-CSRF-Token: $(csrf seller.jar)"
   done
   OTP=$(curl -s -b buyer.jar $API/notifications | jq -r '[.[].payload.delivery_otp_preview // empty][0]')
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
