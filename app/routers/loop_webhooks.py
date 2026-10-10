from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.settings import get_settings
from app.db.session import get_db
from app.schemas.loop_webhook import LoopCollectionWebhookResponse
from app.services.custody.loop_webhook import process_loop_collection_webhook

router = APIRouter(prefix="/webhooks/loop", tags=["Webhooks"])


@router.post(
    "/collection",
    response_model=LoopCollectionWebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Handle LOOP collection callback",
    description=(
        "Accept LOOP collection callbacks, verify signature and timestamp freshness, dedupe by "
        "provider reference, and confirm funding through inquiry before trusting callback data."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Callback signature is missing, invalid, or outside the replay window."
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "LOOP collection rail is unavailable."
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Callback payload was not valid JSON object content."
        },
    },
)
async def loop_collection_callback(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> LoopCollectionWebhookResponse:
    registry = getattr(request.app.state, "custody_registry", None)
    collection_rails = getattr(registry, "collection_rails", None)
    loop_rail = collection_rails.get("loop") if isinstance(collection_rails, Mapping) else None
    if loop_rail is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LOOP collection rail is unavailable.",
        )

    raw_bytes = await request.body()
    raw_payload = raw_bytes.decode("utf-8") if raw_bytes else "{}"

    parsed_payload: Any
    try:
        parsed_payload = json.loads(raw_payload) if raw_payload else {}
    except json.JSONDecodeError:
        parsed_payload = {"malformed_payload": raw_payload[:2000]}

    if not isinstance(parsed_payload, dict):
        parsed_payload = {"payload": parsed_payload}

    settings = get_settings()
    result = process_loop_collection_webhook(
        db,
        payload=parsed_payload,
        raw_payload=raw_payload,
        headers=request.headers,
        signing_secret=settings.loop_passkey,
        inquiry_status_fn=loop_rail.get_funding_status,
    )

    if not result.accepted:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=result.detail,
        )

    return LoopCollectionWebhookResponse(
        accepted=result.accepted,
        duplicate=result.duplicate,
        signature_valid=result.signature_valid,
        dedupe_key=result.dedupe_key,
        provider_reference=result.provider_reference,
        inquiry_outcome=result.inquiry_outcome,
        detail=result.detail,
    )
