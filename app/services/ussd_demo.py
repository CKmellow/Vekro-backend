"""Demo data for the USSD class demo (M23 U3; docs/ussd.md, docs/demo-script.md Scenario 7).

``python -m app.cli seed-ussd-demo --buyer-phone … --seller-phone …`` creates (or reuses) one buyer
and one seller on the phone numbers that will be typed into Africa's Talking's simulator, sets
their USSD PIN, and moves fresh orders through the real services into the states worth showing:
waiting for payment (pay by USSD), paid, on the way, at the door (its delivery code is printed),
and a serialized item in the 24-hour hold. Never in production.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.phone import normalize_phone
from app.core.security import hash_password
from app.models.listing import Listing
from app.models.notification import Notification, NotificationEventType
from app.models.transaction import Transaction
from app.models.user import PartyRole, User, UserRole
from app.schemas.transaction import CreateTransactionRequest, PaymentCallbackRequest
from app.services.payment import MpesaStkStubGateway
from app.services.store import open_store_on
from app.services.ussd import set_ussd_pin


class DemoSeedRefusedError(RuntimeError):
    pass


@dataclass(frozen=True)
class DemoOrder:
    reference: str
    state: str
    delivery_code: str | None = None


@dataclass(frozen=True)
class DemoSeed:
    buyer_phone: str
    seller_phone: str
    password: str | None  # only when an account was created now
    orders: list[DemoOrder]


ITEMS = [
    ("awaiting_payment", "Kiondo basket", Decimal("1500"), False),
    ("locked", "Kitenge wrap dress", Decimal("3200"), False),
    ("out_for_delivery", "Nike Air Force 1, size 42", Decimal("6500"), False),
    ("at_door", "Leather sandals", Decimal("2400"), False),
    ("hold_24h", "Samsung Galaxy A15", Decimal("18500"), True),
]


def seed_ussd_demo(
    db: Session, *, buyer_phone: str, seller_phone: str, pin: str, environment: str
) -> DemoSeed:
    if environment.strip().lower() == "production":
        raise DemoSeedRefusedError("The USSD demo seed never runs in production.")
    from app.services import transaction as tx

    password = secrets.token_urlsafe(9)
    created_any = False
    users: dict[PartyRole, User] = {}
    for role, phone, name in (
        (PartyRole.BUYER, buyer_phone, "Demo Buyer"),
        (PartyRole.SELLER, seller_phone, "Demo Seller"),
    ):
        number = normalize_phone(phone) or ""
        user = db.execute(select(User).where(User.phone == number)).scalar()
        if user is None:
            now = datetime.now(UTC)
            user = User(
                id=uuid.uuid4(),
                name=name,
                phone=number,
                role=UserRole.MEMBER,
                payout_verified_at=now if role == PartyRole.SELLER else None,
                password_hash=hash_password(password),
                mpesa_phone=number,
                session_version=1,
                is_active=True,
                failed_login_attempts=0,
                created_at=now,
                updated_at=now,
            )
            if role == PartyRole.SELLER:
                open_store_on(user, now=now)
            db.add(user)
            db.commit()
            created_any = True
        elif user.party_role != role:
            raise DemoSeedRefusedError(f"{number} is already a {user.party_role.value} account.")
        set_ussd_pin(db, user, pin)
        users[role] = user
    buyer, seller = users[PartyRole.BUYER], users[PartyRole.SELLER]

    orders: list[DemoOrder] = []
    for state, title, price, serialized in ITEMS:
        now = datetime.now(UTC)
        listing = Listing(
            id=uuid.uuid4(),
            seller_id=seller.id,
            title=title,
            price=price,
            is_serialized=serialized,
            unique_id="DEMO-IMEI-0001" if serialized else None,
            dispute_policy={},
            created_at=now,
            updated_at=now,
        )
        db.add(listing)
        db.commit()
        order = tx.create_transaction(
            db,
            buyer,
            CreateTransactionRequest(listing_id=listing.id),
            payment_gateway=MpesaStkStubGateway(),
        )
        code = None
        if state != "awaiting_payment":
            tx.confirm_payment_callback(
                db, PaymentCallbackRequest(transaction_id=order.id, result_code=0)
            )
        if state in ("out_for_delivery", "at_door", "hold_24h"):
            tx.dispatch_transaction(db, order.id, seller)
        if state in ("at_door", "hold_24h"):
            tx.mark_transaction_arrived(db, order.id, seller)
            code = _delivery_code(db, order)
        if state == "hold_24h" and code is not None:
            tx.confirm_buyer_delivery_otp(db, order.id, buyer, otp_code=code)
            code = None
        db.refresh(order)
        orders.append(DemoOrder(order.reference, order.status.value, code))
    return DemoSeed(buyer.phone, seller.phone, password if created_any else None, orders[::-1])


def _delivery_code(db: Session, order: Transaction) -> str | None:
    note = db.execute(
        select(Notification)
        .where(
            Notification.transaction_id == order.id,
            Notification.user_id == order.buyer_id,
            Notification.event_type == NotificationEventType.DELIVERY_ARRIVED,
        )
        .order_by(Notification.created_at.desc())
        .limit(1)
    ).scalar()
    return (note.payload or {}).get("delivery_code") if note else None
