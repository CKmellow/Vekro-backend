import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.settings import get_settings
from app.db.session import get_db
from app.services.card_payments import build_rail, card_payments_available, handle_ipn

router = APIRouter(prefix="/payments/pesapal", tags=["Webhooks"])
_logger = logging.getLogger("app.payments.card")


class PesapalIpnAnswer(BaseModel):
    """The acknowledgement Pesapal documents for an IPN call."""

    orderNotificationType: str
    orderTrackingId: str
    orderMerchantReference: str
    status: int = Field(description="200: received and processed; 500: refused or not processed.")


_DESCRIPTION = (
    "Pesapal's IPN for card checkouts (register this URL with ipn_notification_type GET or "
    "POST). It is only a trigger: Vekro asks Pesapal for the order's status and records funding "
    "only for a COMPLETED payment of the exact amount. An IPN for an order Vekro did not send is "
    "refused (status 500) and changes nothing."
)


async def _params(request: Request) -> dict[str, Any]:
    params: dict[str, Any] = dict(request.query_params)
    if request.method == "POST":
        try:
            body = await request.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            params.update(body)
    return params


def _answer(db: Session, params: dict[str, Any]) -> PesapalIpnAnswer:
    settings = get_settings()
    if not card_payments_available(settings):
        _logger.warning("card_ipn_while_disabled")
        return PesapalIpnAnswer(
            orderNotificationType=str(params.get("OrderNotificationType") or "IPNCHANGE"),
            orderTrackingId=str(params.get("OrderTrackingId") or ""),
            orderMerchantReference=str(params.get("OrderMerchantReference") or ""),
            status=500,
        )
    try:
        return PesapalIpnAnswer(**handle_ipn(db, params, rail=build_rail(settings)))
    except Exception:  # never crash Pesapal's retries into a 5xx loop; it will call again
        db.rollback()
        _logger.exception("card_ipn_failed")
        return PesapalIpnAnswer(
            orderNotificationType=str(params.get("OrderNotificationType") or "IPNCHANGE"),
            orderTrackingId=str(params.get("OrderTrackingId") or ""),
            orderMerchantReference=str(params.get("OrderMerchantReference") or ""),
            status=500,
        )


@router.get(
    "/ipn",
    operation_id="pesapal_card_ipn_get",
    response_model=PesapalIpnAnswer,
    summary="Pesapal IPN (card checkout)",
    description=_DESCRIPTION,
)
async def pesapal_card_ipn_get(
    request: Request, db: Annotated[Session, Depends(get_db)]
) -> PesapalIpnAnswer:
    return _answer(db, await _params(request))


@router.post(
    "/ipn",
    operation_id="pesapal_card_ipn_post",
    response_model=PesapalIpnAnswer,
    summary="Pesapal IPN (card checkout)",
    description=_DESCRIPTION,
)
async def pesapal_card_ipn_post(
    request: Request, db: Annotated[Session, Depends(get_db)]
) -> PesapalIpnAnswer:
    return _answer(db, await _params(request))
