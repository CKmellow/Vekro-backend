from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.auth_context import require_seller_user
from app.db.session import get_db
from app.models.listing import Listing
from app.models.user import User
from app.schemas.listing import (
    CreateListingRequest,
    ListingPhotoResponse,
    ListingResponse,
    ListingSellerSummary,
)
from app.services.listing import (
    ListingNotFoundError,
    ListingValidationError,
    create_listing,
    get_listing_by_id,
)
from app.services.listing_content import (
    MAX_PHOTO_BYTES,
    MAX_PHOTOS,
    ListingPhotoForbiddenError,
    ListingPhotoLimitError,
    ListingPhotoNotFoundError,
    ListingPhotoTooLargeError,
    ListingPhotoUnsupportedTypeError,
    add_listing_photo,
    describe_listing,
    get_listing_photo,
)

router = APIRouter(prefix="/listings", tags=["Listings"])


def _photo_url(listing_id: UUID, photo_id: UUID) -> str:
    return f"/listings/{listing_id}/photos/{photo_id}"


def _listing_response(db: Session, listing: Listing) -> ListingResponse:
    content = describe_listing(db, listing)
    return ListingResponse(
        id=listing.id,
        seller_id=listing.seller_id,
        title=listing.title,
        description=listing.description or "",
        category=listing.category,
        buyer_name=listing.buyer_name,
        price=listing.price,
        is_serialized=listing.is_serialized,
        unique_id=listing.unique_id,
        dispute_policy=listing.dispute_policy or {},
        status=content.status,  # type: ignore[arg-type]
        photos=[
            ListingPhotoResponse(id=photo_id, url=_photo_url(listing.id, photo_id))
            for photo_id in content.photo_ids
        ],
        seller=ListingSellerSummary(
            name=content.seller.name,
            member_since=content.seller.created_at,
            completed_sales=content.completed_sales,
        ),
        created_at=listing.created_at,
        updated_at=listing.updated_at,
    )


@router.post(
    "",
    response_model=ListingResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create listing (seller only)",
    description=(
        "Create a listing owned by the authenticated seller. "
        "Serialized listings must satisfy unique_id and dispute policy requirements. "
        "Add photos afterwards with POST /listings/{listing_id}/photos."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {"description": "Authenticated user is not a seller."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Business-rule or payload validation failed."
        },
    },
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

    return _listing_response(db, listing)


@router.get(
    "/{listing_id}",
    response_model=ListingResponse,
    summary="Get listing by id",
    description=(
        "Public read used by the buyer's Trust Link: content, photos, status and a seller "
        "summary, with serialized/dispute policy metadata when present."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Listing not found."},
    },
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

    return _listing_response(db, listing)


@router.post(
    "/{listing_id}/photos",
    response_model=ListingPhotoResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a listing photo (owner only)",
    description=(
        f"Upload one photo as the raw request body (image/jpeg, image/png or image/webp, "
        f"at most {MAX_PHOTO_BYTES // (1024 * 1024)} MB). Photos keep upload order; the first "
        f"is the cover. At most {MAX_PHOTOS} per listing."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {"description": "Caller is not the listing's seller."},
        status.HTTP_404_NOT_FOUND: {"description": "Listing not found."},
        status.HTTP_409_CONFLICT: {"description": "The listing already has the maximum photos."},
        status.HTTP_413_CONTENT_TOO_LARGE: {"description": "Photo is larger than allowed."},
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {"description": "Not a JPEG, PNG or WebP."},
    },
)
def upload_listing_photo(
    listing_id: UUID,
    data: Annotated[bytes, Body(media_type="image/jpeg")],
    content_type: Annotated[str, Header()],
    current_user: Annotated[User, Depends(require_seller_user)],
    db: Annotated[Session, Depends(get_db)],
) -> ListingPhotoResponse:
    try:
        photo = add_listing_photo(
            db,
            listing_id=listing_id,
            seller=current_user,
            content_type=content_type,
            data=data,
        )
    except ListingPhotoNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ListingPhotoForbiddenError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except ListingPhotoUnsupportedTypeError as exc:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)) from exc
    except ListingPhotoTooLargeError as exc:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)) from exc
    except ListingPhotoLimitError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return ListingPhotoResponse(id=photo.id, url=_photo_url(listing_id, photo.id))


@router.get(
    "/{listing_id}/photos/{photo_id}",
    summary="Get a listing photo",
    description="Public. Serves the image bytes; photos never change, so they cache for a year.",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {"content": {"image/jpeg": {}, "image/png": {}, "image/webp": {}}},
        status.HTTP_404_NOT_FOUND: {"description": "Photo not found."},
    },
)
def get_listing_photo_image(
    listing_id: UUID,
    photo_id: UUID,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    try:
        photo = get_listing_photo(db, listing_id=listing_id, photo_id=photo_id)
    except ListingPhotoNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return Response(
        content=photo.data,
        media_type=photo.content_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
