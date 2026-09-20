from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.auth_context import require_seller_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.listing import CreateListingRequest, ListingResponse
from app.services.listing import (
    ListingNotFoundError,
    ListingValidationError,
    create_listing,
    get_listing_by_id,
)

router = APIRouter(prefix="/listings", tags=["Listings"])


@router.post(
    "",
    response_model=ListingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create listing (seller only)",
)
def create_seller_listing(
    payload: CreateListingRequest,
    current_user: Annotated[User, Depends(require_seller_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ListingResponse:
    try:
        listing = create_listing(db, seller=current_user, payload=payload)
    except ListingValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return ListingResponse.model_validate(listing)


@router.get(
    "/{listing_id}",
    response_model=ListingResponse,
    summary="Get listing by id",
)
def get_listing(
    listing_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> ListingResponse:
    try:
        listing = get_listing_by_id(db, listing_id)
    except ListingNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Listing not found.",
        ) from exc

    return ListingResponse.model_validate(listing)
