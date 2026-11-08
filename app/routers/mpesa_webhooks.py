import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from app.core.settings import get_settings
from app.db.session import get_db
from app.services.daraja import DarajaClient
from app.services.daraja_confirmation import handle_callback

router = APIRouter(prefix="/payments/mpesa", tags=["Webhooks"])
_logger = logging.getLogger("app.payments.daraja")

ACCEPTED = {"ResultCode": 0, "ResultDesc": "Accepted"}


@router.post(
    "/callback",
    summary="Daraja STK callback",
    description=(
        "Where Safaricom posts the M-Pesa Express result (DARAJA_CALLBACK_URL). The callback is "
        "unsigned, so it is only a trigger: Vekro confirms the payment with an STK query before "
        "locking the order and recording funding. Always answers Accepted, as Daraja expects."
    ),
)
def daraja_stk_callback(
    body: Annotated[dict[str, Any], Body()],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.daraja_enabled:
        _logger.warning("daraja_callback_while_disabled")
        return ACCEPTED
    try:
        outcome = handle_callback(db, body, client=DarajaClient.from_settings(settings))
        _logger.info("daraja_callback_processed outcome=%s", outcome)
    except Exception:  # never make Daraja retry into a broken handler
        db.rollback()
        _logger.exception("daraja_callback_failed")
    return ACCEPTED
