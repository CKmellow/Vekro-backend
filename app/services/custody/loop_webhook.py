from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.provider_event import ProviderEvent
from app.services.custody.dto import CollectionResult
from app.services.custody.enums import CollectionOutcome
from app.services.custody.loop_auth import build_loop_signature

LOOP_PROVIDER_NAME = "loop"
LOOP_RAIL_NAME = "loop"

_PROVIDER_REFERENCE_KEYS = (
    "transactionReference",
    "transaction_reference",
    "providerReference",
    "provider_reference",
    "txnRef",
    "txn_reference",
)
_EXTERNAL_EVENT_ID_KEYS = (
    "eventId",
    "event_id",
    "callbackId",
    "callback_id",
    "requestId",
    "request_id",
)
_EVENT_TYPE_KEYS = (
    "eventType",
    "event_type",
    "type",
)
_NESTED_PAYLOAD_KEYS = (
    "data",
    "payload",
    "result",
    "body",
)
_SENSITIVE_HEADER_KEYS = {
    "authorization",
    "x-loop-signature",
    "x-loop-nonce",
}


@dataclass(frozen=True)
class LoopWebhookProcessResult:
    accepted: bool
    duplicate: bool
    signature_valid: bool
    dedupe_key: str
    provider_reference: str | None
    inquiry_outcome: CollectionOutcome | None
    detail: str


def process_loop_collection_webhook(
    db: Session,
    *,
    payload: Mapping[str, Any],
    raw_payload: str,
    headers: Mapping[str, str],
    signing_secret: str,
    inquiry_status_fn: Callable[[str], CollectionResult],
    logger: logging.Logger | None = None,
) -> LoopWebhookProcessResult:
    audit_logger = logger or logging.getLogger("app.custody.loop.webhook")
    now = datetime.now(UTC)
    body = _normalize_payload(payload)

    provider_reference = _extract_provider_reference(body)
    external_event_id = _extract_external_event_id(body)
    event_type = _extract_event_type(body)
    dedupe_key = _build_dedupe_key(
        provider_reference=provider_reference,
        external_event_id=external_event_id,
        raw_payload=raw_payload,
    )
    signature_valid = _verify_callback_signature(
        headers=headers,
        raw_payload=raw_payload,
        signing_secret=signing_secret,
    )

    existing = (
        db.execute(
            select(ProviderEvent).where(
                ProviderEvent.provider_name == LOOP_PROVIDER_NAME,
                ProviderEvent.dedupe_key == dedupe_key,
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return LoopWebhookProcessResult(
            accepted=True,
            duplicate=True,
            signature_valid=bool(existing.request_snapshot.get("signature_valid")),
            dedupe_key=dedupe_key,
            provider_reference=provider_reference,
            inquiry_outcome=_to_collection_outcome(
                existing.response_snapshot.get("inquiry_outcome")
            ),
            detail="Duplicate LOOP callback ignored.",
        )

    event = ProviderEvent(
        provider_name=LOOP_PROVIDER_NAME,
        rail_name=LOOP_RAIL_NAME,
        event_type=event_type,
        dedupe_key=dedupe_key,
        external_event_id=external_event_id,
        request_snapshot={
            "signature_valid": signature_valid,
            "headers": _redact_headers(headers),
            "provider_reference": provider_reference,
        },
        response_snapshot={},
        payload=_redact_payload(body),
        received_at=now,
        processed_at=None,
    )
    db.add(event)

    if not signature_valid:
        event.response_snapshot = {"state": "ignored_invalid_signature"}
        event.processed_at = now
        db.commit()
        return LoopWebhookProcessResult(
            accepted=True,
            duplicate=False,
            signature_valid=False,
            dedupe_key=dedupe_key,
            provider_reference=provider_reference,
            inquiry_outcome=None,
            detail="Callback signature verification failed; callback was ignored.",
        )

    if not provider_reference:
        event.response_snapshot = {"state": "ignored_missing_provider_reference"}
        event.processed_at = now
        db.commit()
        return LoopWebhookProcessResult(
            accepted=True,
            duplicate=False,
            signature_valid=True,
            dedupe_key=dedupe_key,
            provider_reference=None,
            inquiry_outcome=None,
            detail="Callback payload did not include a provider reference.",
        )

    try:
        inquiry_result = inquiry_status_fn(provider_reference)
    except Exception as exc:  # pragma: no cover
        event.response_snapshot = {
            "state": "inquiry_failed",
            "message": str(exc),
        }
        event.processed_at = now
        db.commit()
        audit_logger.warning(
            "loop_collection_callback_inquiry_failed dedupe_key=%s provider_reference=%s",
            dedupe_key,
            provider_reference,
        )
        return LoopWebhookProcessResult(
            accepted=True,
            duplicate=False,
            signature_valid=True,
            dedupe_key=dedupe_key,
            provider_reference=provider_reference,
            inquiry_outcome=None,
            detail="Callback recorded but inquiry confirmation failed.",
        )

    funding_confirmed = inquiry_result.outcome == CollectionOutcome.SUCCEEDED
    event.response_snapshot = {
        "state": "inquiry_confirmed",
        "inquiry": inquiry_result.to_payload(),
        "inquiry_outcome": inquiry_result.outcome.value,
        "funding_confirmed": funding_confirmed,
    }
    event.processed_at = now
    db.commit()

    return LoopWebhookProcessResult(
        accepted=True,
        duplicate=False,
        signature_valid=True,
        dedupe_key=dedupe_key,
        provider_reference=provider_reference,
        inquiry_outcome=inquiry_result.outcome,
        detail="Callback confirmed via LOOP inquiry.",
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


def _extract_event_type(payload: Mapping[str, Any]) -> str:
    event_type = _extract_first_string(payload, _EVENT_TYPE_KEYS)
    if event_type:
        return event_type

    nested = _extract_nested_string(payload, _EVENT_TYPE_KEYS)
    if nested:
        return nested

    return "collection.callback"


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


def _verify_callback_signature(
    *,
    headers: Mapping[str, str],
    raw_payload: str,
    signing_secret: str,
) -> bool:
    timestamp = _header_value(headers, "x-loop-timestamp")
    nonce = _header_value(headers, "x-loop-nonce")
    signature = _header_value(headers, "x-loop-signature")
    secret = signing_secret.strip()

    if not secret or not timestamp or not nonce or not signature:
        return False

    expected = build_loop_signature(
        secret=secret,
        timestamp=timestamp,
        nonce=nonce,
        payload=raw_payload,
    )
    return hmac.compare_digest(signature, expected)


def _header_value(headers: Mapping[str, str], header_name: str) -> str:
    expected = header_name.lower()
    for key, value in headers.items():
        if key.lower() == expected:
            return str(value).strip()
    return ""


def _redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key, value in headers.items():
        key_lower = key.lower().strip()
        if key_lower in _SENSITIVE_HEADER_KEYS:
            redacted[key_lower] = "***"
            continue
        if key_lower.startswith("x-loop-") or key_lower in {
            "content-type",
            "user-agent",
            "x-request-id",
        }:
            redacted[key_lower] = str(value)
    return redacted


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
