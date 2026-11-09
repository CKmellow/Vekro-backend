"""Operator commands. Run with `python -m app.cli <command>`.

create-admin: create a support admin, or promote an existing account to admin. The password comes
from the VEKRO_ADMIN_PASSWORD environment variable, never a flag, so it stays out of shell history.

repair-return-cases: list return cases the timeout jobs left stuck before #221; with --apply,
escalate or close them. Lists only by default.

repair-funding-ledger: list funded escrows whose funding is missing from the ledger (#222); with
--apply, post the missing movement. Lists only by default.

anchor-backfill: create anchor events for history written before M21, once (docs/anchoring.md).

anchor-sales-backfill: record track-record events for completed sales from before M21 A9.
Idempotent.

confirm-mpesa-payments: query Daraja for open STK prompts from the last day whose callback never
arrived, and lock the orders it confirms (#224). Needs DARAJA_ENABLED=true. This is the fallback
when the callback cannot reach Vekro (no tunnel, flaky network): funding never depends on it.
With --watch [SECONDS] it keeps going, querying each open prompt at backed-off intervals (about
5s doubling to 30s, with jitter, never two queries at once) so the sandbox rate limit is not hit.

confirm-card-payments: ask Pesapal for the status of open card checkouts from the last day and
lock the orders it reports completed for the exact amount (#251). Needs card payments configured.

liabilities-snapshot: put every open escrow into a Merkle sum tree and anchor its root and total
(M24 B2, docs/anchoring.md). The scheduler runs it on an interval.

seed-ussd-demo: one demo buyer and one demo seller on the numbers typed into Africa's Talking's
simulator, with a USSD PIN and orders in every state worth showing (docs/ussd.md). Never in
production.

payment-rails: print which rail collects M-Pesa, card and USSD payments under the current
configuration, and where the money sits (docs/demo-modes.md).
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.phone import normalize_phone
from app.core.security import hash_password
from app.models.user import User, UserRole

PASSWORD_ENV = "VEKRO_ADMIN_PASSWORD"


def create_admin(db: Session, *, phone: str, name: str, password: str) -> tuple[User, bool]:
    """The admin, and whether it was created (False: an existing account was promoted)."""
    if len(password) < 12:
        raise ValueError("Admin passwords need at least 12 characters.")
    normalized = normalize_phone(phone)
    user = db.execute(select(User).where(User.phone == normalized)).scalar_one_or_none()
    created = user is None
    if user is None:
        user = User(name=name.strip(), phone=normalized, role=UserRole.ADMIN, password_hash="")
        db.add(user)
    user.role = UserRole.ADMIN
    user.password_hash = hash_password(password)
    user.is_active = True
    user.failed_login_attempts = 0
    user.locked_until = None
    db.commit()
    db.refresh(user)
    return user, created


def _session():
    from app.db.session import _get_session_factory

    return _get_session_factory()()


def repair_return_cases(*, apply: bool) -> int:
    from app.services.return_case_repair import REVIEW, repair_stranded_return_cases

    with _session() as db:
        cases = repair_stranded_return_cases(db, apply=apply)
    for case in cases:
        print(
            f"{case.reference} transaction={case.transaction_id} dispute={case.dispute_id} "
            f"{case.transaction_status.value}/{case.dispute_status.value} -> {case.action}"
        )
    fixable = sum(1 for case in cases if case.action != REVIEW)
    if apply:
        print(f"Repaired {fixable} case(s); {len(cases) - fixable} left for review.")
    else:
        print(f"{len(cases)} stuck case(s); dry run, nothing changed. Re-run with --apply.")
    return 0


def repair_funding_ledger(*, apply: bool) -> int:
    from app.services.funding_ledger_repair import REVIEW
    from app.services.funding_ledger_repair import repair_funding_ledger as repair

    with _session() as db:
        gaps = repair(db, apply=apply)
    for gap in gaps:
        print(
            f"escrow={gap.escrow_id} transaction={gap.transaction_id} "
            f"custody={gap.custody_provider} funded={gap.funded_amount} "
            f"ledger={gap.ledger_funded} -> {gap.action}"
        )
    fixable = sum(1 for gap in gaps if gap.action != REVIEW)
    if apply:
        left = len(gaps) - fixable
        print(f"Posted {fixable} missing funding movement(s); {left} left for review.")
    else:
        print(f"{len(gaps)} escrow(s) out of step; dry run, nothing changed. Re-run with --apply.")
    return 0


def anchor_backfill(*, resume: bool) -> int:
    from app.services.anchoring.backfill import BackfillRefusedError, backfill_anchor_events

    with _session() as db:
        try:
            result = backfill_anchor_events(db, resume=resume)
        except BackfillRefusedError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print(
        f"Backfilled {result.transactions_created} transaction(s), {result.ledger_entries} "
        f"ledger row(s), {result.money_audit_events} audit row(s) and {result.snapshots} "
        "snapshot(s). History before now is attested only from now."
    )
    return 0


def anchor_sales_backfill() -> int:
    from app.services.anchoring.track_record import backfill_seller_sales

    with _session() as db:
        recorded = backfill_seller_sales(db)
    print(f"Recorded {recorded} completed sale(s) for the sellers' track records.")
    return 0


def confirm_mpesa_payments(*, watch_seconds: float | None = None) -> int:
    from app.core.settings import get_settings
    from app.services.daraja import DarajaClient
    from app.services.daraja_confirmation import confirm_pending_payments, watch_pending_payments

    settings = get_settings()
    if not settings.daraja_enabled:
        print("Daraja is disabled (DARAJA_ENABLED=false); nothing to do.", file=sys.stderr)
        return 2
    client = DarajaClient.from_settings(settings)
    with _session() as db:
        if watch_seconds is None:
            outcomes = confirm_pending_payments(db, client=client)
        else:
            print(f"Watching open M-Pesa prompts for {watch_seconds:.0f}s (Ctrl+C to stop).")
            outcomes = watch_pending_payments(db, client=client, duration_seconds=watch_seconds)
    for transaction_id, outcome in outcomes.items():
        print(f"{transaction_id} {outcome}")
    print(f"Checked {len(outcomes)} open payment prompt(s).")
    return 0


def confirm_card_payments() -> int:
    from app.core.settings import get_settings
    from app.services.card_payments import (
        build_rail,
        card_payments_available,
        confirm_pending_card_payments,
    )

    settings = get_settings()
    if not card_payments_available(settings):
        print("Card payments are not configured (PESAPAL_*, FRONTEND_URL).", file=sys.stderr)
        return 2
    with _session() as db:
        outcomes = confirm_pending_card_payments(db, rail=build_rail(settings))
    for transaction_id, outcome in outcomes.items():
        print(f"{transaction_id} {outcome}")
    print(f"Checked {len(outcomes)} open card checkout(s).")
    return 0


def liabilities_snapshot() -> int:
    from app.services.anchoring.liabilities import build_liabilities_snapshot

    with _session() as db:
        snapshot = build_liabilities_snapshot(db)
    anchored = "anchored" if snapshot.anchor_event_id else "not anchored (anchoring is off)"
    print(
        f"Snapshot {snapshot.id}: {snapshot.leaf_count} open escrow(s), "
        f"KES {int(snapshot.total_cents) / 100:.2f} held, {anchored}."
    )
    return 0


def seed_ussd_demo(*, buyer_phone: str, seller_phone: str, pin: str) -> int:
    from app.core.settings import get_settings
    from app.services.ussd_demo import DemoSeedRefusedError
    from app.services.ussd_demo import seed_ussd_demo as seed

    with _session() as db:
        try:
            result = seed(
                db,
                buyer_phone=buyer_phone,
                seller_phone=seller_phone,
                pin=pin,
                environment=get_settings().environment,
            )
        except (DemoSeedRefusedError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print(f"Buyer {result.buyer_phone}, seller {result.seller_phone}, USSD PIN {pin}.")
    if result.password:
        print(f"Web password for the new account(s): {result.password}")
    for order in result.orders:
        code = f" (delivery code {order.delivery_code})" if order.delivery_code else ""
        print(f"  {order.reference}  {order.state}{code}")
    return 0


def payment_rails() -> int:
    from app.core.settings import get_settings
    from app.services.payment_rails import describe_collection_rails

    for route in describe_collection_rails(get_settings()):
        print(f"{route.method:<6} {route.rail:<10} {route.when:<32} {route.money_sits}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-admin", help="Create or promote a support admin.")
    admin.add_argument("--phone", required=True)
    admin.add_argument("--name", default="Vekro Support")
    repair = commands.add_parser(
        "repair-return-cases", help="List (or with --apply, fix) stuck return cases."
    )
    repair.add_argument("--apply", action="store_true")
    funding = commands.add_parser(
        "repair-funding-ledger", help="List (or with --apply, post) unposted funding."
    )
    funding.add_argument("--apply", action="store_true")
    backfill = commands.add_parser(
        "anchor-backfill", help="Create anchor events for history written before M21."
    )
    backfill.add_argument("--resume", action="store_true")
    commands.add_parser(
        "anchor-sales-backfill", help="Record completed sales for the sellers' track records."
    )
    confirm = commands.add_parser(
        "confirm-mpesa-payments", help="Confirm open STK prompts whose callback never arrived."
    )
    confirm.add_argument(
        "--watch",
        nargs="?",
        type=float,
        const=900.0,
        metavar="SECONDS",
        help="Keep confirming, with backoff, for SECONDS (default 900); picks up new prompts.",
    )
    commands.add_parser(
        "confirm-card-payments", help="Confirm open Pesapal card checkouts (status query)."
    )
    commands.add_parser("payment-rails", help="Show which rail collects each payment method.")
    demo = commands.add_parser("seed-ussd-demo", help="Demo buyer, seller and orders for USSD.")
    demo.add_argument("--buyer-phone", required=True)
    demo.add_argument("--seller-phone", required=True)
    demo.add_argument("--pin", default="1234")
    commands.add_parser(
        "liabilities-snapshot", help="Anchor the total held for buyers (Merkle sum tree)."
    )
    args = parser.parse_args(argv)

    if args.command == "repair-return-cases":
        return repair_return_cases(apply=args.apply)
    if args.command == "repair-funding-ledger":
        return repair_funding_ledger(apply=args.apply)
    if args.command == "anchor-backfill":
        return anchor_backfill(resume=args.resume)
    if args.command == "anchor-sales-backfill":
        return anchor_sales_backfill()
    if args.command == "confirm-mpesa-payments":
        return confirm_mpesa_payments(watch_seconds=args.watch)
    if args.command == "confirm-card-payments":
        return confirm_card_payments()
    if args.command == "liabilities-snapshot":
        return liabilities_snapshot()
    if args.command == "seed-ussd-demo":
        return seed_ussd_demo(
            buyer_phone=args.buyer_phone, seller_phone=args.seller_phone, pin=args.pin
        )
    if args.command == "payment-rails":
        return payment_rails()

    password = os.environ.get(PASSWORD_ENV, "")
    if not password:
        print(f"Set {PASSWORD_ENV} to the admin password.", file=sys.stderr)
        return 2

    with _session() as db:
        try:
            user, created = create_admin(db, phone=args.phone, name=args.name, password=password)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    print(f"{'Created' if created else 'Promoted'} admin {user.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
