# Daraja (M-Pesa Express) — Observed Behaviour (M22 P1 discovery)

Recorded on 2026-11-08 against the **sandbox** (`https://sandbox.safaricom.co.ke`, test shortcode
174379) with Vekro's own sandbox app. This file records what was *observed*, which overrides any
assumption elsewhere. Sanitised captures are under `tests/fixtures/daraja/`. Phone numbers,
receipts and tokens are masked; the payer's number is never committed or logged.

Official docs read: developer.safaricom.co.ke "M-Pesa Express Simulate" and "M-Pesa Express
Query" (the portal is a JavaScript app: deep links answer 404 to plain HTTP clients, open them
from `/apis` in a browser). The "Account Balance" page did not render its content; see section 6.

## 1. OAuth

`GET /oauth/v1/generate?grant_type=client_credentials` with HTTP Basic (consumer key and secret).

- HTTP 200 `{"access_token": "...", "expires_in": "3599"}`
  (`tests/fixtures/daraja/oauth_token.json`).
- `expires_in` is a **string**. `DarajaClient` already parses it with `int(...)`.

## 2. STK push

`POST /mpesa/stkpush/v1/processrequest`. Our request matches the documented body field for field.
Limits from the docs that matter to Vekro:

| Field | Limit | Vekro sends |
|---|---|---|
| `AccountReference` | max 12 characters, **shown to the payer in the prompt** | the order reference, e.g. `VK-7K3P9Q` (was the transaction UUID cut to 12 characters before M22) |
| `TransactionDesc` | max 13 characters | `Vekro escrow` |
| `Amount` | whole shillings, minimum 1 | `whole_shillings(order amount)` |
| `CallBackURL` | "publicly accessible over the internet" | `DARAJA_CALLBACK_URL`, required to be `https://` (a tunnel locally) |

The docs do not state an HTTPS requirement; Vekro requires it anyway (settings validation).

Observed on 2026-11-08 (one KES 1 push): accepted, and the prompt arrived on the test phone.

**`CheckoutRequestID` embeds the payer's number.** Observed format:
`ws_CO_` + `DDMMYYYYHHMMSS` (Nairobi time) + one digit + the payer's 9-digit subscriber number,
e.g. `ws_CO_08112026143751` `2` `7XXXXXXXX`. Consequences:

- logs print only the first 20 characters (`masked_checkout`);
- fixtures mask the tail;
- the ID is personal data wherever it is stored or served (follow-up issue: it is currently
  copied into anchored audit payloads as `provider_reference`).

## 3. Callback

Daraja posted to `DARAJA_CALLBACK_URL` from `196.201.212.69` about 55 seconds after the push, when
the prompt was dismissed on the phone:

```json
{"Body": {"stkCallback": {"MerchantRequestID": "...", "CheckoutRequestID": "ws_CO_...",
  "ResultCode": 1032, "ResultDesc": "Request Cancelled by user."}}}
```

(`tests/fixtures/daraja/stk_callback_cancelled.json`.) No `CallbackMetadata` on a failure, as
documented. The callback is unsigned, so Vekro treats it only as a trigger
(`app/services/daraja_confirmation.py`).

## 4. STK query

`POST /mpesa/stkpushquery/v1/query`.

- The docs show `ResultCode` as a **string** (`"0"`); `DarajaClient` uses `int(str(...))`.
- **HTTP 429 with no `errorCode`**: observed twice within the first minute after the push. Once
  for the live test's first poll and once for the server's query triggered by the callback. The
  sandbox rate-limits this endpoint hard. Vekro classifies it as unavailable (try later): the
  callback-triggered confirmation left the order waiting, which is correct.
- A later query, from `python -m app.cli confirm-mpesa-payments` with the tunnel stopped,
  answered the 1032 result normally, and the prompt was marked `failed (cancelled)`.

So the fallback command is not only for a missing tunnel: it also settles prompts whose
callback-triggered query was rate-limited. `confirm-mpesa-payments --watch` (#243) does this with
backoff (5s doubling to 30s, jitter, at least 2s between queries); the scheduler (M24 B4) will run
it on its own.

## 5. A paid order, funded by the callback (2026-11-08, second session)

`DARAJA_LIVE_MODE=callback`, KES 1, order `VK-V4V79E`. The callback arrived about 11 seconds after
the push. The query it triggered answered `ResultCode` 0, so the order locked, the escrow was funded
with KES 1 and the balanced `ESCROW_HELD` / `BUYER_CLEARING` posting was written. No subscriber
digits appeared in any anchored payload, audit row or notification. Captured as
`tests/fixtures/daraja/stk_callback_paid.json`. It differs from the docs' sample:

- `Amount` is the integer `1`, not `1.0`;
- a `Balance` item is present **without** a `Value`;
- `MerchantRequestID` looks like `3197-4224-a7b4-…`.

A query the test sent after the callback, only to capture a fixture, got HTTP 429 again.

## 6. Two prompts to one phone within a minute: the second fails

A second push about 55 seconds after the first, to the same phone, was accepted by the API. The
phone then showed that M-Pesa was "unable to process request due to a similar transaction being
underway". About 13 seconds after that push, the query answered HTTP 200 with a `ResultCode` that
is neither 0 nor one Vekro maps. The code itself was not captured: every query since then has
answered 429. The docs' error table says the same in its own words: "Unable to lock subscriber, a
transaction is already in process for the current subscriber ... Wait at least 1 minute between
requests". Consequences (#245):

- `RESEND_COOLDOWN` is 60 seconds (was 30), so "resend" never triggers this;
- `stk_query` fails a prompt only on codes known to be final (`FINAL_FAILURE_CODES`: 1032, 1037,
  2001, 1). Any other code is pending and logged (`daraja_query_unrecognised_result`), because an
  unknown code may mean "still processing", and nothing re-checks a failed prompt;
- the watcher stops re-querying prompts older than 10 minutes, so a prompt stuck on an unknown
  code cannot eat the query budget;
- in a demo, leave at least a minute between payments from the same phone.

## 7. Not yet observed

- A query answering `ResultCode` 0 captured as a fixture, and the fallback path funding an order
  live (the fallback run collided with the previous prompt, section 6).
- The exact `ResultCode` for "similar transaction underway".

## 8. Account Balance (for M24 reserves)

Not read: the portal page loaded without content. To be checked before M24 B2.
