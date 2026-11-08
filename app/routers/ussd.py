import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.core.settings import get_settings
from app.db.session import get_db
from app.services.ussd import handle_ussd

router = APIRouter(prefix="/ussd", tags=["Webhooks"])
_logger = logging.getLogger("app.ussd")

_NOT_FOUND = PlainTextResponse("END Not found", status_code=404)


def _client_ip(request: Request, trust_forwarded_for: bool) -> str:
    if trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


@router.post(
    "/africastalking/{token}",
    response_class=PlainTextResponse,
    summary="Africa's Talking USSD callback",
    description=(
        "Africa's Talking posts every step of a USSD session here (form-encoded sessionId, "
        "serviceCode, phoneNumber, networkCode, text). The requests are unsigned, so the path "
        "carries USSD_CALLBACK_TOKEN; USSD_ALLOWED_IPS optionally limits callers. Answers "
        "text/plain CON or END screens of at most 160 characters (docs/ussd.md)."
    ),
    responses={404: {"description": "Wrong token, caller not allowed, or USSD is off."}},
)
async def africastalking_ussd(
    token: str,
    request: Request,
    background: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
) -> PlainTextResponse:
    settings = get_settings()
    expected = settings.ussd_callback_token.get_secret_value()
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        return _NOT_FOUND
    allowed = {ip.strip() for ip in settings.ussd_allowed_ips.split(",") if ip.strip()}
    if allowed:
        caller = _client_ip(request, settings.ussd_trust_forwarded_for)
        if caller not in allowed:
            _logger.warning("ussd_caller_not_allowed")
            return _NOT_FOUND
    form = await request.form()
    reply = await run_in_threadpool(
        handle_ussd,
        db,
        session_id=str(form.get("sessionId") or ""),
        phone_number=str(form.get("phoneNumber") or ""),
        text=str(form.get("text") or ""),
        session_seconds=settings.ussd_session_seconds,
    )
    if reply.after is not None:
        background.add_task(reply.after)
    return PlainTextResponse(reply.text, headers={"at-ussd-hop-metadata": reply.hop})
