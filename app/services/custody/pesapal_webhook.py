from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.provider_event import ProviderEvent
from app.services.audit import ACTOR_PROVIDER, record_money_audit_event
from app.services.custody.dto import CollectionResult
from app.services.custody.enums import CollectionOutcome
from app.services.custody.pesapal_auth import PesapalAuthError
from app.services.custody.redaction import safe_error_message

PESAPAL_PROVIDER_NAME = "pesapal"
PESAPAL_RAIL_NAME = "pesapal"

_PROVIDER_REFERENCE_KEYS = (
    "OrderTrackingId",
    "orderTrackingId",
    "order_tracking_id",
    "tracking_id",
)
_EXTERNAL_EVENT_ID_KEYS = (
    "OrderNotificationType",
    "orderNotificationType",
    "order_notification_type",
    "notification_type",
)

_NESTED_PAYLOAD_KEYS = (
    "data",
    "payload",
    "result",
    "body",
)


@dataclass(frozen=True)
class PesapalWebhookProcessResult:
    accepted: bool
    duplicate: bool
    dedupe_key: str
    provider_reference: str | None
    inquiry_outcome: CollectionOutcome | None
    detail: str


def process_pesapal_collection_webhook(
    db: Session,
    *,
    payload: Mapping[str, Any],
    raw_payload: str,
    inquiry_status_fn: Callable[[str], CollectionResult],
    logger: logging.Logger | None = None,
) -> PesapalWebhookProcessResult:
    audit_logger = logger or logging.getLogger("app.custody.pesapal.webhook")
    now = datetime.now(UTC)
    body = _normalize_payload(payload)

    provider_reference = _extract_provider_reference(body)
    external_event_id = _extract_external_event_id(body)
    dedupe_key = _build_dedupe_key(
        provider_reference=provider_reference,
        external_event_id=external_event_id,
        raw_payload=raw_payload,
    )

    existing = (
        db.execute(
            select(ProviderEvent).where(
                ProviderEvent.provider_name == PESAPAL_PROVIDER_NAME,
                ProviderEvent.dedupe_key == dedupe_key,
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        audit_logger.info(
            "pesapal_collection_callback_replay_ignored dedupe_key=%s provider_reference=%s",
            dedupe_key,
            provider_reference,
        )
        return PesapalWebhookProcessResult(
            accepted=True,
            duplicate=True,
            dedupe_key=dedupe_key,
            provider_reference=provider_reference,
            inquiry_outcome=_to_collection_outcome(
                existing.response_snapshot.get("inquiry_outcome")
            ),
            detail="Duplicate Pesapal callback ignored.",
        )

    event = ProviderEvent(
        provider_name=PESAPAL_PROVIDER_NAME,
        rail_name=PESAPAL_RAIL_NAME,
        event_type="collection.callback",
        dedupe_key=dedupe_key,
        external_event_id=external_event_id,
        request_snapshot={
            "provider_reference": provider_reference,
        },
        response_snapshot={},
        payload=_redact_payload(body),
        received_at=now,
        processed_at=None,
    )
    db.add(event)

    if not provider_reference:
        event.response_snapshot = {"state": "ignored_missing_provider_reference"}
        event.processed_at = now
        db.commit()
        return PesapalWebhookProcessResult(
            accepted=True,
            duplicate=False,
            dedupe_key=dedupe_key,
            provider_reference=None,
            inquiry_outcome=None,
            detail="Callback payload did not include OrderTrackingId.",
        )

    try:
        inquiry_result = inquiry_status_fn(provider_reference)
    except (PesapalAuthError, RuntimeError, ValueError, TypeError, LookupError) as exc:
        event.response_snapshot = {
            "state": "inquiry_failed",
            "message": safe_error_message(exc),
        }
        event.processed_at = now
        db.commit()
        audit_logger.warning(
            "pesapal_collection_callback_inquiry_failed dedupe_key=%s provider_reference=%s",
            dedupe_key,
            provider_reference,
        )
        return PesapalWebhookProcessResult(
            accepted=True,
            duplicate=False,
            dedupe_key=dedupe_key,
            provider_reference=provider_reference,
            inquiry_outcome=None,
            detail="Callback recorded but status inquiry failed.",
        )

    funding_confirmed = inquiry_result.outcome == CollectionOutcome.SUCCEEDED
    event.response_snapshot = {
        "state": "inquiry_confirmed",
        "inquiry": inquiry_result.to_payload(),
        "inquiry_outcome": inquiry_result.outcome.value,
        "funding_confirmed": funding_confirmed,
    }
    event.processed_at = now
    if funding_confirmed:
        record_money_audit_event(
            db,
            action="funding_confirmed",
            actor_type=ACTOR_PROVIDER,
            reason="Pesapal callback confirmed via server-side status inquiry.",
            provider_reference=provider_reference,
            rail_name=PESAPAL_RAIL_NAME,
            details={"dedupe_key": dedupe_key},
            occurred_at=now,
        )
    db.commit()

    return PesapalWebhookProcessResult(
        accepted=True,
        duplicate=False,
        dedupe_key=dedupe_key,
        provider_reference=provider_reference,
        inquiry_outcome=inquiry_result.outcome,
        detail="Callback confirmed via Pesapal status inquiry.",
    )


def _normalize_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in payload.items()}


def _extract_provider_reference(payload: Mapping[str, Any]) -> str | None:
    direct = _extract_first_string(payload, _PROVIDER_REFERENCE_KEYS)
    if direct:
        return direct
    return _extract_nested_string(payload, _PROVIDER_REFERENCE_KEYS)


def _extract_external_event_id(payload: Mapping[str, Any]) -> str | None:
    direct = _extract_first_string(payload, _EXTERNAL_EVENT_ID_KEYS)
    if direct:
        return direct
    return _extract_nested_string(payload, _EXTERNAL_EVENT_ID_KEYS)


def _extract_nested_string(payload: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for container_key in _NESTED_PAYLOAD_KEYS:
        nested = payload.get(container_key)
        if isinstance(nested, Mapping):
            value = _extract_first_string(nested, keys)
            if value:
                return value
    return None


def _extract_first_string(payload: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        candidate = payload.get(key)
        if candidate is None:
            continue
        text = str(candidate).strip()
        if text:
            return text
    return None


def _build_dedupe_key(
    *,
    provider_reference: str | None,
    external_event_id: str | None,
    raw_payload: str,
) -> str:
    if provider_reference:
        return _truncate(f"collection:{provider_reference}", max_length=120)
    if external_event_id:
        return _truncate(f"event:{external_event_id}", max_length=120)

    digest = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()
    return _truncate(f"body:{digest}", max_length=120)


def _truncate(value: str, *, max_length: int) -> str:
    return value if len(value) <= max_length else value[:max_length]


def _to_collection_outcome(value: Any) -> CollectionOutcome | None:
    if value is None:
        return None
    try:
        return CollectionOutcome(str(value))
    except ValueError:
        return None


def _redact_payload(value: Any) -> Any:
    sensitive_keys = {
        "phone",
        "phonenumber",
        "msisdn",
        "token",
        "authorization",
        "signature",
        "passkey",
        "client_secret",
        "consumer_secret",
    }
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_lower = key.strip().lower().replace("-", "").replace("_", "")
            if key_lower in sensitive_keys:
                redacted[key] = "***"
            else:
                redacted[key] = _redact_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        if value.startswith("+") and value[1:].isdigit() and len(value) >= 8:
            return "***"
        return value
    if isinstance(value, Decimal):
        return str(value)
    return value