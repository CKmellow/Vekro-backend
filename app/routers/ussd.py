import hmac
import ipaddress
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


CLIENT_IP_HEADERS = {"cf-connecting-ip"}


def ussd_caller_ip(headers, peer: str, header_setting: str) -> str | None:
    """The caller's IP for USSD_ALLOWED_IPS, or None when it cannot be trusted (#289).

    Only CF-Connecting-IP is trusted: Cloudflare's edge sets it and overwrites any value the caller
    sends. It counts only when the request reached the API from a private or loopback address, the
    way cloudflared on the VM (or the web proxy) connects. Anything else, including a spoofable
    X-Forwarded-For, gives None.
    """
    if header_setting.strip().lower() not in CLIENT_IP_HEADERS:
        return None
    try:
        hop = ipaddress.ip_address(peer)
    except ValueError:
        return None
    if not (hop.is_private or hop.is_loopback):
        return None
    value = headers.get("cf-connecting-ip", "").strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def allowlist_active(settings) -> bool:
    """USSD_ALLOWED_IPS is enforced only together with USSD_CLIENT_IP_HEADER=cf-connecting-ip."""
    return bool(settings.ussd_allowed_ips.strip()) and (
        settings.ussd_client_ip_header.strip().lower() in CLIENT_IP_HEADERS
    )


@router.post(
    "/africastalking/{token}",
    response_class=PlainTextResponse,
    summary="Africa's Talking USSD callback",
    description=(
        "Africa's Talking posts every step of a USSD session here (form-encoded sessionId, "
        "serviceCode, phoneNumber, networkCode, text). The requests are unsigned, so the path "
        "carries USSD_CALLBACK_TOKEN; behind the Cloudflare tunnel USSD_ALLOWED_IPS can also limit "
        "callers (read from CF-Connecting-IP only). Answers "
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
    if allowlist_active(settings):
        allowed = {ip.strip() for ip in settings.ussd_allowed_ips.split(",") if ip.strip()}
        peer = request.client.host if request.client else ""
        caller = ussd_caller_ip(request.headers, peer, settings.ussd_client_ip_header)
        if caller is None or caller not in allowed:
            _logger.warning("ussd_caller_not_allowed trusted_ip=%s", caller is not None)
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
