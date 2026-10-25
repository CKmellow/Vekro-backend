from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.auth_context import require_seller_user
from app.db.session import get_db
from app.models.user import User
from app.schemas.seller_analytics import (
    AnalyticsKpis,
    DisputeCategoryCount,
    OutcomeWeek,
    SalesPoint,
    SellerAnalyticsResponse,
    TopListingRow,
)
from app.services.seller_analytics import seller_analytics

router = APIRouter(prefix="/seller", tags=["Seller analytics"])


@router.get(
    "/analytics",
    response_model=SellerAnalyticsResponse,
    summary="The seller's analytics for 7, 30 or 90 days",
    description=(
        "KPIs against the previous period of the same length, sales over time (EAT), outcomes "
        "per week, dispute categories and top listings. Only the seller's own paid orders."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Authentication required."},
        status.HTTP_403_FORBIDDEN: {"description": "Not a seller."},
    },
)
def get_analytics(
    current_user: Annotated[User, Depends(require_seller_user)],
    db: Annotated[Session, Depends(get_db)],
    range: Annotated[Literal["7d", "30d", "90d"], Query()] = "30d",  # noqa: A002
) -> SellerAnalyticsResponse:
    a = seller_analytics(db, current_user.id, range)
    return SellerAnalyticsResponse(
        range_days=a.range_days,
        completed_sales=a.completed_sales,
        current=AnalyticsKpis(**asdict(a.current)),
        previous=AnalyticsKpis(**asdict(a.previous)),
        bucket=a.bucket,  # type: ignore[arg-type]
        sales_series=[SalesPoint(date=d, amount=amt, orders=n) for d, amt, n in a.sales_series],
        outcomes=[
            OutcomeWeek(week_start=w, released=r, refunded=f, disputed=d)
            for w, r, f, d in a.outcomes
        ],
        dispute_categories=[
            DisputeCategoryCount(category=c, count=n) for c, n in a.dispute_categories
        ],
        top_listings=[TopListingRow(**asdict(t)) for t in a.top_listings],
        server_time=datetime.now(UTC),
    )
