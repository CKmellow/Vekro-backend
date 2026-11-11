# Accounts and modes: one account for buying and selling

Status: approved by Cyprian 2026-11-11 (M28), with the payout-readiness change in section 2.

## 1. The model

| Rule | Meaning |
|---|---|
| One person, one account | An account is keyed by its verified phone (`users.phone`, already unique). |
| Every account can buy | There is no "buyer role". Any active, non-admin account can pay through a Trust Link. |
| Selling is a capability | Selling means the account has a **store**. A store is opened, can be closed and can be reopened. It is never deleted. |
| Admin is separate | An admin account never has a store, never buys and is never converted from or into a normal account. |

The store stays on the `users` row (one store per account) instead of moving to a new
`stores` table. `listings.seller_id`, `transactions.seller_id` and every anchored payload already
name the seller by the user's UUID. Keeping the store on the same row means no foreign key moves
and no anchored record changes meaning (see section 5). Shop name, area, about, logo and payout
number are already columns on `users`.

### Schema change (one Alembic migration)

- `users.role`: the enum becomes `member | admin`. `buyer` and `seller` go.
- New columns on `users`:
  - `store_status`: `none | active | closed`, default `none`;
  - `store_opened_at`, `store_closed_at`;
  - `seller_terms_version`, `seller_terms_accepted_at`;
  - `sms_login_enabled`, a boolean, default `true` (the Profile switch in A4).
- New check constraint on `transactions`: `buyer_id <> seller_id`. This is a second line of
  defence behind the service check in section 4.
- New check constraint on `users`: `role <> 'admin' OR store_status = 'none'`.

## 2. Migrating existing rows

The migration maps every row and merges nothing.

| Today | After | Store |
|---|---|---|
| `buyer` | `member` | `none` |
| `seller` | `member` | `active`. `store_opened_at` takes the account's `created_at`, and `seller_terms_*` takes `legacy`/`created_at`, because they accepted the seller terms at sign-up. |
| `admin` | `admin` | `none` (the check constraint enforces it) |

**Every existing seller gets an active store, whether or not they finished onboarding.** Today a
seller without a payout number can still exist with listings. If those sellers got no store, their
listings and orders would vanish from their area.

**A store without a verified payout number cannot take new orders** (Cyprian, 2026-11-11):
- checkout refuses with "This store isn't ready yet" (422);
- `GET /listings/{id}` returns `store_ready: false`, so the listing stays visible but cannot be
  bought;
- the seller sees a "Finish setup: add payout number (SMS-verified)" banner, driven by
  `store.payout_ready` on `/auth/me`;
- "verified" means `mpesa_phone` is set, which only the SMS-confirmed `/me/mpesa-number` flow
  (or onboarding with the same code) can do.

