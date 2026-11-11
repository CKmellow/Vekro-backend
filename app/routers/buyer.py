"""The buyer area's Home (M28): what needs the buyer now, and recent orders."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth_context import require_member
from app.db.session import get_db
from app.models.transaction import TransactionStatus
from app.models.user import PartyRole, User
from app.schemas.transaction_detail import TransactionSummaryResponse
from app.services.deadlines import deadlines_for
from app.services.transaction_list import list_orders_for_viewer

router = APIRouter(prefix="/buyer", tags=["Buyer"])

NeedKind = Literal["pay", "at_door", "hold_check", "send_return", "confirm_return"]

# Which of the buyer's orders wait on the buyer, and why.
NEEDS: dict[TransactionStatus, NeedKind] = {
    TransactionStatus.AWAITING_PAYMENT: "pay",
    TransactionStatus.AT_DOOR_PENDING_INSPECTION: "at_door",
    TransactionStatus.HOLD_24H: "hold_check",
    TransactionStatus.DISPUTED_FUNCTIONAL: "send_return",
    TransactionStatus.AWAITING_BUYER_RECONFIRMATION: "confirm_return",
}
RECENT = 5


class NeedsYouItem(BaseModel):
    kind: NeedKind = Field(
        description=(
            "pay: complete payment; at_door: the item is at the door (inspect before due_at); "
            "hold_check: the 24-hour check on a serialized item (report before due_at); "
            "send_return: send the item back (before due_at); confirm_return: answer on a return."
        )
    )
    due_at: datetime | None = Field(description="When the order times out, if it does.")
    order: TransactionSummaryResponse


class BuyerHomeResponse(BaseModel):
    needs_you: list[NeedsYouItem] = Field(description="Soonest deadline first.")
    recent: list[TransactionSummaryResponse] = Field(description=f"The {RECENT} newest orders.")
    counts: dict[str, int] = Field(description="Orders per buyer filter (Orders tab badges).")
    server_time: datetime = Field(description="Server clock; offset countdowns with it.")


def _due(transaction) -> datetime | None:
    deadlines = deadlines_for(transaction)
    return deadlines.inspect_by or deadlines.hold_until or deadlines.return_send_by


@router.get(
    "/home",
    response_model=BuyerHomeResponse,
    summary="The buyer area's Home: what needs you now, and recent orders",
    description=(
        "For any member, about the orders it bought (never its store's sales). Admins have no "
        "buyer area (403)."
    ),
)
def get_buyer_home(
    current_user: Annotated[User, Depends(require_member)],
    db: Annotated[Session, Depends(get_db)],
) -> BuyerHomeResponse:
    waiting = list_orders_for_viewer(
        db,
        viewer=current_user,
        side=PartyRole.BUYER,
        statuses=list(NEEDS),
        page_size=50,
    )
    recent = list_orders_for_viewer(db, viewer=current_user, side=PartyRole.BUYER, page_size=RECENT)
    far = datetime.max.replace(tzinfo=UTC)
    needs = sorted(
        (
            NeedsYouItem(
                kind=NEEDS[item.transaction.status],
                due_at=_due(item.transaction),
                order=TransactionSummaryResponse.from_item(item, PartyRole.BUYER),
            )
            for item in waiting.items
        ),
        key=lambda need: need.due_at or far,
    )
    return BuyerHomeResponse(
        needs_you=needs,
        recent=[TransactionSummaryResponse.from_item(i, PartyRole.BUYER) for i in recent.items],
        counts={key.value: n for key, n in (recent.filter_counts or {}).items()},
        server_time=datetime.now(UTC),
    )
