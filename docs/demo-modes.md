# Demo modes: which rail takes each payment, and where the money sits

Vekro picks the collection rail from configuration alone. Print the current choice with:

```bash
.venv/bin/python -m app.cli payment-rails
```

The backend also logs it at startup (`collection_rails ...`).

## Profiles

| Profile | Settings | M-Pesa | Card | USSD | Where the money sits |
|---|---|---|---|---|---|
| **Default** (development, tests, CI) | nothing set | simulated stub: no prompt is sent; the order is paid via `POST /transactions/payment-callback` | not offered yet (M22 P2) | not offered yet (M23) | nowhere: Vekro's ledger rows only (`ESCROW_HELD`) |
| **Class demo** | `DARAJA_ENABLED=true` + `DARAJA_*`, `make tunnel` | Daraja sandbox STK push to the buyer's real phone; funded only after Vekro's own STK query confirms | not offered yet (M22 P2) | not offered yet (M23) | the sandbox test shortcode 174379 collects; Vekro's ledger records it as held. This is **collection, not custody**: no third party holds it in escrow |
| **Real custody** | `ECONFIRM_ENABLED=true` + key + `ECONFIRM_CODE_ENCRYPTION_KEY` | eConfirm (third-party escrow) for amounts in `ECONFIRM_MIN_AMOUNT`-`ECONFIRM_MAX_AMOUNT`; other amounts fall back to Daraja if enabled, else simulated | not offered yet | not offered yet | eConfirm holds the money until release or reversal |

Notes:

- Simulated stays the default. Nothing real is called unless a profile is switched on.
- eConfirm, when usable, is always tried first, so turning Daraja on never takes orders away from
  real custody.
- `CUSTODY_COLLECTION_RAIL_PRIORITY` (LOOP, Pesapal) drives the custody registry and its tests,
  not the buyer's checkout. Pesapal reaches buyers through M22 P2.

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

If the callback never arrives, or its query was rate-limited (the sandbox answers HTTP 429 often,
see `docs/daraja-observed.md`), confirm open prompts directly:

```bash
make confirm-mpesa          # python -m app.cli confirm-mpesa-payments
```

It queries every open prompt from the last 24 hours and locks the orders Daraja confirms. Run it
whenever an order seems stuck in "waiting for payment". The scheduler (M24 B4) will run it on an
interval.

### 5. Opt-in live test

```bash
# .env: DARAJA_* credentials, DARAJA_LIVE_TEST_PHONE=2547XXXXXXXX (never committed)
RUN_DARAJA_LIVE_TESTS=1 .venv/bin/pytest -m live -k daraja -s
```

It sends **one KES 1** STK push through `create_transaction` into a scratch SQLite database, then
polls the STK query, backing off on 429, and saves sanitised fixtures to
`tests/fixtures/daraja/`. Straight after the push it copies the database to
`<db>.fallback`, so one payment can be confirmed both ways:

- with `DARAJA_LIVE_DB=<path>`, `DARAJA_LIVE_EXPECT_CALLBACK=1`, the backend running on that
  database (`DATABASE_URL` and `ALEMBIC_DATABASE_URL`) and the tunnel up, the test waits for the
  callback to lock the order;
- then, with the tunnel stopped, run `confirm-mpesa-payments` against the `.fallback` copy.

Only run it with the account owner's go-ahead: it sends a real prompt to a real phone.