**Accounts are never merged.** Because `users.phone` is unique, one phone already maps to one row
and the migration never meets two rows to merge. If one person holds two accounts on two phones,
they stay two accounts:
- Google is matched on `sub`, never on e-mail (#324), so a shared e-mail links nothing.
- Merging needs proof of both phones and touches anchored records, so it stays out of scope.
  Support can close one store and the person keeps using the other account.

**Anchoring the migrated stores.** The migration itself never anchors anything; Alembic should not
write to the hash chain. A one-off, idempotent CLI command, `backfill-store-events`, records a
`store_opened` event with `migrated: true` for each existing store. It runs on the VM after the
deploy, the same way `backfill_seller_sales` ran for A9.

**Downgrade.** Each account maps back as follows:
- `member` with a store that is `active` or `closed` becomes `seller`;
- any other `member` becomes `buyer`.

An account that both bought and sold comes back as `seller`. Its purchases are still readable,
because order reads already check the order's `buyer_id`, not the role.

## 3. Log in and where people land

### API

`LoginResponse` and `/auth/me` keep `role` during the transition, so each merged step leaves the
live app working:
- `role` is derived as `admin`, `seller` (active store) or `buyer`. It is marked deprecated in
  OpenAPI.
- New fields:
  - `account_type: "member" | "admin"`;
  - `store: { status: "none" | "active" | "closed", name, opened_at, closed_at }`.

The frontend moves to the new fields in its own PR. `role` is removed only after that PR is live.

### `/login` methods

| Method | Members (buy and sell) | Admin |
|---|---|---|
| Password | yes | yes |
| SMS code | yes, unless the member turned it off in Profile | **never** |
| Continue with Google | yes | only if that admin linked Google themselves while signed in (already true: an unknown Google subject never reaches an admin) |

### Why seller SMS login fails today

`verify_phone_code` raises `PhoneNotBuyerError` (HTTP 409, "This number has a seller account")
for any role except buyer.

The fix:
- Any active member can use an SMS code.
- An unknown number is still created as a new member with no store (today's find-or-create).

### Admin refusal: generic and silent

- `POST /auth/phone/request-code` for an admin phone, or for a member who turned SMS login off:
  - answers exactly like any other number (202, same body, same timing path);
  - sends no SMS and stores no code;
  - logs `phone_code_refused reason=admin|disabled` without the phone.
- `/auth/phone/verify` then fails as "That code has expired" (410), the same as for any number
  with no live code. A second check in `verify_phone_code` refuses admins even if a code row
  existed.
- Nothing in the response tells an attacker that the number belongs to an admin.

### Code rules (unchanged, now tested per role)

| Rule | Value |
|---|---|
| Use | Single-use (`consumed_at`); a new code replaces the old one |
| Lifetime | 5 minutes |
| Resend | 30 s cooldown |
| Per phone | 5 codes per hour |
| Per IP | 20 codes per hour, in memory (one API process on the VM) |
| Lockout | 5 wrong tries lock the phone for 5 minutes |
| Storage | Only an HMAC of the code is stored, bound to phone and purpose |

### `destinationFor`

The signature becomes `destinationFor(account, returnTo, lastMode)`.

- Admin: `/admin`.
- Member with an active store: their last-used mode. The mode is kept in `localStorage` under
  `vekro.mode` and is a UI preference only; the server never reads it. With no saved mode they
  land in `/seller`.
- Member without an active store: `/buyer` (the new Home).
- `returnTo` is honoured when the account may use that area:
  - `/seller/*` needs an active store, except `/seller/orders/*` and `/seller/settings`, which a
    closed store may still read;
  - `/buyer/*`, `/order/*` and `/pay/*` are open to any member;
  - `/admin/*` is for admins only.

  When the account may not use the area, the person goes to their home.

## 4. Authorization rules

`require_buyer_user` and `require_seller_user` are replaced by three dependencies:

| Dependency | Who passes | Used for |
|---|---|---|
| `require_member` | any active non-admin | buying: create order, pay, inspect, accept or reject, report, dispute, buyer order list, Home summary, profile |
| `require_store_owner` | member whose store is `active` or `closed` | reading the seller's own history: seller orders, order detail, payouts, analytics, returns inbox; acting on orders already open |
| `require_active_store` | member whose store is `active` | create, edit or restore listings; seller onboarding steps that change the store |
| `require_admin_user` | admin (unchanged) | admin console |

### Who acts on an order

The role on an order comes from the order, not the account. This is already how
`get_transaction_for_viewer` works: the viewer is the order's buyer if `user.id == buyer_id` and
its seller if `user.id == seller_id`.

Every buyer action checks `transaction.buyer_id == user.id`, and every seller action checks
`transaction.seller_id == user.id`. Both checks already exist in the services; the role checks in
front of them go.

### Lists split by mode

- `GET /transactions` gains `as=buying|selling`.
  - It defaults to `selling` for an account with a store and `buying` otherwise, so today's
    frontend keeps working.
  - `_owner_clause` and the counterpart lookup key off `as`, not `user.role`.
- Notifications get an `audience: buying|selling` field, derived from whether the recipient is the
  order's buyer or seller. Each shell filters on it.

### Self-purchase ban

- `create_transaction` refuses an order whose `listing.seller_id == buyer.id`: 422, "You can't buy
  from your own store."
- `GET /listings/{id}` returns `is_own_listing: true` to its signed-in owner, so the pay page can
  say this before the buyer starts paying.
- The `buyer_id <> seller_id` check constraint backs this up in the database.
- Tests cover the service, the route and the constraint.

### Closed stores take no orders

- `create_transaction` refuses a listing whose store is not `active`.
- `GET /listings/{id}` returns `store_status: "closed"`, and the Trust Link shows "This store is
  closed".

### Admins never buy

`require_member` refuses admins. An admin with orders would be a party to the disputes they decide.

### `create-admin` stops promoting accounts

Today `create-admin` turns an existing account into an admin. It will now refuse any phone that
already has an account. Converting an account would combine admin with a store or with orders.

### Test matrix (A5)

The tests cover these accounts:
- a member with no store;
- a member with an active store;
- a member with a closed store;
- an admin.

They run each against every route group: buying, store history, store changes, admin.

## 5. Anchoring and the seller track record

| Concern | Why it stays correct |
|---|---|
| Party identity in anchored payloads | `transaction_created` and later events already carry `buyer_id` and `seller_id` as separate UUIDs per order. When one account buys and sells, the UUID shows up as a buyer on some orders and as a seller on others, never both on the same order (constraint). No anchored payload names a role, so none is reinterpreted. |
| Track record | `seller_track_record` counts orders where `Transaction.seller_id = <account>` and the order was released. That account's own purchases have it as `buyer_id`, so they never count. |
| Wash trading | Without the self-purchase ban, a seller could buy from their own store and inflate their "verified sales". The ban closes this for one account. Two accounts on two phones can still do it. The cheap extra check below covers the obvious case, and the FAQ will state the limit plainly. |
| Liabilities proof | Per order, for the order's buyer. Unchanged. |
| Admin references | Admins still appear only as `admin_ref` (D5), and an admin never has a store or orders, so an `admin_ref` never matches a party on an order. |
| Store lifecycle | New global-chain events `store_opened` and `store_closed`. Each payload holds `seller_id` (UUID), `at`, `reopened: bool` on opening, and `migrated: bool`. They carry no name, phone, shop name or payout number. The public track record lists these events, so a verifier can see when the store was open. |

**Cheap extra check:** `create_transaction` refuses when the paying phone equals the store's
payout number (422, "You can't buy from a store paid out to this number.").

## 6. Open, switch, close

### Start selling

The buyer area opens the existing onboarding steps in a wizard:
1. Store name (and optional area).
2. Payout phone, verified by SMS (the existing `/me/mpesa-number` code flow).
3. Seller terms: a checkbox. The terms version is stored.
4. The first listing (S7), which carries the dispute policy, because the policy is set per
   listing today.

The wizard calls a new `POST /account/store` after step 3:
- it moves `store_status` from `none` to `active`;
- it anchors `store_opened`;
- then the wizard opens S7.

A store with no listing yet is still a valid store, so a dropped connection never leaves a
half-made state.

### Mode switch

The shell shows a Buying ⇄ Selling switch only to accounts with a store. It writes
`vekro.mode` and navigates. It never calls the server.

### Close store: `POST /account/store/close`

The request is refused (409) while any of the following exist. The response lists each blocker so
the UI can show what must finish first:
- open orders, meaning any order whose status is not one of `released`, `refunded_buyer`,
  `returned_to_seller` or the `resolved_*` statuses, listed with their references;
- held money, meaning a balance still held in escrow for the store's orders (KES total);
- payouts still pending or of unknown outcome;
- open disputes or return cases, listed with their references.

When nothing blocks, one database transaction:
- archives every active listing (sets `archived_at`);
- sets `store_status = closed` and `store_closed_at`;
- anchors `store_closed`.

All orders, payouts, disputes, anchored events and the track record stay.

**Reopen: `POST /account/store/reopen`.** This sets the store back to `active` and anchors
`store_opened` with `reopened: true`. Listings stay archived; the seller restores the ones they
want.

### Why stores and orders are never hard-deleted

For the UI and docs:
- Every order's money movements are hash-chained and anchored on a public chain. Deleting the
  rows behind them would make Vekro's own audit fail and would look exactly like tampering.
- The buyer of a past order keeps the right to see their record, and to have it verified.
- Payment records must be kept for tax and dispute purposes.

UI copy: "Closing your store hides your listings and stops new orders. Your past orders and
payouts stay on record, because buyers and Vekro's public audit rely on them. You can reopen any
time."

## 7. Deleting personal data (documented future work, not built)

Under the Kenya Data Protection Act, 2019, a data subject can ask for their personal data to be
erased. The Act also limits keeping data to what a purpose or a legal duty requires (check the
section numbers, ss. 39-40, against the Act before citing them in the report).

The planned approach:
- Anonymise the profile:
  - name becomes "Deleted user";
  - phone, Google link, e-mail, logo, shop text and USSD PIN are cleared;
  - the account is deactivated;
  - all sessions are revoked.
- Keep the transaction, ledger and dispute records, linked only by UUID. Anchored payloads already
  hold no personal data (D5, A9), so the public record needs no change.

## 8. Delivery order (freeze 19 Nov 2026)

Each step is one issue and one PR, merged green before the next. Each step leaves the live app
working.

1. **Docs.** This note, plus the eConfirm KES 100 refund confirmation in `report-evidence.md`.
2. **Backend: accounts.**
   - Migration and role mapping.
   - The new dependencies and the authorization matrix.
   - The self-purchase ban, including the payer equals payout phone check.
   - The `as=` filter on order lists and the notification `audience`.
   - The derived `role` stays.
   - `create-admin` refusal.
   - Tests for the migration, the matrix and the ban.
3. **Backend: login.**
   - Member SMS login.
   - Admin and opted-out refusal, silent and generic.
   - The `sms_login_enabled` switch.
   - Per-role login tests.
   - The security section in `report-evidence.md`.
4. **Backend: store lifecycle.**
   - Open, close and reopen, with the close guards.
   - `store_opened` and `store_closed` anchoring, plus the `backfill-store-events` CLI.
   - The "This store is closed" listing field.
5. **Backend: buyer area endpoints.**
   - Home "needs you" summary.
   - Order filters: active, completed, refunded, disputed, plus search.
   - Profile: name, change phone with SMS, SMS login on or off.
   - Re-sync `openapi.json` in the frontend.
6. **Frontend: design specs** in `design/` for the buyer shell, Home, Orders, Returns & disputes,
   Profile & security, Start selling and Close store.
7. **Frontend: accounts.** Move to `account_type` and `store`, `destinationFor` with `lastMode`,
   and the mode switch in both shells.
8. **Frontend: buyer shell.** Sidebar and bottom nav, Home, Orders, Returns & disputes,
   Notifications, Profile & security, Help.
9. **Frontend: Start selling and Close store.**
10. **e2e, docs and deploy.**
    - e2e for the five A5 journeys.
    - `TEST-LOGINS.local.md`, `demo-script.md` and `presentation-faq.md`.
    - Deploy with nohup, verify on app.vekro.me.
    - Remove the deprecated `role` field only after the frontend is live; after the freeze it
      stays as is.

Steps 2 to 5 are the risky ones and come first. If time runs short, the order still leaves a
working app at every step.

## 9. Decisions (approved 2026-11-11)

1. Every existing seller gets an active store, but a store without a verified payout number takes
   no new orders until setup is finished (section 2).
2. Admins cannot buy (section 4).
3. Payer phone equal to the payout number: the order is refused (section 5).
4. Accounts with a store land in Selling on a device with no saved mode (section 3).
5. The store stays on `users` for now. A separate `stores` table (multiple stores per account,
   store history) is future work, listed in `report-evidence.md`.
6. `create-admin` refuses phones that already have an account; admin SMS-code requests get the
   same 202 with no SMS and no stored code.
