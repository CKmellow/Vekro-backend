# Demo modes: which rail takes each payment, and where the money sits

Vekro picks the collection rail from configuration alone. Print the current choice with:

```bash
.venv/bin/python -m app.cli payment-rails
```

The backend also logs it at startup (`collection_rails ...`).

## Profiles

| Profile | Settings | M-Pesa | Card | USSD | Where the money sits |
|---|---|---|---|---|---|
| **Default** (development, tests, CI) | nothing set | simulated stub: no prompt is sent; the order is paid via `POST /transactions/payment-callback` | not configured | not offered yet (M23) | nowhere: Vekro's ledger rows only (`ESCROW_HELD`) |
| **Class demo** | `DARAJA_ENABLED=true` + `DARAJA_*`, `make tunnel` | Daraja sandbox STK push to the buyer's real phone; funded only after Vekro's own STK query confirms | Pesapal hosted checkout (with `PESAPAL_*`, `PESAPAL_IPN_ID`, `FRONTEND_URL`); funded only after Vekro's own status query reports COMPLETED for the exact amount | not offered yet (M23) | the sandbox test shortcode 174379 collects; Vekro's ledger records it as held. Card money lands in the merchant's Pesapal account. Both are **collection, not custody**: no third party holds it in escrow |
| **Real custody** | `ECONFIRM_ENABLED=true` + key + `ECONFIRM_CODE_ENCRYPTION_KEY` | eConfirm (third-party escrow) for amounts in `ECONFIRM_MIN_AMOUNT`-`ECONFIRM_MAX_AMOUNT`; other amounts fall back to Daraja if enabled, else simulated | not offered yet | not offered yet | eConfirm holds the money until release or reversal |

Notes:

- Simulated stays the default. Nothing real is called unless a profile is switched on.
- eConfirm, when usable, is always tried first, so turning Daraja on never takes orders away from
  real custody.
- `CUSTODY_COLLECTION_RAIL_PRIORITY` (LOOP, Pesapal) drives the custody registry and its tests,
  not the buyer's checkout. Pesapal reaches buyers through M22 P2.

## Class demo: card payments (Pesapal)

1. Set `PESAPAL_ENABLED=true`, the sandbox base URL and the public demo keys
   (`developer.pesapal.com/api3-demo-keys.txt`), and `FRONTEND_URL`.
2. Register the IPN once. It stores `PESAPAL_IPN_ID` in `.env`:
   `python -m app.services.custody.pesapal_ipn_registration --callback-url https://<host>/payments/pesapal/ipn --notification-type GET`
   (export the `PESAPAL_*` values first).
3. The buyer chooses card. `POST /transactions` with `payment_method: card` answers a `REDIRECT`
   payment instruction. The buyer pays on Pesapal's page with a test card
   (`docs/pesapal-observed.md` section 4) and comes back to `FRONTEND_URL/pay/return?order=<id>&ref=<reference>`.
4. The IPN (`/payments/pesapal/ipn`) and the return page (`POST /transactions/{id}/payment/refresh`)
   are triggers only. Vekro asks Pesapal for the status and funds the order only for a COMPLETED
   payment of the exact amount, currency and reference. A mismatch is refused and audited.
   `python -m app.cli confirm-card-payments` settles open checkouts if neither arrives.

What it proves and what it does not: the buyer sees a real card form and a real authorisation in
Pesapal's sandbox. The money is collected into the merchant's Pesapal account, so it is not held by
an independent escrow agent. Refunds of card orders go to the admin queue (`GET /admin/refunds/manual`): support refunds them in
Pesapal, then completes them (`docs/runbooks.md` RB-11).

## Class demo: Daraja round trip

### 1. Tunnel (callbacks)

Daraja posts the payment result to `DARAJA_CALLBACK_URL`, which must be public HTTPS. Locally that
is an ngrok static domain:

```bash
ngrok config add-authtoken <token>     # once per machine
# .env
DARAJA_CALLBACK_URL=https://<your-static-domain>.ngrok-free.dev/payments/mpesa/callback
make tunnel                            # ngrok http 8000 --url https://<that host>
```

`scripts/tunnel.sh` reads the host from `DARAJA_CALLBACK_URL`, so the tunnel and the URL sent to
Daraja cannot drift apart. Cloudflare quick tunnels (`cloudflared tunnel --url
http://localhost:8000`) also work, but their hostname changes on every start, so
`DARAJA_CALLBACK_URL` must be updated each time.

Check the route through the tunnel (expect `Accepted` and a `daraja_callback_unknown_checkout` log
line, never a 404):

```bash
curl -X POST https://<host>/payments/mpesa/callback -H 'Content-Type: application/json' \
  -d '{"Body":{"stkCallback":{"CheckoutRequestID":"ws_CO_probe","ResultCode":0}}}'
```

### 2. Run

```bash
DARAJA_ENABLED=true make run      # terminal 1
make tunnel                       # terminal 2
```

The buyer pays from the web app; the prompt arrives on their phone.

### 3. The callback is only a trigger

The callback is unsigned. When it arrives, Vekro runs its own STK query, and only a confirmed,
exact-amount result locks the order and records funding. A callback alone never funds.

### 4. No tunnel or flaky Wi-Fi: the fallback

The callback is the primary path. The sandbox rate-limits the STK query (HTTP 429, often within
the first minute; see `docs/daraja-observed.md`), so a callback's own query can be refused and
the order left waiting. Run the fallback watcher alongside the demo:

```bash
make confirm-mpesa-watch    # python -m app.cli confirm-mpesa-payments --watch [SECONDS], default 900
```

It looks for open prompts every few seconds, including new orders. It queries each one about 5s
after first seeing it, then at doubling intervals up to 30s, each with ±20% jitter. It never sends
two queries less than 2s apart, and a prompt the callback settles meanwhile just drops out. A 429
or a pending answer only backs off; it never fails a payment. With a few payments in a class demo
that keeps Vekro well under the limit. Prompts older than 10 minutes are left alone.

Leave **at least a minute between two payments from the same phone**. Daraja refuses a second
prompt while the first is in progress ("a similar transaction being underway"), which is why the
resend button waits 60 seconds. `make confirm-mpesa` does a single pass instead. The
scheduler (M24 B4) will run this on its own.

### 5. Opt-in live test

```bash
# .env: DARAJA_* credentials, DARAJA_LIVE_TEST_PHONE=2547XXXXXXXX (never committed)
RUN_DARAJA_LIVE_TESTS=1 DARAJA_LIVE_MODE=callback|fallback DARAJA_LIVE_DB=<path> \
  .venv/bin/pytest -m live -k daraja -s
```

Each run sends **one KES 1** STK push through `create_transaction` into a scratch SQLite
database and funds that one order by one path:

- `callback`: start the backend on the same database (`DATABASE_URL` and `ALEMBIC_DATABASE_URL`
  = `sqlite:///<path>`, `DARAJA_ENABLED=true`) with `make tunnel` up. The test sends no query of
  its own and waits for the callback to lock the order.
- `fallback`: stop the tunnel. The test runs the watcher (`stop_when_idle`) until the order locks.

Sanitised fixtures go to `tests/fixtures/daraja/`. Only run it with the account owner's go-ahead:
it sends a real prompt to a real phone.
