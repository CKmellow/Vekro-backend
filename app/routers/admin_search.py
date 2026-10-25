from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.auth_context import require_admin_user
from app.db.session import get_db
from app.models.transaction import TransactionStatus
from app.models.user import User
from app.schemas.admin_case import (
    AdminSearchEvent,
    AdminSearchParty,
    AdminTransactionRow,
    AdminTransactionSearchResponse,
)
from app.services.admin_search import search_transactions

router = APIRouter(prefix="/admin/transactions", tags=["Admin Disputes"])


@router.get(
    "",
    response_model=AdminTransactionSearchResponse,
    summary="Search orders (admin only, read only)",
    description=(
        "Matches the reference, buyer or seller phone (any format), seller name, item title or "
        "serial. Filters by status, placed dates (EAT), amount and item type. Newest first; "
        "without a query this is recent activity."
    ),
    responses={
        401: {"description": "Authentication required."},
        403: {"description": "Authenticated user is not an admin."},
    },
)
def search(
    current_user: Annotated[User, Depends(require_admin_user)],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str | None, Query(max_length=80)] = None,
    status_filter: Annotated[list[TransactionStatus] | None, Query(alias="status")] = None,
    placed_from: Annotated[date | None, Query()] = None,
    placed_to: Annotated[date | None, Query()] = None,
    min_amount: Annotated[Decimal | None, Query(ge=0)] = None,
    max_amount: Annotated[Decimal | None, Query(ge=0)] = None,
    serialized: Annotated[bool | None, Query()] = None,
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> AdminTransactionSearchResponse:
    _ = current_user
    result = search_transactions(
        db,
        q=q,
        statuses=status_filter,
        placed_from=placed_from,
        placed_to=placed_to,
        min_amount=min_amount,
        max_amount=max_amount,
        serialized=serialized,
        page=page,
        page_size=page_size,
    )
    return AdminTransactionSearchResponse(
        items=[
            AdminTransactionRow(
                transaction_id=r.transaction.id,
                reference=r.transaction.reference,
                status=r.transaction.status,
                amount=r.transaction.amount,
                item_title=r.listing.title,
                is_serialized=r.listing.is_serialized,
                buyer=AdminSearchParty(name=r.buyer.name, phone=r.buyer.phone),
                seller=AdminSearchParty(name=r.seller.name, phone=r.seller.phone),
                created_at=r.transaction.created_at,
                last_event=(
                    AdminSearchEvent(title=r.last_event[0], at=r.last_event[1])
                    if r.last_event
                    else None
                ),
                dispute_id=r.dispute_id,
            )
            for r in result.items
        ],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )
