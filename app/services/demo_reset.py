"""Demo data for the presentations on the live VM (#311; docs/demo-script.md).

``DEMO_RESET_CONFIRM=yes python -m app.cli demo-reset --payer-phone +2547…`` sets up a clean
shop on a fresh number series, so the demo accounts never show the old test orders. Anchored
history is append-only, so nothing old is deleted: old orders keep their accounts and time out
through the normal state machine. It creates:

- a seller shop with photographed listings, two buyers and a support admin;
- staged orders, each funded by a **real sandbox payment**. A Daraja sandbox prompt goes to the
  payer phone, and the order counts as paid only once Vekro's own confirmation (callback plus STK
  query, or the scheduler) has locked it. Nothing is marked paid by hand, so the anchored record
  and the reserves check stay true. The orders then move through the real services into the
  states worth showing.

The state windows are real (at the door 1 hour, hold 24 hours, dispatch 48 hours), so it runs
on the demo day and can be re-run for some states only (``--states at_door``). Re-running on the
same series reuses the accounts and their passwords.
"""

from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.phone import normalize_phone
from app.core.security import hash_password
from app.models.listing import Listing
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import PartyRole, User, UserRole
from app.schemas.transaction import CreateTransactionRequest
from app.services.listing_content import add_listing_photo
from app.services.payment import PaymentGateway
from app.services.store import open_store_on, record_store_opened
from app.services.ussd import set_ussd_pin
from app.services.ussd_demo import _delivery_code

CONFIRM_ENV = "DEMO_RESET_CONFIRM"
ASSETS = Path(__file__).resolve().parent.parent / "demo_assets"
STATES = ("paid", "out_for_delivery", "at_door", "hold_24h", "dispute")


class DemoResetRefusedError(RuntimeError):
    pass


@dataclass(frozen=True)
class Item:
    title: str
    description: str
    photo: str
    price: Decimal | None = None  # None: the sandbox prompt amount
    serial: str | None = None


# Shop listings for the live part of the demo (docs/demo-script.md steps 1 to 3).
SHOP = [
    Item(
        "Kiondo basket, hand-woven sisal",
        "Woven in Machakos, leather handles. About 30 cm across.",
        "kiondo-basket.jpg",
    ),
    Item(
        "Maasai beaded necklace",
        "Two-strand collar in red, blue and white glass beads.",
        "beaded-necklace.jpg",
        Decimal("1800"),
    ),
]
# One listing per staged order, so each one can be bought.
STAGED: dict[str, Item] = {
    "paid": Item("Kitenge wrap dress, size 12", "Ankara cotton, tie waist.", "kitenge-dress.jpg"),
    "out_for_delivery": Item(
        "Canvas sneakers, size 42", "White canvas, red stripe.", "canvas-sneakers.jpg"
    ),
    "at_door": Item(
        "Leather sandals, size 40", "Hand-stitched leather, brass studs.", "leather-sandals.jpg"
    ),
    "hold_24h": Item(
        "Android phone, 128 GB",
        "Boxed, with charger. Serial number checked at the door.",
        "android-phone.jpg",
        serial="DEMO-IMEI-351234560000011",
    ),
    "dispute": Item(
        "Android phone, 128 GB (second unit)",
        "Boxed, with charger. Serial number checked at the door.",
        "android-phone.jpg",
        serial="DEMO-IMEI-351234560000029",
    ),
}


@dataclass(frozen=True)
class Account:
    role: str
    name: str
    phone: str
    password: str | None  # only when created now


@dataclass(frozen=True)
class StagedOrder:
    state: str
    reference: str
    status: str
    delivery_code: str | None = None
    note: str = ""


@dataclass
class DemoReset:
    accounts: list[Account] = field(default_factory=list)
    shop_listings: list[str] = field(default_factory=list)
    orders: list[StagedOrder] = field(default_factory=list)


def series_phones(series: int) -> dict[str, str]:
    if not 3 <= series <= 9:
        raise DemoResetRefusedError("--series must be 3 to 9 (2 is the first go-live set).")
    base = f"+254700000{series}"
    return {
        "seller": f"{base}10",
        "buyer": f"{base}11",
        "card_buyer": f"{base}12",
        "admin": f"{base}20",
    }


def check_confirmed(environ: dict[str, str]) -> None:
    if environ.get(CONFIRM_ENV, "").strip().lower() != "yes":
        raise DemoResetRefusedError(
            f"Refused: set {CONFIRM_ENV}=yes to create demo accounts and send sandbox prompts."
        )


def wait_until_paid(
    db: Session,
    order: Transaction,
    *,
    timeout_seconds: float = 240,
    poll_seconds: float = 5,
    sleep: Callable[[float], None] = time.sleep,
    on_wait: Callable[[str], None] = lambda message: None,
) -> bool:
    """True once Vekro's own confirmation has locked the order (the API's callback handler or the
    scheduler's STK query did it, in their own processes)."""
    on_wait(f"Approve the M-Pesa sandbox prompt for {order.reference} (KES {order.amount}).")
    waited = 0.0
    while waited <= timeout_seconds:
        db.expire_all()
        current = db.get(Transaction, order.id)
        if current is not None and current.status != TransactionStatus.AWAITING_PAYMENT:
            return current.status == TransactionStatus.LOCKED
        sleep(poll_seconds)
        waited += poll_seconds
    return False


