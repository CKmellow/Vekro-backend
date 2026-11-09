# Pesapal API 3.0 — Observed Behaviour (M22 P2 discovery)

Recorded on 2026-11-08 against the **sandbox** (`https://cybqa.pesapal.com/pesapalv3`) with the
public demo merchant keys for Kenya (`developer.pesapal.com/api3-demo-keys.txt`, linked from the
official Authentication page; no registration needed). Probe: `scripts/pesapal_probe.py`.
Captures: `tests/fixtures/pesapal/observed/` (token redacted). No payment was made.

Official pages read: API reference, Authentication, RegisterIPN, SubmitOrderRequest,
GetTransactionStatus, RefundRequest (developer.pesapal.com/how-to-integrate/e-commerce/api-30-json/).

## 1. Auth

`POST /api/Auth/RequestToken` `{consumer_key, consumer_secret}` returns HTTP 200
`{token, expiryDate, error: null, status: "200", message}`. `expiryDate` is ISO 8601 UTC, about
5 minutes ahead.

## 2. IPN registration: the M15 path is wrong

| Path | Result |
|---|---|
| `POST /api/Transactions/RegisterIPN` (M15 `pesapal_ipn_registration.py`, fixed in #249) | **HTTP 404** "No HTTP resource was found" |
| `POST /api/URLSetup/RegisterIPN` (documented) | HTTP 200, `ipn_id` GUID, `ipn_status: 1` |

The response spells the field `ipn_status_decription` (sic); the docs' sample spells it correctly.
Registration accepted a URL that was not reachable at the time, so Pesapal does not verify the URL
when registering it.

## 3. SubmitOrderRequest

Documented constraints that matter to Vekro: `id` up to 50 characters (alphanumerics, `- _ . :`);
`description` up to 100; `billing_address` needs `phone_number` or `email_address`;
`notification_id` is the IPN id. A KES 1 order with an e-mail-only billing address returned HTTP 200
`{order_tracking_id, merchant_reference, redirect_url, error: null, status: "200"}`.

M15's adapter also sent a `reference` field that is not in the documented request; removed in #249.

## 4. Hosted page: card payments are offered (go for P2)

`redirect_url` (`.../pesapaliframe/PesapalIframe3/Index?OrderTrackingId=...`) shows four options for
the demo merchant ("Development Work-KE"):

- M-Pesa;
- Airtel Money;
- **Visa / Mastercard / American Express**;
- the Pesapal e-wallet.

Choosing card shows billing fields (pre-filled from `billing_address`), then card fields. A sandbox
banner links to Pesapal's test cards (`/PesapalIframe/PesapalIframe3/TestPayments`):

| Outcome | Brand | Number | Expiry | CVV |
|---|---|---|---|---|
| No authorization (frictionless, completes) | Visa | 4761 7390 0101 0010 | 07/28 | 123 |
| | Mastercard | 5200 0000 0000 0114 | 07/28 | 123 |
| | Amex | 3400 000000 03961 | 07/28 | 1234 |
| 3D Secure challenge | Visa | 4000 0000 0000 1091 | 07/28 | 123 |
| | Mastercard | 5200 0000 0000 0007 | 07/28 | 123 |
| Card failure (declined) | Visa | 4000 0000 0000 1018 | 07/28 | 123 |

These are Pesapal's published test cards for its test environment; they do not work live.

## 5. GetTransactionStatus

`GET /api/Transactions/GetTransactionStatus?orderTrackingId=...`

Documented `status_code`: 0 INVALID, 1 COMPLETED, 2 FAILED, 3 REVERSED.

**Observed for an order nobody has paid yet:**

- HTTP **200**, but the body has `"status": "500"`;
- `status_code: 0`, `payment_status_description: "INVALID"`;
- `error: {code: "payment_details_not_found", message: "Pending Payment", ...}`.

So `INVALID` with `payment_details_not_found` means **still pending**, not failed. Since #249 the
adapter reads the documented `status_code`: 1 succeeded, 2 and 3 failed, and 0 or any unknown code
pending. It also returns the paid `amount`, `currency` and `confirmation_code` for the funding
check. The body also carries `call_back_url` with the
return parameters appended, plus `order_tracking_id` and `account_number`, which the docs' table
does not list.

## 6. Callback and IPN

From the docs (not yet observed live):

- The buyer returns to `callback_url` with `OrderTrackingId`, `OrderMerchantReference` and
  `OrderNotificationType=CALLBACKURL`.
- The IPN (GET or POST, as registered) carries the same fields with `IPNCHANGE`.
- Neither carries the payment status, so Vekro must call GetTransactionStatus.
- The IPN endpoint must answer JSON
  `{orderNotificationType, orderTrackingId, orderMerchantReference, status: 200|500}`.

## 7. Refunds: differs from the brief

The brief says Pesapal refunds cannot be automated. The docs describe
`POST /api/Transactions/RefundRequest` (`confirmation_code`, `amount`, `username`, `remarks`), but:

- "A refund has to be approved by the merchant";
- Pesapal's finance team then processes it, and HTTP 200 "does not mean the refund has been
  effected";
- only COMPLETED payments can be refunded, and only once;
- mobile payments can only be refunded in full.

So a refund can be *requested* by API, but its completion is manual and asynchronous. P2 keeps the
brief's design (admin queue, "manual refund required") and records the API as an option for later.

## 8. Card payments observed (2026-11-09, sandbox, KES 10 each)

Three orders, each paid on Pesapal's hosted page with one of the published test cards from section
4. Status responses are saved, with the card number masked to its last four digits, in
`tests/fixtures/pesapal/observed/get_transaction_status_completed_*.json`.

| Test card (section 4) | Expected | Observed |
|---|---|---|
| Visa ...0010 "no authorization" | completes | `status_code: 1`, `Completed`; the hosted page then spun on "Processing" and never redirected; the scheduler's card fallback funded the order within a minute |
| Visa ...1091 "3D Secure challenge" | challenge, then completes | **no challenge page shown**; `status_code: 1`, `Completed`; redirected back to `/pay/return` |
| Visa ...1018 "card failure" | declined | **`status_code: 1`, `Completed`**; redirected back; Vekro funded the order |

So in this sandbox, on this day, neither the 3D Secure challenge nor the decline could be
reproduced: Pesapal itself reports all three as completed, and Vekro read them correctly. The
declined path (`status_code: 2`) stays covered by the unit fixtures only. Other observed fields: the
status body carries `payment_method: "Visa"`, `payment_account` (masked card), a 22-digit
`confirmation_code`, `description: "Transaction successfully processed."` and an `error` object with
all-null fields.

## 9. Not yet observed

- A real decline (`status_code: 2`) and a reversal (`3`).
- The IPN call itself (the tunnel showed no Pesapal IPN for these three; status came from the
  return page's refresh and the scheduler).
