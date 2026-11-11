# eConfirm live KES 100 round trip on vekro-vm (prepared, not run)

**Status: prepared 2026-11-11, NOT run.** It moves real money (KES 100 plus eConfirm's and
M-Pesa's fees) through a third-party escrow. Run it only after Cyprian's explicit go-ahead in that
session.

## What it shows

One real Vekro order whose money is held by **eConfirm**, a third-party escrow, instead of Vekro's
own Daraja shortcode:

1. The buyer pays KES 100 by an M-Pesa prompt that eConfirm sends. The money sits with eConfirm.
2. The order page says who holds the money (`custody_provider = econfirm`). The anchored record
   seals the eConfirm escrow facts like any other money fact.
3. The buyer accepts at the door. Vekro releases through eConfirm with the one-time
   `confirmation_code` it stored, encrypted, at creation. eConfirm pays the seller's M-Pesa.
4. The Verified record panel shows VERIFIED, and the liabilities proof covers the order while it is
   held.

It is the only path where a licensed-style third party, not Vekro, holds the money. That makes it
the answer to "where is the money?" for production (`docs/presentation-faq.md`).

## Cost

| Item | Amount | Source |
|---|---|---|
| Principal | KES 100 | `ECONFIRM_MIN_AMOUNT` (eConfirm's documented minimum) |
| eConfirm fee | **not published**; read it on the eConfirm portal before running | eConfirm dashboard |
| M-Pesa | the payer's normal paybill or STK charge, if any; receiving the release costs the receiver nothing | Safaricom tariff |

The payer and receiver are both Cyprian's Safaricom number, so the principal comes back to him at
release. What is spent is the fees. If anything stops half-way, the escrow id is in the logs and in
the eConfirm dashboard, and the money is recoverable there (reversal to the payer).

## Before (on the VM)

1. eConfirm portal: API key with release and reversal rights; note the fee for a KES 100 escrow;
   the account e-mail verified.
2. Seller payout number = Cyprian's number. Log in as the demo seller (series 3, `TEST-LOGINS`),
   go to Settings → payout number, and confirm with the SMS code (it shows in the AT simulator under
   the seller's number). Or use a dedicated seller whose `mpesa_phone` is his number.
3. A KES 100 listing on that seller (`Listings → New`, price 100).
4. Take a backup: `cd ~/vekro && deploy/backup.sh`.

## Switch on (config only, no deploy)

In `~/vekro/deploy/.env` (mode 600; never print it):

```
ECONFIRM_ENABLED=true
ECONFIRM_BASE_URL=https://api.econfirm.co.ke/api/v1
ECONFIRM_API_KEY=<from the eConfirm portal>
ECONFIRM_FALLBACK_EMAIL=<an inbox Cyprian reads, e.g. support@vekro.me>
ECONFIRM_MIN_AMOUNT=100
ECONFIRM_MAX_AMOUNT=100          # only a KES 100 order can go to eConfirm while it is on
# ECONFIRM_CODE_ENCRYPTION_KEY is already set on the VM
```

Then recreate the two services that read it, and check:

```sh
cd ~/vekro
nohup docker compose -f deploy/compose.yaml -f deploy/compose.vm.yml --env-file deploy/.env up -d api scheduler > ~/econfirm-on.log 2>&1 < /dev/null &
curl -s https://api.vekro.me/health
docker compose -f deploy/compose.yaml -f deploy/compose.vm.yml --env-file deploy/.env exec -T api python -m app.cli payment-rails < /dev/null
```

Every other order keeps its rail: KES 1 demo orders are below the minimum, and the KES 1,800
necklace is above the maximum.

## Run

1. Buyer (series 3 buyer, whose M-Pesa phone is Cyprian's) opens the KES 100 Trust Link → **Pay
   KES 100 with M-Pesa**. The prompt comes **from eConfirm**: approve it.
2. Wait for "Payment received". The scheduler polls eConfirm every `ECONFIRM_POLL_INTERVAL_SECONDS`.
   Check: the order (`GET /transactions/{id}`) has `custody_provider: econfirm`.
3. Seller: dispatch, then mark arrived (web or USSD). Buyer: accept with the delivery code.
4. Release goes through eConfirm. Watch `vk logs -f scheduler | grep -i econfirm` until the payout
   is `succeeded`. The KES 100 (less any fee) arrives on Cyprian's M-Pesa.
5. Verified record panel: VERIFIED after the next anchoring batch.

## Capture evidence (sanitised)

- `scripts/econfirm_probe.py` is not in the API image: run `.venv/bin/python scripts/econfirm_probe.py
  --dry-run` on the laptop to see what it sanitises. The real bodies of `stk-push`, `release` and the funded/Completed transitions go to
  `tests/fixtures/econfirm/observed/` with ids, phones, e-mails and confirmation codes replaced
  (as `econfirm_probe.py` does).
- Screenshots: order page (held by eConfirm), eConfirm dashboard (escrow row; blur the account
  e-mail), M-Pesa SMS of the release (blur the number), Verified record panel. Save them to
  `docs/report-assets/` (git-ignored).
- Add a row to `docs/report-evidence.md` (live evidence) and to `docs/econfirm-api-observed.md`
  section 9 (what was observed).

## Switch off (rollback; always, right after)

```
ECONFIRM_ENABLED=false
```

Recreate `api` and `scheduler` the same way, then run `payment-rails` again. Orders already on
eConfirm keep eConfirm as their custodian: the scheduler still settles them, because
`custody_provider` is stored per order.

If the run stops half-way:

| Where it stopped | Do |
|---|---|
| Prompt not approved | Nothing: the order times out; no money moved. |
| Funded, not released | Reject at the door (refund) **with eConfirm still on**, or reverse in the eConfirm dashboard. |
| Release sent, status unknown | Leave eConfirm on until the scheduler sees `Completed`; check the dashboard with the escrow id from the logs. |
| VM problem | Restore is not needed: nothing in Vekro's database is lost. The money is with eConfirm under the escrow id. |

## Not done here

The laptop's opt-in smoke test (`RUN_ECONFIRM_LIVE_TESTS=1 ECONFIRM_LIVE_MONEY_CONFIRM=KES100
pytest -m live -k econfirm -s`, `tests/test_econfirm_live.py`) is the provider-only alternative.
It opens, funds and **reverses** one escrow without a Vekro order. It needs
`ECONFIRM_FALLBACK_EMAIL` and the two `ECONFIRM_LIVE_*_PHONE` values in the laptop `.env`. It must
run from Cyprian's own shell: the agent's sandbox cannot reach eConfirm directly.
