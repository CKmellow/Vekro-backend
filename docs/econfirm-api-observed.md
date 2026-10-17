# eConfirm API v1 — Observed Behaviour (M20 / E1 discovery)

Recorded by `scripts/econfirm_probe.py` on 2026-10-17 against the **production**
API at `https://api.econfirm.co.ke`. This file records what was *observed* from
real responses, which overrides any assumption in the M20 brief. Captured
responses (sanitised: fake ids, codes, phones and e-mails) are under
`tests/fixtures/econfirm/`.

**Scope of this probe:** `GET /api/ping`, `POST /api/v1/transactions` (one
pending escrow), and `GET /api/v1/transactions/{id}` only. **stk-push, release
and reversal were NOT called** — no money moved.

---

## 0. Access / transport (important, undocumented)

eConfirm sits behind **Cloudflare**. Requests with a stdlib client User-Agent
(`Python-urllib/3.x`, and almost certainly `python-requests/*`) are rejected
**before reaching eConfirm** with:

```
HTTP 403  Cloudflare Error 1010 "Access denied"
error_name: browser_signature_banned
detail: "The site owner has blocked access based on your browser's signature."
```

A normal browser `User-Agent` header clears this generic bot filter; the
authenticated API then responds normally. **The real HTTP client (E2) must send
a browser-like `User-Agent`.** This is a Cloudflare WAF signature check, so a
JA3/TLS fingerprint block is also possible from some networks — if 403s recur
with a browser UA, that is the next thing to investigate. The Cloudflare 403
body is a different envelope from eConfirm's own responses (it carries
`cloudflare_error: true`, `ray_id`, `zone`); treat it as a transport/UNKNOWN
condition, not an eConfirm application error.

---

## 1. Authentication

- `Authorization: Bearer <ek_... key>` on all `/v1` requests.
- `GET /api/ping` needs no auth. Observed: `HTTP 200 {"message": "API is working!"}`.
  (Note `/ping` is at `/api/ping`, one level **above** `/api/v1`.)

---

## 2. Response envelope

eConfirm application responses use a consistent envelope:

```json
{ "success": true, "message": "…", "data": { … } }
```

Documented error envelope (not re-triggered in this probe; from portal docs):
`success: false` with `message` and `errors` fields, plus the HTTP status code.

---

## 3. `POST /api/v1/transactions` — create escrow

Request body sent:
```json
{
  "buyer_email": "…", "seller_email": "…", "receiver_phone": "254712345678",
  "amount": 100, "currency": "KES", "description": "VK-… (>=5 chars)",
  "terms": "…"
}
```

Observed response: **HTTP 201**, shape (see `create_transaction.json`):
```
success: bool
message: str                      # "Escrow transaction created successfully"
data.id: str                      # "txn_...."  (the transaction id)
data.status: str                  # "pending"
data.amount: int                  # whole shillings (100)
data.currency: str                # "KES"
data.buyer_email: str             # FLAT string on create
data.seller_email: str            # FLAT string on create
data.description: str
data.confirmation_code: str       # <-- 8 chars, e.g. "AB12CD34"
data.created_at: str              # ISO-8601 with +03:00 offset
data.integration.api_account_email_verified: bool
data.integration.max_principal_kes: int
```

- `amount` echoes back as an **integer** (whole shillings); send whole-shilling amounts.
- Minimum principal is **KES 100** (per portal docs; not stress-tested here).

---

## 4. `GET /api/v1/transactions/{id}` — poll status

Observed response: **HTTP 200**, shape (see `get_transaction.json`):
```
success: bool
data.id: str
data.status: str
data.amount: int
data.currency: str
data.description: str
data.terms: str                   # present on GET (not on create)
data.created_at: str
data.updated_at: str              # present on GET
data.buyer.email: str             # NESTED object on GET (buyer.{email,verified})
data.buyer.verified: bool
data.seller.email: str
data.seller.verified: bool
data.integration.api_account_email_verified: bool
data.integration.max_principal_kes: int
```

**Create and GET bodies differ in shape:** create returns `buyer_email`/
`seller_email` as flat strings and includes `confirmation_code`; GET returns
nested `buyer`/`seller` objects (with `verified`) and **does NOT echo
`confirmation_code`**. The adapter must parse both shapes, and must map status
from `data.status`.

---

## 5. `confirmation_code` — BLOCKER RESOLVED

**The `confirmation_code` IS returned to the API caller**, at
`data.confirmation_code` in the `POST /transactions` **201** response (observed:
an 8-character string). It is **not delivered only to a human**, so Vekro can
drive release and reversal on its own. The E1 blocker does not apply.

