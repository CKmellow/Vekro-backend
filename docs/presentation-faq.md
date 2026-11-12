# Presentation FAQ: short, honest answers

For the capstone presentations (week of 23 Nov 2026). Each answer points to where the detail
lives. If a question is not here, say what Vekro does today and what it does not.

## Where is the money held?

| Rail | Where the money sits while an order is open | Status today |
|---|---|---|
| M-Pesa (Daraja STK push) | Vekro's M-Pesa business shortcode account. Vekro releases it by button press or timeout. | **Sandbox** (test shortcode 174379): no real money moves. |
| Card (Pesapal hosted checkout) | Pesapal's merchant account. Pesapal **collects** it; it is not a custodian. Card refunds go through a manual queue (RB-11). | **Sandbox**, with Pesapal's published test cards. |
| eConfirm | eConfirm, a third-party escrow service, holds it (tier 1). | Integrated and tested against fixtures. The live KES 100 round trip has **not** been run. |
| Simulated | Nowhere: it is a test rail for development and CI. | Never counts as funded in production. |

The app says which one applies on every order ("Test payment via Daraja sandbox: no real money
moves", or "Held by Pesapal"). Detail: `docs/demo-modes.md`, `app/services/payment_rails.py`.

## Is Vekro a licensed escrow?

No. Vekro is a capstone prototype. Holding customer money in Kenya needs a licensed partner
(a bank, a payment service provider under the CBK, or an escrow custodian). Production would hold
funds with such a custodian (eConfirm, or a bank trust account) and use Vekro for the rules and the
record. Do not call Daraja or Pesapal "escrow".

## Why does the blockchain matter here?

*"Vekro holds the money; the blockchain holds Vekro to account."* Every money fact of an order is
added to an append-only hash chain: paid, held, released, refunded, a dispute decision, a payout.
The facts are batched into Merkle trees, and only each batch's root goes on a public registry
(Base Sepolia `0xD8094faf2B44711dD372A59C288130FA8113E4DB`). Then:

- the **buyer's browser** checks its order against the public root, without trusting Vekro's server
  (the "Verified record" panel);
- an **insider's later edit** shows up as TAMPERED (demo step 7);
- a **payout waits** until its facts are sealed, and an edited payout is halted (payout gate);
- a seller's **"verified sales"** count needs a proof for each sale;
- the **total held for buyers** is published as a Merkle sum tree, so each buyer can check their
  amount is in it (proof of liabilities).

No personal data goes on-chain: only roots. A batch costs a fraction of a cent. Detail:
`docs/anchoring.md`.

## What doesn't it prove?

- **That the money is at the bank or M-Pesa.** The chain proves what Vekro *recorded*. The reserves
  check compares provider balances with what Vekro owes, but it is Vekro's own report, anchored.
  Real proof of reserves needs the custodian to sign its balance.
- **That a record was true when written.** Anchoring stops later edits, not lies at write time.
  Payments are confirmed by the provider's own status query (Daraja STK query, Pesapal status)
  before any order counts as paid.
- **The minute before sealing.** An edit before the batch is anchored leaves no trace. The payout
  gate covers money leaving escrow.
- **Item quality, or who is right in a dispute.** The chain records the decision and when it was
  made; the decision is a person's.
- **That nothing was left out** of a seller's track record. Each counted sale is real, but omissions
  are not detectable.
- It is a **testnet** (Base Sepolia). Production would anchor on Base mainnet (same code, a funded
  key).

## Why no crypto wallets or tokens?

The users are Kenyan social-commerce buyers and sellers who already use M-Pesa. Wallets would add
key management, exchange-rate risk, licensing questions (VASP rules) and a worse experience, and
nobody asked for them. The chain is used where it adds something (an independent check on Vekro),
not for payments. Users never see the words "blockchain" or "crypto": the app says "sealed" and
"public record".

## What would production need?

1. A licensed custodian or bank holding the money (eConfirm after its live test, or a trust account)
   and the legal agreements around it.
2. Live keys: Daraja production (a paybill or till with B2C for payouts), Pesapal live, Africa's
   Talking live with a registered sender ID and a dedicated USSD code.
3. Base mainnet anchoring with a funded key, and key management (HSM or KMS).
4. Signed USSD callbacks or an operator-asserted network path (see below), monitoring and alerting,
   and backups off the VM.
5. A data-protection review (Kenya Data Protection Act 2019) and terms of service.
6. More than one VM: today it is a single B2als_v2 that stops at night.

## Security limits to admit

- **USSD callbacks are not signed.** Africa's Talking does not sign them. Vekro protects the
  endpoint with a secret path token, and optionally an IP allowlist that reads only Cloudflare's
  `CF-Connecting-IP` through the tunnel (#289). Every USSD action also needs the user's PIN or the
  delivery code.
- **In the sandbox simulator anyone can type any phone number.** So anyone with the simulator can
  "be" a demo user up to the PIN. On a live code the mobile network asserts the number.
- **The campus network intercepts TLS** (Strathmore Wi-Fi presents its own certificate), so the
  admin SSH through Cloudflare refuses to connect there, as it should. Use a phone hotspot. The
  public site still works because browsers trust the interception certificate on managed devices.
  We never turn verification off.
- **The 23:00 auto-stop.** While the VM is off, callbacks are lost. On start, the scheduler
  re-checks the last 24 hours with the providers.
- **Sandbox SMS** reaches only the AT simulator, not real phones.
- **One administrator** account type, with password login (or Google they linked), rate limits and
  lockout; never an SMS code, not even to reset the password. No hardware 2FA yet.

## One account for buying and selling? (M28)

Yes. One person is one account, keyed by a verified phone. Every account can buy; selling is a
store on the same account, opened from the buyer area (store name, seller terms, a payout number
proven by an SMS code). Admins are a separate kind of account: no store, no purchases, so they are
never a party to a dispute they decide.

- **Can a seller buy from their own store?** No. The backend refuses it, the database refuses an
  order whose buyer and seller are the same account, and a buyer can't pay from the number a store
  is paid out to. Otherwise a seller could inflate their public "verified sales". Two accounts on
  two phones can still collude; the record shows that sales happened through escrow, not who is
  behind each phone.
- **Can a seller close their store?** Yes, once nothing still needs them: no funded order in
  progress, no money held, no payout outstanding, no open dispute. Closing archives the listings and
  stops new orders; it can be reopened. Stores and orders are **never deleted**: they are part of
  the anchored record, and buyers keep the right to verify their orders.
- **What about deleting personal data (Kenya DPA 2019)?** Documented future work: anonymise the
  profile (name, phone, Google link) and keep the transaction records, which hold no personal data
  in their anchored form.
- **Why no SMS-code login for admins?** A SIM swap would hand over the console, which moves other
  people's money. Admins use a password, or Google they linked themselves. Their numbers get the
  same answer as any other on the code endpoints, so the endpoints can't reveal who is an admin.

## Tests and evidence

`docs/report-evidence.md` has the test totals per suite, coverage, the CI jobs, each security
measure with the test that proves it, and the live sandbox evidence.
