"""USSD menus for buyers and sellers (M23, #263; docs/ussd.md).

Africa's Talking sends every step of a session with the caller's answers so far joined by ``*``.
This module turns that path into one screen. It identifies the caller by the number AT asserts,
keeps what it showed (the order ids behind each numbered line) in ``ussd_sessions``, and performs
every action through the same service functions as the web, so USSD follows the same state
machine, timeouts and anchoring. Screens are plain text, at most ``MAX_SCREEN`` characters.

Sensitive actions need the caller's USSD PIN (``set_ussd_pin``); accepting at the door needs the
order's delivery code instead. Only database work happens while AT waits (10 seconds): an M-Pesa
prompt requested over USSD is sent after the reply (``UssdReply.after``).
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.phone import normalize_phone
from app.core.security import hash_password, verify_password
from app.models.listing import Listing
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.models.ussd_session import UssdSession

_logger = logging.getLogger("app.ussd")

MAX_SCREEN = 160
PIN_PATTERN = re.compile(r"^\d{4}$")
PIN_MAX_FAILURES = 3
PIN_LOCKOUT = timedelta(minutes=15)
ORDERS_SHOWN = 3

STATUS_LABELS = {
    "initiated": "Waiting for payment",
    "awaiting_payment": "Waiting for payment",
    "locked": "Paid, waiting for dispatch",
    "out_for_delivery": "On the way",
    "dispatched": "On the way",
    "at_door_pending_inspection": "At the door",
    "hold_24h": "In the 24h check",
    "disputed_functional": "Problem reported",
    "return_in_transit": "Return on the way",
    "return_received": "Return received",
    "awaiting_buyer_reconfirmation": "Is it fixed?",
    "escalated_admin_review": "With Vekro support",
    "returned_to_seller": "Returned to seller",
    "released": "Complete",
    "refunded_buyer": "Refunded",
    "resolved_release": "Complete",
    "resolved_refund": "Refund issued",
    "resolved_split": "Settled by support",
}
BUYER_ACTION_STATUSES = frozenset(
    {
        TransactionStatus.AT_DOOR_PENDING_INSPECTION,
        TransactionStatus.HOLD_24H,
        TransactionStatus.DISPUTED_FUNCTIONAL,
        TransactionStatus.AWAITING_BUYER_RECONFIRMATION,
    }
)
SELLER_ACTION_STATUSES = frozenset(
    {
        TransactionStatus.LOCKED,
        TransactionStatus.OUT_FOR_DELIVERY,
        TransactionStatus.RETURN_IN_TRANSIT,
    }
)
DOOR_REASONS = [
    ("damaged", "Damaged"),
    ("wrong_item", "Wrong item"),
    ("not_as_described", "Not as described"),
    ("missing_parts", "Missing parts"),
    ("other", "Other"),
]
ISSUE_CATEGORIES = [
    ("not_working", "Not working"),
    ("damaged_on_arrival", "Damaged on arrival"),
    ("missing_parts", "Missing parts"),
    ("not_as_described", "Not as described"),
    ("other", "Other"),
]


class UssdPinError(ValueError):
    pass


@dataclass(frozen=True)
class UssdReply:
    text: str  # starts with "CON " or "END "
    hop: str
    after: Callable[[], None] | None = None  # work to do once AT has its answer

    def __post_init__(self) -> None:
        if len(self.text) > MAX_SCREEN:  # pragma: no cover - guarded by tests on every screen
            raise ValueError(f"USSD screen too long ({len(self.text)}): {self.text!r}")


def con(text: str, hop: str) -> UssdReply:
    return UssdReply("CON " + text, hop)


def end(text: str, hop: str = "end") -> UssdReply:
    return UssdReply("END " + text, hop)


def set_ussd_pin(db: Session, user: User, pin: str) -> None:
    if not PIN_PATTERN.match(pin):
        raise UssdPinError("The USSD PIN must be 4 digits.")
    user.ussd_pin_hash = hash_password(pin)
    user.ussd_pin_failures = 0
    user.ussd_pin_locked_until = None
    db.add(user)
    db.commit()


def _money(amount: Decimal) -> str:
    return f"KES {Decimal(amount):,.0f}"


def _label(transaction: Transaction) -> str:
    return STATUS_LABELS.get(transaction.status.value, transaction.status.value)


class _Ussd:
    def __init__(self, db: Session, session: UssdSession, user: User, now: datetime) -> None:
        self.db, self.session, self.user, self.now = db, session, user, now

    # ---- helpers -------------------------------------------------------------------------
    def check_pin(self, pin: str) -> UssdReply | None:
        """None when the PIN is right; otherwise the screen to show."""
        user = self.user
        if not user.ussd_pin_hash:
            return end("Set your USSD PIN in the Vekro app first, then dial again.", "pin")
        locked = user.ussd_pin_locked_until
        if locked is not None and _utc(locked) > self.now:
            return end("Too many wrong PINs. Try again in 15 minutes.", "pin")
        if PIN_PATTERN.match(pin) and verify_password(pin, user.ussd_pin_hash):
            user.ussd_pin_failures = 0
            user.ussd_pin_locked_until = None
            self.db.add(user)
            return None
        user.ussd_pin_failures = (user.ussd_pin_failures or 0) + 1
        if user.ussd_pin_failures >= PIN_MAX_FAILURES:
            user.ussd_pin_failures = 0
            user.ussd_pin_locked_until = self.now + PIN_LOCKOUT
        self.db.add(user)
        self.db.commit()
        return end("Wrong PIN. Nothing changed.", "pin")

    def act(self, transaction: Transaction, action: Callable[[], Any], done: str) -> UssdReply:
        """Run a service action; any refusal becomes a short, friendly END screen."""
        from app.services.transaction import InvalidTransactionOtpError, TransactionValidationError

        try:
            action()
        except InvalidTransactionOtpError:
            self.db.rollback()
            return end("Wrong delivery code. Nothing changed.", "refused")
        except TransactionValidationError as exc:
            self.db.rollback()
            return end(str(exc)[: MAX_SCREEN - 4], "refused")
        except Exception as exc:  # the services raise one error family per refusal
            self.db.rollback()
            name = type(exc).__name__
            if name.endswith("InvalidStateError"):
                self.db.refresh(transaction)
                return end(
                    f"{transaction.reference} can't do that now. It is: {_label(transaction)}.",
                    "refused",
                )
            if name.endswith(("ForbiddenError", "NotFoundError")) or "NotFound" in name:
                return end("Order not found.", "refused")
            _logger.exception("ussd_action_failed transaction_id=%s", transaction.id)
            return end("Something went wrong. Nothing changed. Try again later.", "error")
        return end(done, "done")

    def orders(self) -> list[Transaction]:
        """The latest orders that need this caller first, then the latest others: on a feature
        phone, what to act on should not be hidden behind finished orders."""
        buyer = self.user.role == UserRole.BUYER
        column = Transaction.buyer_id if buyer else Transaction.seller_id
        needs_me = BUYER_ACTION_STATUSES if buyer else SELLER_ACTION_STATUSES
        latest = list(
            self.db.execute(
                select(Transaction)
                .where(column == self.user.id)
                .order_by(Transaction.created_at.desc())
                .limit(50)
            ).scalars()
        )
        first = [t for t in latest if t.status in needs_me]
        rest = [t for t in latest if t.status not in needs_me]
        return (first + rest)[:ORDERS_SHOWN]

    def chosen(self, choice: str) -> Transaction | None:
        ids = (self.session.context or {}).get("orders") or []
        if not choice.isdigit() or not 1 <= int(choice) <= len(ids):
            return None
        return self.db.get(Transaction, uuid.UUID(ids[int(choice) - 1]))

    # ---- menus ---------------------------------------------------------------------------
    def route(self, steps: list[str]) -> UssdReply:
        seller = self.user.role == UserRole.SELLER
        if not steps:
            if seller:
                return con("Vekro seller\n1 My orders\n0 Exit", "menu")
            return con("Vekro\n1 My orders\n2 Pay for an order\n0 Exit", "menu")
        if steps[0] == "0":
            return end("Goodbye.")
        if steps[0] == "1":
            return self.my_orders(steps[1:], seller)
        if steps[0] == "2" and not seller:
            return self.pay(steps[1:])
        return end("Invalid choice. Dial again.", "invalid")

    def my_orders(self, steps: list[str], seller: bool) -> UssdReply:
        if not steps:
            orders = self.orders()
            if not orders:
                return end("You have no orders yet.", "orders")
            self.session.context = {"orders": [str(o.id) for o in orders], "shown": {}}
            flag_modified(self.session, "context")
            lines = [f"{i} {o.reference} {_label(o)}" for i, o in enumerate(orders, 1)]
            return con("Your orders\n" + "\n".join(lines), "orders")
        transaction = self.chosen(steps[0])
        if transaction is None:
            return end("Invalid choice. Dial again.", "invalid")
        # The menu for an order depends on its status. If the web (or another session) moved the
        # order since its menu was shown, the same digit could mean another action: refuse.
        context = dict(self.session.context or {})
        shown = dict(context.get("shown") or {})
        key, status = str(transaction.id), transaction.status.value
        if len(steps) == 1:
            shown[key] = status
            self.session.context = {**context, "shown": shown}
            flag_modified(self.session, "context")
        elif shown.get(key) != status:
            return end(
                f"{transaction.reference} just changed. It is now: {_label(transaction)}. "
                "Dial again.",
                "changed",
            )
        if seller:
            return self.seller_order(transaction, steps[1:])
        return self.buyer_order(transaction, steps[1:])

    def heading(self, transaction: Transaction) -> str:
        return f"{transaction.reference} {_money(transaction.amount)}\n{_label(transaction)}"

    def buyer_order(self, transaction: Transaction, steps: list[str]) -> UssdReply:
        from app.services import transaction as tx

        status = transaction.status
        buyer, tid = self.user, transaction.id
        if status == TransactionStatus.AT_DOOR_PENDING_INSPECTION:
            if not steps:
                return con(self.heading(transaction) + "\n1 Accept (enter code)\n2 Reject", "order")
            if steps[0] == "1":
                if len(steps) == 1:
                    return con("Enter the 6-digit delivery code from your SMS", "code")
                code = steps[1]
                listing = self.db.get(Listing, transaction.listing_id)
                done = (
                    "Accepted. Payment is held 24 hours in case you find a problem."
                    if listing is not None and listing.is_serialized
                    else "Accepted. Payment goes to the seller."
                )
                return self.act(
                    transaction,
                    lambda: tx.confirm_buyer_delivery_otp(self.db, tid, buyer, otp_code=code),
                    done,
                )
            if steps[0] == "2":
                reasons = self.door_reasons(transaction)
                if len(steps) == 1:
                    lines = [f"{i} {label}" for i, (_, label) in enumerate(reasons, 1)]
                    return con("Why are you rejecting it?\n" + "\n".join(lines), "reason")
                choice = steps[1]
                if not choice.isdigit() or not 1 <= int(choice) <= len(reasons):
                    return end("Invalid choice. Dial again.", "invalid")
                if len(steps) == 2:
                    return con("Enter your USSD PIN to reject", "pin")
                return self.with_pin(
                    steps[2],
                    transaction,
                    lambda: tx.withhold_buyer_delivery_otp(
                        self.db, tid, buyer, reason=reasons[int(choice) - 1][0]
                    ),
                    "Rejected. The seller takes it back and you get a full refund.",
                )
        if status == TransactionStatus.HOLD_24H:
            if not steps:
                return con(self.heading(transaction) + "\n1 Report a problem", "order")
            if steps[0] == "1":
                if len(steps) == 1:
                    lines = [f"{i} {label}" for i, (_, label) in enumerate(ISSUE_CATEGORIES, 1)]
                    return con("What is wrong?\n" + "\n".join(lines), "category")
                choice = steps[1]
                if not choice.isdigit() or not 1 <= int(choice) <= len(ISSUE_CATEGORIES):
                    return end("Invalid choice. Dial again.", "invalid")
                if len(steps) == 2:
                    return con("Enter your USSD PIN to report it", "pin")
                return self.with_pin(
                    steps[2],
                    transaction,
                    lambda: tx.report_functional_issue(
                        self.db,
                        tid,
                        buyer,
                        category=ISSUE_CATEGORIES[int(choice) - 1][0],
                        description="Reported over USSD.",
                    ),
                    "Reported. Payment stays held. Send the item back to the seller.",
                )
        if status == TransactionStatus.DISPUTED_FUNCTIONAL:
            if not steps:
                return con(self.heading(transaction) + "\n1 I've sent it back", "order")
            if steps[0] == "1":
                if len(steps) == 1:
                    return con("Enter your USSD PIN to confirm you sent it back", "pin")
                return self.with_pin(
                    steps[1],
                    transaction,
                    lambda: tx.mark_buyer_sent_back(self.db, tid, buyer),
                    "Noted. The seller confirms when the item arrives.",
                )
        if status == TransactionStatus.AWAITING_BUYER_RECONFIRMATION:
            if not steps:
                return con(
                    self.heading(transaction) + "\n1 Yes, it's fixed\n2 No, still not fixed",
                    "order",
                )
            if steps[0] in ("1", "2"):
                if len(steps) == 1:
                    return con("Enter your USSD PIN to answer", "pin")
                accepted = steps[0] == "1"
                return self.with_pin(
                    steps[1],
                    transaction,
                    lambda: tx.submit_buyer_reconfirmation(
                        self.db, tid, buyer, accepted=accepted, notes="Answered over USSD."
                    ),
                    (
                        "Thanks. Payment goes to the seller."
                        if accepted
                        else "Noted. The seller gets one more chance or Vekro support steps in."
                    ),
                )
        if steps:
            return end("Invalid choice. Dial again.", "invalid")
        return end(self.heading(transaction) + "\nNothing to do here now.", "order")

    def seller_order(self, transaction: Transaction, steps: list[str]) -> UssdReply:
        from app.services import transaction as tx

        actions = {
            TransactionStatus.LOCKED: (
                "Mark dispatched",
                lambda: tx.dispatch_transaction(self.db, transaction.id, self.user),
                "Marked dispatched. The buyer is told it is on the way.",
            ),
            TransactionStatus.OUT_FOR_DELIVERY: (
                "Mark arrived",
                lambda: tx.mark_transaction_arrived(self.db, transaction.id, self.user),
                "Marked arrived. The buyer gets the delivery code by SMS.",
            ),
            TransactionStatus.RETURN_IN_TRANSIT: (
                "I received the return",
                lambda: tx.mark_seller_received(self.db, transaction.id, self.user),
                "Noted. Choose refund or repair in the Vekro app.",
            ),
        }
        action = actions.get(transaction.status)
        if action is None:
            if steps:
                return end("Invalid choice. Dial again.", "invalid")
            return end(self.heading(transaction) + "\nNothing to do here now.", "order")
        label, run, done = action
        if not steps:
            return con(self.heading(transaction) + f"\n1 {label}", "order")
        if steps[0] != "1":
            return end("Invalid choice. Dial again.", "invalid")
        if len(steps) == 1:
            return con("Enter your USSD PIN to confirm", "pin")
        return self.with_pin(steps[1], transaction, run, done)

    def with_pin(
        self, pin: str, transaction: Transaction, action: Callable[[], Any], done: str
    ) -> UssdReply:
        refused = self.check_pin(pin)
        return refused if refused is not None else self.act(transaction, action, done)

    def door_reasons(self, transaction: Transaction) -> list[tuple[str, str]]:
        listing = self.db.get(Listing, transaction.listing_id)
        declared = (listing.dispute_policy or {}).get("door_reject_reasons") if listing else None
        if not declared:
            return DOOR_REASONS
        reasons = [(str(r), str(r).replace("_", " ").capitalize()) for r in declared][:4]
        return [*reasons, ("other", "Other")]

    def pay(self, steps: list[str]) -> UssdReply:
        if not steps:
            return con("Enter the order reference, for example VK7K3P9Q", "pay")
        reference = _reference(steps[0])
        transaction = self.db.execute(
            select(Transaction).where(
                Transaction.reference == reference, Transaction.buyer_id == self.user.id
            )
        ).scalar()
        if transaction is None:
            return end("No order with that reference on this number.", "pay")
        if transaction.status != TransactionStatus.AWAITING_PAYMENT:
            return end(f"{transaction.reference} is already: {_label(transaction)}.", "pay")
        if transaction.payment_method != "mpesa":
            return end("This order is paid by card. Use the link in your SMS.", "pay")
        if len(steps) == 1:
            return con(
                f"Pay {_money(transaction.amount)} for {transaction.reference}\n"
                "Enter your USSD PIN",
                "pin",
            )
        refused = self.check_pin(steps[1])
        if refused is not None:
            return refused
        order_id, buyer_id = transaction.id, self.user.id
        return UssdReply(
            "END An M-Pesa prompt is coming to this phone. Enter your M-Pesa PIN there.",
            "pay",
            after=lambda: _send_prompt(order_id, buyer_id),
        )


def _reference(text: str) -> str:
    """Accept VK7K3P9Q, vk-7k3p9q or 7K3P9Q, as typed on a keypad."""
    letters = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    if letters.startswith("VK"):
        letters = letters[2:]
    return f"VK-{letters}"


def _send_prompt(order_id: uuid.UUID, buyer_id: uuid.UUID) -> None:
    """After the reply: the same resend the web uses (cooldown included)."""
    from app.db.session import _get_session_factory
    from app.services.payment_prompts import resend_payment_prompt

    with _get_session_factory()() as db:
        buyer = db.get(User, buyer_id)
        if buyer is None:  # pragma: no cover
            return
        try:
            resend_payment_prompt(db, transaction_id=order_id, buyer=buyer)
        except Exception:  # logged; the buyer can dial again or use the web
            _logger.exception("ussd_payment_prompt_failed transaction_id=%s", order_id)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _find_user(db: Session, phone: str) -> User | None:
    return db.execute(
        select(User).where(or_(User.phone == phone, User.mpesa_phone == phone)).limit(1)
    ).scalar()


def handle_ussd(
    db: Session,
    *,
    session_id: str,
    phone_number: str,
    text: str,
    session_seconds: int = 180,
    now: datetime | None = None,
) -> UssdReply:
    current = now or datetime.now(UTC)
    try:
        phone = normalize_phone(phone_number) or ""
    except ValueError:
        return end("Vekro works with Kenyan numbers only.", "invalid")
    session = db.get(UssdSession, session_id[:64])
    if session is not None and _utc(session.expires_at) <= current:
        return end("This session has expired. Please dial again.", "expired")
    if session is None:
        session = UssdSession(
            session_id=session_id[:64],
            phone=phone,
            context={},
            created_at=current,
            expires_at=current + timedelta(seconds=session_seconds),
        )
        db.add(session)
    elif session.phone != phone:
        return end("Please dial again.", "invalid")
    user = _find_user(db, phone)
    if user is None or not user.is_active or user.role == UserRole.ADMIN:
        db.commit()
        return end("This number has no Vekro account. Sign up at vekro.co.ke", "no_account")
    session.user_id = user.id
    steps = [step.strip() for step in text.split("*")] if text else []
    reply = _Ussd(db, session, user, current).route(steps)
    db.add(session)
    db.commit()
    return reply