**Consequence for E3/E4:** `confirmation_code` appears **only in the create
response**, not in GET. Vekro must **persist it at creation** (stored encrypted
/ redacted in logs per Hard Rule 7) because it cannot be re-fetched later, and
it is required for `release` and `reversal`.

---

## 6. Status values

From the create/GET `data.status` field and the portal docs (exact strings):

| status            | meaning                                   |
|-------------------|-------------------------------------------|
| `pending`         | created, not yet funded (observed)        |
| `stk_initiated`   | STK prompt sent; awaiting payer confirm   |
| `Escrow Funded`   | funds secured in escrow                   |
| `payout_initiated`| release or reversal payout in progress    |
| `Completed`       | payout finished successfully              |
| `payout_failed`   | release could not complete                |

Note the inconsistent casing/spacing (`pending` vs `Escrow Funded`): map with a
normaliser, not exact-string equality on the raw value.

Internal mapping for E3:
- `pending`, `stk_initiated` → pending / awaiting-payment (not yet funded)
- `Escrow Funded` → funded (confirmed; ledger may post)
- `payout_initiated` → payout in progress (UNKNOWN until terminal)
- `Completed` → released/reversed success
- `payout_failed` → payout failed (retry or reverse)

---

## 7. Principal cap — DEVIATES from the M20 brief

This API account's **e-mail is verified**: `integration.api_account_email_verified: true`
and `integration.max_principal_kes: 500000`. So the real per-escrow cap is
**KES 500,000**, not the KES 1,000 the brief assumed.

Recommendation: keep `ECONFIRM_MAX_AMOUNT=1000` as a conservative **Vekro-side**
guard for now (demo safety), and treat the eConfirm cap as 500,000. The adapter
should read `integration.max_principal_kes` from responses rather than hard-code
a cap. **User decision needed** on the Vekro guard value.

---

## 8. Documented HTTP codes & rate limits (from portal; for E2 error mapping)

- 200 ok / 201 created / 400 validation or bad confirmation code / 401 bad key /
  404 not found / 409 release already in progress / 422 not releasable (state,
  phone, principal) / 429 rate limited / 502 STK or payout initiation failed /
  500 server error.
- Rate limits: most routes 1,000 req / 60 min / IP; STK push 120/min/IP;
  **release & reversal 30/min/key**.
- Taxonomy mapping (M10): 4xx validation (400/401/404/422) → `FAILED_DEFINITE`
  (no retry/failover); 429/5xx/timeouts/transport/Cloudflare-403 → `UNKNOWN`
  (GET status before any retry; never blind-retry create — no idempotency key).

---

## 8a. Transport implementation notes (E2)

The real transport (`app/services/custody/econfirm_transport.py`, built on
`httpx2`) classifies each exchange for the circuit breaker and fallback:

- **Infra failures (count against the breaker, map to UNKNOWN):** connection
  errors, timeouts, HTTP 429, HTTP 5xx, a Cloudflare block (403 + Cloudflare
  body/`error_code` 1010/`cloudflare_error`), and an unexpected non-JSON body on
  a < 400 response.
- **Healthy exchanges (do NOT open the breaker):** 2xx, and 4xx validation
  rejections (400/401/404/409/422) — the request reached eConfirm and was
  answered. The *business* outcome of a 4xx is still `FAILED_DEFINITE` at the
  adapter layer (E3/E4); it simply is not an infrastructure fault.
- Transport/connection failures return `status_code=0` with body
  `{"status": "transport_error", ...}`, preserving the adapter's existing
  UNKNOWN mapping.
- The browser User-Agent is configurable via `ECONFIRM_USER_AGENT`
  (default a Chrome UA). A Cloudflare 403 sets `cloudflare_blocked=True` on the
  response so E5 can open the breaker for new escrows while treating an existing
  eConfirm escrow as UNKNOWN (A8).
- Every request carries an `X-Request-Id`; an inbound `X-Correlation-Id` header
  is logged. Logs redact the API key (`ek_`), bearer tokens, phone numbers and
  confirmation codes.

## 9. NOT yet observed (deferred past E1)

Real response bodies for `POST /payments/stk-push`, `POST …/release`, and
`POST …/reversal`, and the funded/`Completed`/`payout_failed` transitions, are
**not** captured here — the brief forbids calling them in E1. The portal-doc
shapes are recorded in the M20 brief; they must be verified against real
responses in E4/E6 (with the user's go-ahead and a ≤ KES 10 amount).

---

## 10. Reproduce

```
.venv/bin/python scripts/econfirm_probe.py --dry-run   # show plan
.venv/bin/python scripts/econfirm_probe.py             # live ping/create/get
```

Requires `ECONFIRM_BASE_URL` and `ECONFIRM_API_KEY` in `.env` (gitignored).
The script never prints or writes the API key, and sanitises confirmation
codes, ids, phones and e-mails before writing fixtures.
