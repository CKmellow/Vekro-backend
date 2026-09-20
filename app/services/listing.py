from sqlalchemy.orm import Session

from app.models.listing import Listing
from app.models.user import User, UserRole
from app.schemas.listing import CreateListingRequest


class ListingValidationError(Exception):
    pass


class ListingNotFoundError(Exception):
    pass


def _validate_serialized_listing(payload: CreateListingRequest) -> dict:
    dispute_policy = dict(payload.dispute_policy)

    if not payload.is_serialized:
        return dispute_policy

    if payload.unique_id is None:
        raise ListingValidationError("unique_id is required for serialized listings.")

    resolution = dispute_policy.get("resolution")
    if not isinstance(resolution, str) or not resolution.strip():
        raise ListingValidationError(
            "dispute_policy.resolution is required for serialized listings."
        )

    dispute_policy["resolution"] = resolution.strip()
    return dispute_policy


def create_listing(db: Session, seller: User, payload: CreateListingRequest) -> Listing:
    if seller.role != UserRole.SELLER:
        raise ListingValidationError("Only sellers can create listings.")

    dispute_policy = _validate_serialized_listing(payload)

    listing = Listing(
        seller_id=seller.id,
        title=payload.title,
        price=payload.price,
        is_serialized=payload.is_serialized,
        unique_id=payload.unique_id,
        dispute_policy=dispute_policy,
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)
    return listing


def get_listing_by_id(db: Session, listing_id) -> Listing:
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise ListingNotFoundError("Listing not found.")
    return listing
