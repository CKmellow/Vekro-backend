from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.listing import Listing
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.schemas.transaction import CreateTransactionRequest
from app.services.payment import PaymentGateway, PaymentRequest, get_payment_gateway


class TransactionValidationError(Exception):
    pass


class ListingNotFoundForTransactionError(Exception):
    pass


class PaymentInitiationError(Exception):
    pass


def create_transaction(
    db: Session,
    buyer: User,
    payload: CreateTransactionRequest,
    payment_gateway: PaymentGateway | None = None,
) -> Transaction:
    if buyer.role != UserRole.BUYER:
        raise TransactionValidationError("Only buyers can create transactions.")

    listing = db.get(Listing, payload.listing_id)
    if listing is None:
        raise ListingNotFoundForTransactionError("Listing not found.")

    amount = Decimal(payload.amount)

    transaction = Transaction(
        listing_id=listing.id,
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        amount=amount,
        status=TransactionStatus.AWAITING_PAYMENT,
    )
    db.add(transaction)
    db.commit()
    db.refresh(transaction)

    gateway = payment_gateway or get_payment_gateway()
    try:
        gateway.initiate_stk_push(
            PaymentRequest(
                transaction_id=str(transaction.id),
                amount=f"{transaction.amount:.2f}",
                phone_number=buyer.mpesa_phone,
                account_reference=str(transaction.id),
                transaction_desc=f"Escrow payment for listing {listing.id}",
            )
        )
    except Exception as exc:  # pragma: no cover
        raise PaymentInitiationError("Failed to initiate payment transport.") from exc

    return transaction
