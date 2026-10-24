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


LISTING_STATUSES = ("active", "awaiting_payment", "sold", "archived")


@dataclass(frozen=True)
class ListingPage:
    items: list[Listing]
    total: int
    page: int
    page_size: int
    counts: dict[str, int]


def _status_from(statuses: set[TransactionStatus], *, archived: bool = False) -> str:
    if statuses - UNPAID_STATUSES - REFUNDED_STATUSES:
        return "sold"
    if statuses & UNPAID_STATUSES:
        return "awaiting_payment"
    return "archived" if archived else "active"


def listing_status(db: Session, listing_id: uuid.UUID) -> str:
    listing = db.get(Listing, listing_id)
    return _status_from(
        set(
            db.execute(select(Transaction.status).where(Transaction.listing_id == listing_id))
            .scalars()
            .all()
        ),
        archived=listing is not None and listing.archived_at is not None,
    )


def list_seller_listings(
    db: Session,
    *,
    seller: User,
    status: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> ListingPage:
    """One page of the seller's listings, newest first. Counts cover all of them, before filters."""
    listings = list(
        db.execute(
            select(Listing)
            .where(Listing.seller_id == seller.id)
            .order_by(Listing.created_at.desc(), Listing.id.desc())
        )
        .scalars()
        .all()
    )
    by_listing: dict[uuid.UUID, set[TransactionStatus]] = {}
    for listing_id, tx_status in db.execute(
        select(Transaction.listing_id, Transaction.status).where(Transaction.seller_id == seller.id)
    ).all():
        by_listing.setdefault(listing_id, set()).add(tx_status)
    statuses = {
        listing.id: _status_from(
            by_listing.get(listing.id, set()), archived=listing.archived_at is not None
        )
        for listing in listings
    }

    counts = {key: 0 for key in LISTING_STATUSES}
    for value in statuses.values():
        counts[value] += 1

    needle = (q or "").strip().lower()
    shown = [
        listing
        for listing in listings
        if (status is None or statuses[listing.id] == status)
        and (not needle or needle in listing.title.lower())
    ]
    start = (page - 1) * page_size
    return ListingPage(
        items=shown[start : start + page_size],
        total=len(shown),
        page=page,
        page_size=page_size,
        counts=counts,
    )


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
