"""Listing status, photos and the public seller summary."""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.listing import Listing
from app.models.listing_photo import ListingPhoto
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User

MAX_PHOTOS = 4
MAX_PHOTO_BYTES = 2 * 1024 * 1024
PHOTO_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}

UNPAID_STATUSES = {TransactionStatus.INITIATED, TransactionStatus.AWAITING_PAYMENT}
# The buyer got their money back, so the item can be sold again.
REFUNDED_STATUSES = {
    TransactionStatus.REFUNDED_BUYER,
    TransactionStatus.RESOLVED_REFUND,
    TransactionStatus.RETURNED_TO_SELLER,
}
RELEASED_STATUSES = {TransactionStatus.RELEASED, TransactionStatus.RESOLVED_RELEASE}


class ListingPhotoError(Exception):
    """Base for photo upload problems."""


class ListingPhotoForbiddenError(ListingPhotoError):
    pass


class ListingPhotoNotFoundError(ListingPhotoError):
    pass


class ListingPhotoUnsupportedTypeError(ListingPhotoError):
    pass


class ListingPhotoTooLargeError(ListingPhotoError):
    pass


class ListingPhotoLimitError(ListingPhotoError):
    pass


@dataclass(frozen=True)
class ListingContent:
    status: str
    photo_ids: list[uuid.UUID]
    seller: User
    completed_sales: int


def listing_status(db: Session, listing_id: uuid.UUID) -> str:
    statuses = set(
        db.execute(select(Transaction.status).where(Transaction.listing_id == listing_id))
        .scalars()
        .all()
    )
    if statuses - UNPAID_STATUSES - REFUNDED_STATUSES:
        return "sold"
    if statuses & UNPAID_STATUSES:
        return "awaiting_payment"
    return "active"


def listing_is_sold(db: Session, listing_id: uuid.UUID) -> bool:
    return listing_status(db, listing_id) == "sold"


def completed_sales(db: Session, seller_id: uuid.UUID) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(Transaction)
            .where(
                Transaction.seller_id == seller_id,
                Transaction.status.in_(RELEASED_STATUSES),
            )
        ).scalar_one()
    )


def photo_ids(db: Session, listing_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        db.execute(
            select(ListingPhoto.id)
            .where(ListingPhoto.listing_id == listing_id)
            .order_by(ListingPhoto.position)
        )
        .scalars()
        .all()
    )


def describe_listing(db: Session, listing: Listing) -> ListingContent:
    seller = db.get(User, listing.seller_id)
    if seller is None:  # pragma: no cover - FK guarantees
        raise LookupError("Listing seller not found.")
    return ListingContent(
        status=listing_status(db, listing.id),
        photo_ids=photo_ids(db, listing.id),
        seller=seller,
        completed_sales=completed_sales(db, seller.id),
    )


def add_listing_photo(
    db: Session,
    *,
    listing_id: uuid.UUID,
    seller: User,
    content_type: str,
    data: bytes,
) -> ListingPhoto:
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ListingPhotoNotFoundError("Listing not found.")
    if listing.seller_id != seller.id:
        raise ListingPhotoForbiddenError("Only the listing's seller can add photos.")
    media_type = content_type.split(";")[0].strip().lower()
    if media_type not in PHOTO_CONTENT_TYPES:
        raise ListingPhotoUnsupportedTypeError("Photos must be JPEG, PNG or WebP.")
    if not data:
        raise ListingPhotoUnsupportedTypeError("The photo is empty.")
    if len(data) > MAX_PHOTO_BYTES:
        raise ListingPhotoTooLargeError("Photos must be 2 MB or smaller.")
    existing = photo_ids(db, listing.id)
    if len(existing) >= MAX_PHOTOS:
        raise ListingPhotoLimitError(f"A listing can have at most {MAX_PHOTOS} photos.")

    photo = ListingPhoto(
        id=uuid.uuid4(),
        listing_id=listing.id,
        position=len(existing),
        content_type=media_type,
        size_bytes=len(data),
        data=data,
    )
    db.add(photo)
    db.commit()
    return photo


def get_listing_photo(db: Session, *, listing_id: uuid.UUID, photo_id: uuid.UUID) -> ListingPhoto:
    photo = db.get(ListingPhoto, photo_id)
    if photo is None or photo.listing_id != listing_id:
        raise ListingPhotoNotFoundError("Photo not found.")
    return photo
