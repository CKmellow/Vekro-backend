import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy.orm import Session

from app.core.auth_context import get_client_identifier
from app.core.settings import get_settings
from app.db.session import get_db
from app.models.transaction import Transaction
from app.schemas.public_order import PublicCardPaymentStatus, PublicOrderSummary
from app.services.card_payments import (
    build_rail,
    card_checkout_for_return,
    card_payments_available,
    confirm_card_payment,
)
from app.services.public_order import (
    PublicLookupRateLimitedError,
    check_lookup_quota,
    find_public_order,
    state_group,
)

router = APIRouter(prefix="/public/orders", tags=["Public"])
_logger = logging.getLogger("app.payments.card")


def _check_quota(request: Request) -> None:
    try:
        check_lookup_quota(get_client_identifier(request))
    except PublicLookupRateLimitedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many lookups. Please wait a moment.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc


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
    _check_quota(request)
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


_CARD_RESPONSES: dict[int | str, dict[str, str]] = {
    404: {"description": "No card checkout with this reference and tracking id."},
    429: {"description": "Too many lookups. See Retry-After."},
}
_TrackingId = Annotated[
    str,
    Query(
        min_length=1,
        max_length=120,
        description="The OrderTrackingId Pesapal appends to the return URL.",
    ),
]


def _card_status(transaction: Transaction, prompt_status: str, reason: str | None):
    payment_status = prompt_status if prompt_status in {"pending", "succeeded"} else "failed"
    return PublicCardPaymentStatus(
        reference=transaction.reference,
        state_group=state_group(transaction),
        payment_status=payment_status,  # type: ignore[arg-type]
        failure_reason=(
            reason
            if payment_status == "failed" and reason in {"declined", "provider_error"}
            else None
        ),  # type: ignore[arg-type]
    )


def _card_checkout(db: Session, reference: str, tracking_id: str):
    found = card_checkout_for_return(db, reference, tracking_id)
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found.")
    return found


@router.get(
    "/{reference}/card-payment",
    response_model=PublicCardPaymentStatus,
    summary="Card payment status for the return page (no auth)",
    description=(
        "What /pay/return shows after Pesapal's checkout, without a session: whether the order is "
        "paid and the checkout's status. Needs the order reference and the OrderTrackingId "
        "Pesapal appended to the return URL; anything else is 404. No names, phones, ids or "
        "amounts. Poll it; POST asks Pesapal first."
    ),
    responses=_CARD_RESPONSES,
)
def public_card_payment(
    reference: Annotated[str, Path(min_length=4, max_length=64)],
    tracking_id: _TrackingId,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> PublicCardPaymentStatus:
    _check_quota(request)
    transaction, prompt = _card_checkout(db, reference, tracking_id)
    return _card_status(transaction, prompt.status, prompt.failure_reason)


@router.post(
    "/{reference}/card-payment",
    response_model=PublicCardPaymentStatus,
    summary="Check a card payment with Pesapal now (no auth)",
    description=(
        "The return page's check on arrival (and on 'Check again'), before Pesapal's IPN may "
        "have arrived: Vekro asks Pesapal for the order's status and records funding only for a "
        "COMPLETED payment of the exact amount, currency and reference, the same rule as the "
        "IPN. Nothing in the request is trusted as a result. Answers like the GET."
    ),
    responses=_CARD_RESPONSES,
)
def check_public_card_payment(
    reference: Annotated[str, Path(min_length=4, max_length=64)],
    tracking_id: _TrackingId,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> PublicCardPaymentStatus:
    _check_quota(request)
    transaction, prompt = _card_checkout(db, reference, tracking_id)
    settings = get_settings()
    if card_payments_available(settings):
        try:
            confirm_card_payment(db, transaction, prompt, rail=build_rail(settings))
        except Exception:  # the page keeps polling; the IPN and the scheduler still settle it
            db.rollback()
            _logger.exception("card_return_check_failed transaction_id=%s", transaction.id)
        db.refresh(transaction)
        db.refresh(prompt)
    return _card_status(transaction, prompt.status, prompt.failure_reason)
