from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.listing import Listing
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.schemas.transaction import CreateTransactionRequest


class TransactionValidationError(Exception):
    pass


class ListingNotFoundForTransactionError(Exception):
    pass


def create_transaction(
    db: Session,
    buyer: User,
    payload: CreateTransactionRequest,
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
    return transaction
