# eConfirm live KES 100 round trip on vekro-vm (prepared, not run)

**Status: run once, 2026-11-11, on VK-B627E5. Funded by eConfirm and released through eConfirm to
Cyprian's M-Pesa (`Completed`)**; the record is VERIFIED (attempt log below). Run it again only with
Cyprian's explicit go-ahead in that session. It moves real money (KES 100 plus eConfirm's and
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
# Releases and refunds through eConfirm (docs/custody-tiers.md step 5). Without these two the
# release waits as UNKNOWN "rail_unavailable" (#320). `simulated` stays first, so every other
# order's payouts route exactly as before.
CUSTODY_PAYOUT_RAIL_PRIORITY=simulated,econfirm
ALLOW_LIVE_PAYOUTS=true
# ECONFIRM_CODE_ENCRYPTION_KEY is already set on the VM
```

Copy `deploy/.env` to `deploy/.env.before-econfirm` (600) first: switching off is restoring it.

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

## Switch off (rollback; always, right after, and only once the order is settled)

Restore `deploy/.env.before-econfirm` (`ECONFIRM_ENABLED=false`, `ALLOW_LIVE_PAYOUTS=false`, no
custody override), recreate `api` and `scheduler` the same way, and run `payment-rails` again.

**Do not switch off while an eConfirm order is unsettled** (#320). Switched off, the scheduler stops
polling eConfirm (`econfirm_poll` runs only while eConfirm is usable), so a payment that lands
afterwards is not recorded, and the payment page shows the prompt as expired. The poller also looks
back only 15 minutes. To record a payment that landed while it was off, switch on again and run
`run_econfirm_funding_reconciliation(db, collection_rail=build_econfirm_provider(settings),
payment_timeout_minutes=240)` once inside the API container (what was done for VK-B627E5).

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

## Attempt log (2026-11-11, VK-B627E5)

| Time (UTC) | What happened |
|---|---|
| 11:59 | Backup `vekro-20261111T115906Z.dump`. KES 100 listing on seller 310, whose payout number Cyprian set to his phone. `ECONFIRM_ENABLED=true`, min = max = 100; `payment-rails` showed `econfirm KES 100-100`. |
| 12:06 | Buyer 311 (Amina; M-Pesa phone = Cyprian's) opened **VK-B627E5**. Vekro opened an eConfirm escrow, stored its confirmation code encrypted, and eConfirm sent a **real** M-Pesa prompt. Cyprian approved it: eConfirm e-mailed "Escrow txn_… is funded with KES 100.00" (15:06 EAT). |
| ~12:09 | Cyprian chose to abort. eConfirm was switched off **before the poller had recorded the funding**, so Vekro still showed awaiting payment, and B2 said "expired, nothing was charged" (wrong copy, frontend #308). |
| 12:11 | Read-only status query: eConfirm `Escrow Funded` → adapter outcome `SUCCEEDED` (the mapping was right). Cyprian chose to finish the round trip in Vekro. |
| 12:13 | eConfirm on again. One reconciliation pass: `polled=1, funded=1`; VK-B627E5 `locked` (held by eConfirm). |
| 12:20 | Over the real USSD endpoint (AT-shaped requests from the VM): seller dispatched, then marked arrived (PIN); buyer accepted with the delivery code → `released`. |
| 12:22–12:34 | Release attempts UNKNOWN `rail_unavailable`: eConfirm was not a payout rail (no request reached eConfirm). Fixed by `CUSTODY_PAYOUT_RAIL_PRIORITY=simulated,econfirm` and `ALLOW_LIVE_PAYOUTS=true` (#320). |
| 12:35 | Release sent through eConfirm: `payout_initiated`, then **`succeeded`**, eConfirm `raw_status: Completed`; escrow `released_amount 100.00`, paid to Cyprian's M-Pesa. |
| 12:37 | Switched off (the `.env` from before restored; `payment-rails` Daraja only). `verify_transaction.py`: **VERIFIED, 55 of 55 events anchored** (the retried release attempts are sealed money facts too). |

Evidence: `docs/report-assets/econfirm-release-VK-B627E5.sanitised.json` (git-ignored; the release
request and eConfirm's `Completed` response, with phone, eConfirm id and code redacted). eConfirm's
fee was not shown to Vekro's API; read it on the eConfirm statement.
