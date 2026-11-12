# Demo script: the presentation run (10 to 12 minutes)

The live run on **https://app.vekro.me** (VM `vekro-vm`, sandbox rails, Base Sepolia record), as of
2026-11-11. The local developer scenarios that came before are kept in the
[appendix](#appendix-local-developer-scenarios).

What to say about money: in the sandbox **no real money moves**, and the app says so on every
payment ("Test payment via … sandbox"). M-Pesa money is collected to Vekro's M-Pesa business
shortcode. Card money is held by Pesapal. Never call Vekro, Daraja or Pesapal a licensed escrow
custodian (`docs/presentation-faq.md`).

## 0. Before the session (demo day)

| When | What |
|---|---|
| Morning | Start the VM (Azure portal → `vekro-vm` → Start, or `az vm start -g vekro -n vekro-vm`). It stops itself at 23:00 EAT. Check `https://app.vekro.me/version.json` and `https://api.vekro.me/health`. |
| Morning | Stage the orders, on a **new series** each demo day (old orders keep their own accounts): `ssh ssh.vekro.me`, then `cd ~/vekro && docker compose -f deploy/compose.yaml -f deploy/compose.vm.yml --env-file deploy/.env exec -T -e DEMO_RESET_CONFIRM=yes api python -m app.cli demo-reset --payer-phone <Cyprian's number> --series <n> --states paid out_for_delivery hold_24h dispute --logins-file /tmp/logins.txt`. Approve each M-Pesa sandbox prompt on the phone (KES 1, about one a minute). Copy the new passwords out of the container's `/tmp/logins.txt` (`docker compose … exec api cat /tmp/logins.txt >> ~/vekro-demo-logins.txt`) into `TEST-LOGINS.local.md`. |
| 30 min before | `… demo-reset … --series <n> --states at_door` (the door window is 1 hour). Note its delivery code. |
| 10 min before | Laptop: phone hotspot (not Strathmore Wi-Fi), four browser profiles or windows logged in: **seller** (`/seller`), **buyer Amina** (`/buyer/orders`), **buyer Brian**, **admin** (`/admin`). AT simulator open ("Launch Simulator"). Phone unlocked, M-Pesa SIM in. `TEST-LOGINS.local.md` open off-screen. |

Accounts and order references for the day: `TEST-LOGINS.local.md` → "Accounts: VM".

## 1. The seller lists an item and shares the Trust Link (1.5 min)

1. Seller window → **Listings** → **New listing** (`/seller/listings/new`).
2. Title `Kiondo basket, small`; Price `1` (say: "KES 1, because this is the sandbox"); add a
   photo; **Publish and get link**.
3. Expected: **"Your Trust Link is ready"** with the link and the share buttons. Copy it (the
   WhatsApp share is what a real seller uses).

Fallback: the reset already made `Kiondo basket, hand-woven sisal` (KES 1). Its `/pay/<id>` link is in
`TEST-LOGINS.local.md`.

## 2. Buyer 1 pays by M-Pesa (sandbox STK to Cyprian's phone, KES 1) (1.5 min)

1. Buyer Amina's window → paste the Trust Link. Expected: the item, **"Paying as Amina Otieno"**,
   Total KES 1.
2. **Pay KES 1 with M-Pesa**. Expected: **"Check your phone and enter your M-Pesa PIN"**.
3. On the phone: the sandbox prompt → PIN. Expected within seconds: **"Payment received"** and
   "Paid, funds held safely", with the note "Test payment via Daraja sandbox: no real money moves".

Fallback (Daraja sandbox down or slow): leave it. The scheduler confirms a late payment by its own
STK query. Then show the staged **paid** order instead (Amina's **My orders**). If Daraja is down
for the whole session, say so and continue on the staged orders. Do not switch rails on the live VM.

## 3. Buyer 2 pays by card (Pesapal sandbox) (1.5 min)

1. Buyer Brian's window → the **Maasai beaded necklace** Trust Link (KES 1,800) → **Card** →
   **Complete payment**. Expected: Pesapal's sandbox page.
2. Card `4761 7390 0101 0010`, 07/28, CVV 123 (Pesapal's published test card; typed by the
   presenter). Expected: back on `/pay/return` → **"Held by Pesapal"** with the sandbox note.

Fallback: Pesapal's page sometimes hangs on "Processing". Say "the provider's page is slow; Vekro
asks Pesapal itself", and wait. The scheduler funds it within about a minute, and **Check again**
on the return page shows it. If Pesapal is down, skip to step 4 and show a card order from the
history (VK-2N6HSK).

## 4. The seller dispatches and marks arrived over USSD (2 min)

1. AT simulator → type the **seller's number with +254** → dial `*384*83579#`.
   Expected: `Vekro seller / 1 My orders / 0 Exit`.
2. `1` → pick Amina's order from step 2 (paid) → `1` Dispatch → PIN `1234`. Expected: done.
3. Dial again → `1` → the same order → `1` Mark arrived → PIN `1234`. Expected: done; the buyer's
   web page moves to **"Your item has arrived"** within one poll (15 s).

Fallback (AT sandbox down): the seller's web window → **Orders** → the order → **Mark as
dispatched** → **Yes, it's dispatched**, then **Mark arrived** → **Yes, mark arrived**. Same
service, same record.

## 5. The buyer accepts at the door (1 min)

The delivery code goes by SMS. In the sandbox it appears in the AT simulator under the **buyer's**
number (the speech-bubble icon), never on a real phone.

1. Amina's window → the order → **Inspect at your door** → **Inspect and confirm** → type the 6
   digits → **Accept item** → **Yes, accept**. Expected: **"All done"**, then **"Complete, seller
   paid"**.

Fallback: use the staged **at the door** order (its code was printed by the reset), or accept over
USSD as the buyer: `1` → the order → `1` Accept (enter code) → the code → "Accepted. Payment goes to
the seller."

## 6. The Verified record says VERIFIED (1 min)

1. Same order page → **Verified record** panel. Expected: "Tamper-evident record: every step of
   this order is sealed and checks out." The seals link to Base Sepolia (BaseScan).
2. Say: "My browser just checked this order's history against the public record. Vekro's server
   was not trusted for the roots."

If the panel says *pending*: the last seal is a minute away. Open the order from step 2 of the
staged set (paid earlier) instead, which is already sealed.

## 7. Tamper demo on a copy (1 min)

1. On the panel: **Download receipt** → `receipt.json`.
2. Laptop terminal (backend checkout):
   ```sh
   cp receipt.json tampered.json   # then change one amount in tampered.json, e.g. "1.00" -> "100.00"
   .venv/bin/python scripts/verify_transaction.py --bundle receipt.json  --rpc-url https://sepolia.base.org --registry 0xD8094faf2B44711dD372A59C288130FA8113E4DB
   .venv/bin/python scripts/verify_transaction.py --bundle tampered.json --rpc-url https://sepolia.base.org --registry 0xD8094faf2B44711dD372A59C288130FA8113E4DB
   ```
   Expected: `VERIFIED`, then `TAMPERED` with the field that no longer matches its seal.
3. Say: "This is what an insider's edit looks like to anyone holding a receipt. The live database
   was not touched."

Fallback (no RPC from the laptop): run the same two commands on the VM (`ssh ssh.vekro.me`).

## 8. Proof of held money (liabilities) (45 s)

1. Amina's staged **paid** order → Verified record panel. Expected: "Your KES 1 is included in
   Vekro's published total of held money (KES Y)".
2. Say: "Vekro publishes one total of everything it holds for buyers. Each buyer can check their
   own amount is in it, without seeing anyone else's. The chain does not prove the money is at the
   bank: that is the reserves check, and it is Vekro's own report" (`docs/presentation-faq.md`).

## 9. The admin resolves an escalated dispute (1.5 min)

1. Admin window → `/admin` → **Escalation queue** → **Open case …** for the staged dispute (Android
   phone, second unit: "serial number on the box does not match").
2. **Review decision** → **Refund buyer** → Reason `Serial mismatch confirmed with both parties` →
   type the `D-XXXX` code shown → **Confirm and execute**. Expected: **"Decision recorded. Payouts
   are on their way."** The decision is itself a sealed money fact.

Fallback: if the queue is empty (the dispute was not staged), show the case history of a past one,
or skip to step 10.

## 9a. Amina opens a store (1.5 min)

One account buys and sells (`docs/accounts-and-modes.md`).

1. Buyer Amina's window → **Home** (`/buyer`). Say: "Amina has only bought so far. Selling is not
   a second account: it is a store on the same one."
2. **Sell on Vekro too** → Store name `Amina's Kiondo`, tick **I accept the Vekro seller terms** →
   **Open my store**.
3. **Where we pay you**: the payout number is pre-filled (her M-Pesa number). **Text me a code**.
   Read the 6-digit code in the AT simulator for that number (sandbox SMS reaches only the
   simulator) and type it. Say: "A store takes no orders until an SMS code proves the payout
   number."
4. Expected: S7 **Create listing** opens. Publish `Small kiondo, KES 1`. The shell now shows
   **Buying | Selling**; switch back and forth.

Fallback: if the SMS does not arrive, show that her Trust Link says **"This store isn't ready
yet"**, and the seller banner **"Finish setup: add payout number (SMS-verified)"**.

## 9b. The seller buys from another shop (1 min)

1. Seller Wanjiru's window → **More** (phone) or the sidebar → **Buying**. Say: "Same account,
   now buying."
2. Open Amina's Trust Link from 9a → **Pay KES 1 with M-Pesa**. Expected: the order is created and
   shows under **Buying → Orders**, never among her store's sales. (The STK prompt goes to her
   demo number, so leave it unpaid.)
3. Open her **own** Trust Link (the Kiondo basket from step 1). Expected: **"This is your own
   listing. You can't buy from your own store."** Say: "The backend refuses it too, and a buyer can't
   pay from the number a store is paid out to, so nobody can buy their own 'verified sales'."

Optional: **Settings → Your store → Close store** on Amina's new store shows that closing waits for
open orders and money, and that stores are never deleted (they are part of the record).

## 10. Dark mode and the installed app (45 s)

1. Any page → the theme switch → **Dark**, then **Same as device**. Expected: the whole app
   switches, with no flash on reload.
2. On the phone: open `app.vekro.me` in Chrome → **Install Vekro** → open it from the home screen.
   Expected: it opens full-screen, without the browser bar, on the same login.

## If the VM is down

`app.vekro.me` shows a Cloudflare error. Start the VM (step 0); the stack starts itself in 1 to 2
minutes. Payments taken while it was off are confirmed from the providers when it starts (24-hour
window). Never take payments while it is off.

---

# Appendix: local developer scenarios

Covers the four capstone scenarios (normal flow, failover, unknown outcome, duplicate funding), which run on simulated custody (Tier 2, application-enforced) in a non-production environment. Scenario 5 covers eConfirm custody.

What to say about custody: simulated custody is enforced by this application and is not regulated. eConfirm is a third-party escrow service. Do not describe Vekro or eConfirm as licensed, regulated, or insured. Point to the `custody_provider` field instead: it says who holds the money.

### Setup (local developer scenarios)

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
