"""Open, close and reopen the account's store (M28, docs/accounts-and-modes.md, section 6)."""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth_context import require_member, require_store_owner
from app.db.session import get_db
from app.models.user import SELLER_TERMS_VERSION, User
from app.schemas.auth import StoreSummary
from app.services.store import (
    CloseBlockers,
    StoreCloseBlockedError,
    StoreNotAllowedError,
    close_blockers,
    close_store,
    reopen_store,
    start_selling,
)

router = APIRouter(prefix="/account/store", tags=["Store"])


class StartSellingRequest(BaseModel):
    shop_name: str = Field(min_length=2, max_length=60, description="The store's name.")
    shop_area: str | None = Field(default=None, max_length=80, description="Where it sells.")
    terms_version: str = Field(
        description=(
            "The seller terms version accepted; must be the current one "
            f"({SELLER_TERMS_VERSION})."
        )
    )


class CloseBlockersResponse(BaseModel):
    """What must finish before the store can close. Empty lists and 0 mean it can close now."""

    can_close: bool
    open_orders: list[str] = Field(description="References of funded orders not yet final.")
    held_amount: Decimal = Field(description="KES still held in escrow for this store's orders.")
    unsettled_payouts: list[str] = Field(description="References with a payout not yet settled.")
    open_disputes: list[str] = Field(description="References with an open return or dispute.")

    @classmethod
    def of(cls, blockers: CloseBlockers) -> "CloseBlockersResponse":
        return cls(
            can_close=not blockers.any,
            open_orders=blockers.open_orders,
            held_amount=blockers.held_amount,
            unsettled_payouts=blockers.unsettled_payouts,
            open_disputes=blockers.open_disputes,
        )


def _mine(db: Session, current: User) -> User:
    # The session user belongs to the middleware's DB session; change it in this one.
    user = db.get(User, current.id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    return user


@router.post(
    "",
    response_model=StoreSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Start selling: open a store on this account",
    description=(
        "Names the store and records the accepted seller terms; anchors store_opened. The store "
        "takes orders once its payout number is verified by SMS (PUT /me/mpesa-number with the "
        "code), then the seller adds a first listing with its dispute policy."
    ),
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Admin accounts can't open a store."},
        status.HTTP_409_CONFLICT: {"description": "Already open or closed (reopen it)."},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Terms not accepted."},
    },
)
def post_store(
    payload: StartSellingRequest,
    current_user: Annotated[User, Depends(require_member)],
    db: Annotated[Session, Depends(get_db)],
) -> StoreSummary:
    user = _mine(db, current_user)
    if payload.terms_version != SELLER_TERMS_VERSION:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Accept the current seller terms to open a store.",
        )
    try:
        start_selling(
            db,
            user,
            shop_name=payload.shop_name,
            shop_area=payload.shop_area,
            terms_version=payload.terms_version,
        )
    except StoreNotAllowedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return StoreSummary.of(user)


@router.get(
    "/close-check",
    response_model=CloseBlockersResponse,
    summary="What must finish before the store can close",
)
def get_close_check(
    current_user: Annotated[User, Depends(require_store_owner)],
    db: Annotated[Session, Depends(get_db)],
) -> CloseBlockersResponse:
    return CloseBlockersResponse.of(close_blockers(db, _mine(db, current_user)))


@router.post(
    "/close",
    response_model=StoreSummary,
    summary="Close the store",
    description=(
        "Archives every listing and stops new orders (Trust Links say the store is closed). "
        "Orders, payouts, disputes and the public record all stay: stores and orders are never "
        "deleted, because buyers and Vekro's anchored audit trail rely on them. Refused (409, "
        "with what must finish first) while orders, held money, payouts or disputes are open. "
        "Anchors store_closed. The store can be reopened."
    ),
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "Something must finish first; the body lists it.",
            "model": CloseBlockersResponse,
        }
    },
)
def post_close(
    current_user: Annotated[User, Depends(require_store_owner)],
    db: Annotated[Session, Depends(get_db)],
):
    user = _mine(db, current_user)
    try:
        close_store(db, user)
    except StoreCloseBlockedError as exc:
        body = CloseBlockersResponse.of(exc.blockers).model_dump(mode="json")
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "Finish these first.", **body},
        )
    except StoreNotAllowedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return StoreSummary.of(user)


@router.post(
    "/reopen",
    response_model=StoreSummary,
    summary="Reopen a closed store",
    description="Anchors store_opened (reopened). Listings stay archived until restored.",
    responses={status.HTTP_409_CONFLICT: {"description": "The store is not closed."}},
)
def post_reopen(
    current_user: Annotated[User, Depends(require_store_owner)],
    db: Annotated[Session, Depends(get_db)],
) -> StoreSummary:
    user = _mine(db, current_user)
    try:
        reopen_store(db, user)
    except StoreNotAllowedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return StoreSummary.of(user)