def run_demo_reset(
    db: Session,
    *,
    environ: dict[str, str],
    payer_phone: str,
    series: int = 3,
    states: tuple[str, ...] = STATES,
    pin: str = "1234",
    prompt_amount: Decimal = Decimal("1"),
    payment_gateway: PaymentGateway | None = None,
    pay: Callable[[Session, Transaction], bool] | None = None,
    gap_seconds: float = 65,
    sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None] = print,
) -> DemoReset:
    check_confirmed(environ)
    unknown = sorted(set(states) - set(STATES))
    if unknown:
        raise DemoResetRefusedError(f"Unknown state(s): {', '.join(unknown)}.")
    try:
        payer = normalize_phone(payer_phone)
    except ValueError:
        payer = None
    if not payer:
        raise DemoResetRefusedError("--payer-phone is not a Kenyan mobile number.")
    phones = series_phones(series)
    from app.cli import create_admin
    from app.services import transaction as tx

    result = DemoReset()
    people: dict[str, User] = {}
    for key, role, name in (
        ("seller", PartyRole.SELLER, "Wanjiru Kamau"),
        ("buyer", PartyRole.BUYER, "Amina Otieno"),
        ("card_buyer", PartyRole.BUYER, "Brian Mwangi"),
    ):
        user, password = _account(
            db, phones[key], role, name, payer if role == PartyRole.BUYER else None
        )
        if role == PartyRole.SELLER and not user.shop_name:
            user.shop_name = "Kiondo & Co."
            user.shop_area = "Nairobi, Kilimani"
            user.shop_about = "Handmade baskets, kitenge and leather, made in Kenya."
            db.commit()
        set_ussd_pin(db, user, pin)
        people[key] = user
        result.accounts.append(Account(key, name, user.phone, password))
    admin = db.execute(select(User).where(User.phone == phones["admin"])).scalar()
    admin_password = None
    if admin is None:
        admin_password = secrets.token_urlsafe(12)
        create_admin(db, phone=phones["admin"], name="Vekro Support", password=admin_password)
    result.accounts.append(Account("admin", "Vekro Support", phones["admin"], admin_password))

    seller, buyer = people["seller"], people["buyer"]
    has_listings = db.execute(select(Listing.id).where(Listing.seller_id == seller.id)).first()
    if has_listings is None:
        for item in SHOP:
            listing = _listing(db, seller, item, prompt_amount)
            result.shop_listings.append(f"{listing.title}: /pay/{listing.id}")

    if pay is None:

        def pay(db: Session, order: Transaction) -> bool:
            return wait_until_paid(db, order, sleep=sleep, on_wait=say)

    first = True
    for state in (s for s in STATES if s in states):
        if not first:
            sleep(gap_seconds)  # Daraja refuses a second prompt to one phone within a minute
        first = False
        listing = _listing(db, seller, STAGED[state], prompt_amount)
        order = tx.create_transaction(
            db,
            buyer,
            CreateTransactionRequest(listing_id=listing.id),
            payment_gateway=payment_gateway,
        )
        if not pay(db, order):
            db.expire_all()
            order = db.get(Transaction, order.id)
            result.orders.append(
                StagedOrder(state, order.reference, order.status.value, note="not paid: skipped")
            )
            continue
        code = None
        if state in ("out_for_delivery", "at_door", "hold_24h", "dispute"):
            tx.dispatch_transaction(db, order.id, seller)
        if state in ("at_door", "hold_24h", "dispute"):
            tx.mark_transaction_arrived(db, order.id, seller)
            code = _delivery_code(db, order)
        if state in ("hold_24h", "dispute") and code is not None:
            tx.confirm_buyer_delivery_otp(db, order.id, buyer, otp_code=code)
            code = None
        if state == "dispute":
            tx.report_functional_issue(
                db,
                order.id,
                buyer,
                category="other",
                description=(
                    "Screen flickers and the serial number on the box does not match the phone."
                ),
            )
        db.expire_all()
        order = db.get(Transaction, order.id)
        result.orders.append(StagedOrder(state, order.reference, order.status.value, code))
    return result


def _account(
    db: Session, phone: str, role: PartyRole, name: str, mpesa_phone: str | None
) -> tuple[User, str | None]:
    user = db.execute(select(User).where(User.phone == phone)).scalar()
    if user is not None:
        if user.party_role != role:
            raise DemoResetRefusedError(f"{phone} is already a {user.party_role.value} account.")
        if role == PartyRole.SELLER and user.payout_verified_at is None:
            # Demo numbers are operator-seeded sandbox numbers: counted as verified.
            user.payout_verified_at = datetime.now(UTC)
            db.commit()
        return user, None
    password = secrets.token_urlsafe(12)
    now = datetime.now(UTC)
    user = User(
        id=uuid.uuid4(),
        name=name,
        phone=phone,
        role=UserRole.MEMBER,
        password_hash=hash_password(password),
        # Demo numbers are operator-seeded sandbox numbers: counted as verified.
        payout_verified_at=now if role == PartyRole.SELLER else None,
        # Buyers' prompts go to the payer phone; sellers are paid out to their own number.
        mpesa_phone=mpesa_phone or phone,
        session_version=1,
        is_active=True,
        failed_login_attempts=0,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    if role == PartyRole.SELLER:
        open_store_on(user, now=now)
        record_store_opened(db, user)
    db.commit()
    return user, password


def _listing(db: Session, seller: User, item: Item, prompt_amount: Decimal) -> Listing:
    now = datetime.now(UTC)
    listing = Listing(
        id=uuid.uuid4(),
        seller_id=seller.id,
        title=item.title,
        description=item.description,
        price=item.price if item.price is not None else prompt_amount,
        is_serialized=item.serial is not None,
        unique_id=item.serial,
        dispute_policy={},
        created_at=now,
        updated_at=now,
    )
    db.add(listing)
    db.commit()
    add_listing_photo(
        db,
        listing_id=listing.id,
        seller=seller,
        content_type="image/jpeg",
        data=(ASSETS / item.photo).read_bytes(),
    )
    return listing
