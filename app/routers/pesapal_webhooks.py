from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.pesapal_webhook import PesapalCollectionWebhookResponse
from app.services.custody.pesapal_webhook import process_pesapal_collection_webhook

router = APIRouter(prefix="/api/webhooks/pesapal", tags=["Webhooks"])


@router.get(
    "/callback",
    operation_id="pesapal_collection_callback_get",
    response_model=PesapalCollectionWebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Handle Pesapal collection callback trigger",
    description=(
        "Accept Pesapal callback notifications and always confirm funding finality through "
        "server-side transaction status inquiry. Callback payload fields are never trusted "
        "as final state by themselves."
    ),
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Pesapal collection rail is unavailable."
        },
    },
)
async def pesapal_collection_callback_get(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> PesapalCollectionWebhookResponse:
    return await _handle_pesapal_collection_callback(request=request, db=db)


@router.post(
    "/callback",
    operation_id="pesapal_collection_callback_post",
    response_model=PesapalCollectionWebhookResponse,
    status_code=status.HTTP_200_OK,
    summary="Handle Pesapal collection callback trigger",
    description=(
        "Accept Pesapal callback notifications and always confirm funding finality through "
        "server-side transaction status inquiry. Callback payload fields are never trusted "
        "as final state by themselves."
    ),
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Pesapal collection rail is unavailable."
        },
    },
)
async def pesapal_collection_callback_post(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> PesapalCollectionWebhookResponse:
    return await _handle_pesapal_collection_callback(request=request, db=db)


async def _handle_pesapal_collection_callback(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> PesapalCollectionWebhookResponse:
    registry = getattr(request.app.state, "custody_registry", None)
    collection_rails = getattr(registry, "collection_rails", None)
    pesapal_rail = collection_rails.get("pesapal") if isinstance(collection_rails, Mapping) else None
    if pesapal_rail is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Pesapal collection rail is unavailable.",
        )

    query_payload: dict[str, Any] = {str(key): value for key, value in request.query_params.items()}

    raw_bytes = await request.body()
    raw_payload = raw_bytes.decode("utf-8") if raw_bytes else request.url.query

    body_payload: dict[str, Any] = {}
    if raw_payload:
        try:
            parsed = json.loads(raw_payload)
            if isinstance(parsed, dict):
                body_payload = {str(key): value for key, value in parsed.items()}
        except json.JSONDecodeError:
            body_payload = {}

    payload = {**query_payload, **body_payload}
    if not payload and raw_payload:
        payload = {"raw_payload": raw_payload[:2000]}

    result = process_pesapal_collection_webhook(
        db,
        payload=payload,
        raw_payload=raw_payload or json.dumps(payload, separators=(",", ":"), sort_keys=True),
        inquiry_status_fn=pesapal_rail.get_funding_status,
    )

    return PesapalCollectionWebhookResponse(
        accepted=result.accepted,
        duplicate=result.duplicate,
        dedupe_key=result.dedupe_key,
        provider_reference=result.provider_reference,
        inquiry_outcome=result.inquiry_outcome,
        detail=result.detail,
    )