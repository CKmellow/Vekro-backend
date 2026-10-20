import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.listing import Listing
from app.models.transaction import Transaction
from app.models.user import User, UserRole


class TransactionNotVisibleError(Exception):
    """The transaction does not exist or the viewer is not a party to it."""


@dataclass(frozen=True)
class TransactionView:
    transaction: Transaction
    listing: Listing
    buyer: User
    seller: User
    viewer_role: UserRole


def get_transaction_for_viewer(
    db: Session,
    *,
    transaction_id: uuid.UUID,
    viewer: User,
) -> TransactionView:
    """Loads a transaction for its buyer, its seller or an admin. Anyone else sees "not found"."""
    transaction = db.get(Transaction, transaction_id)
    if transaction is None:
        raise TransactionNotVisibleError("Transaction not found.")

    if viewer.role == UserRole.ADMIN:
        viewer_role = UserRole.ADMIN
    elif viewer.id == transaction.buyer_id:
        viewer_role = UserRole.BUYER
    elif viewer.id == transaction.seller_id:
        viewer_role = UserRole.SELLER
    else:
        raise TransactionNotVisibleError("Transaction not found.")

    listing = db.get(Listing, transaction.listing_id)
    buyer = db.get(User, transaction.buyer_id)
    seller = db.get(User, transaction.seller_id)
    if listing is None or buyer is None or seller is None:  # pragma: no cover - FK guarantees
        raise TransactionNotVisibleError("Transaction not found.")

    return TransactionView(
        transaction=transaction,
        listing=listing,
        buyer=buyer,
        seller=seller,
        viewer_role=viewer_role,
    )
