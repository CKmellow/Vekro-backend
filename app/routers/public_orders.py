from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy.orm import Session

from app.core.auth_context import get_client_identifier
from app.db.session import get_db
from app.schemas.public_order import PublicOrderSummary
from app.services.public_order import (
    PublicLookupRateLimitedError,
    check_lookup_quota,
    find_public_order,
    state_group,
)

router = APIRouter(prefix="/public/orders", tags=["Public"])


@router.get(
    "/{key}",
    response_model=PublicOrderSummary,
    summary="Public order summary (no auth)",
    description=(
        "The order behind an SMS link (its id) or a reference: title, amount and where the money "
        "is, with no names, phones or ids. Full details need a phone-verified session."
    ),
    responses={
        404: {"description": "No such order."},
        429: {"description": "Too many lookups. See Retry-After."},
    },
)
def public_order(
    key: Annotated[str, Path(min_length=4, max_length=64)],
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> PublicOrderSummary:
    try:
        check_lookup_quota(get_client_identifier(request))
    except PublicLookupRateLimitedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many lookups. Please wait a moment.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc

    found = find_public_order(db, key)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found.")
    transaction, listing, listing_status = found
    return PublicOrderSummary(
        reference=transaction.reference,
        listing_title=listing.title,
        amount=transaction.amount,
        state_group=state_group(transaction),
        listing_status=listing_status,  # type: ignore[arg-type]
    )
