"""Operator commands. Run with `python -m app.cli <command>`.

create-admin: create a support admin, or promote an existing account to admin. The password comes
from the VEKRO_ADMIN_PASSWORD environment variable, never a flag, so it stays out of shell history.

repair-return-cases: list return cases the timeout jobs left stuck before #221; with --apply,
escalate or close them. Lists only by default.

repair-funding-ledger: list funded escrows whose funding is missing from the ledger (#222); with
--apply, post the missing movement. Lists only by default.

anchor-backfill: create anchor events for history written before M21, once (docs/anchoring.md).
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
    args = parser.parse_args(argv)

    if args.command == "repair-return-cases":
        return repair_return_cases(apply=args.apply)
    if args.command == "repair-funding-ledger":
        return repair_funding_ledger(apply=args.apply)
    if args.command == "anchor-backfill":
        return anchor_backfill(resume=args.resume)

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
