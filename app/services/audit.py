from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.money_audit_event import MoneyAuditEvent

ACTOR_USER = "user"
ACTOR_ADMIN = "admin"
ACTOR_SYSTEM = "system"
ACTOR_PROVIDER = "provider"

_AUDIT_TABLE = MoneyAuditEvent.__tablename__
_TABLE_READY_KEY = "money_audit_table_ready"
money_audit_logger = logging.getLogger("app.audit.money")


@dataclass(frozen=True)
class MoneyAuditQuery:
    transaction_id: uuid.UUID | None = None
    provider_reference: str | None = None
    rail_name: str | None = None
    action: str | None = None
    limit: int = 50
    offset: int = 0


def build_correlation_id(
    *,
    transaction_id: uuid.UUID | str | None,
    provider_reference: str | None,
    rail_name: str | None,
) -> str:
    return (
        f"txn={transaction_id or '-'}|ref={provider_reference or '-'}|rail={rail_name or '-'}"
    )


def record_money_audit_event(
    db: Session,
    *,
    action: str,
    actor_type: str,
    actor_id: uuid.UUID | None = None,
    reason: str | None = None,
    transaction_id: uuid.UUID | None = None,
    escrow_id: uuid.UUID | None = None,
    attempt_id: uuid.UUID | None = None,
    provider_reference: str | None = None,
    rail_name: str | None = None,
    amount: Decimal | None = None,
    currency: str | None = None,
    details: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> MoneyAuditEvent | None:
    correlation_id = build_correlation_id(
        transaction_id=transaction_id,
        provider_reference=provider_reference,
        rail_name=rail_name,
    )
    money_audit_logger.info(
        "money_audit action=%s actor_type=%s actor_id=%s transaction_id=%s "
        "provider_reference=%s rail=%s amount=%s correlation_id=%s",
        action,
        actor_type,
        actor_id or "-",
        transaction_id or "-",
        provider_reference or "-",
        rail_name or "-",
        amount if amount is not None else "-",
        correlation_id,
    )

    if not _audit_table_ready(db):
        return None

    event = MoneyAuditEvent(
        id=uuid.uuid4(),
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        reason=reason,
        transaction_id=transaction_id,
        escrow_id=escrow_id,
        attempt_id=attempt_id,
        provider_reference=provider_reference,
        rail_name=rail_name,
        correlation_id=correlation_id,
        amount=amount,
        currency=currency,
        details=details or {},
    )
    if occurred_at is not None:
        event.occurred_at = occurred_at
    db.add(event)
    return event


def list_money_audit_events(db: Session, query: MoneyAuditQuery) -> list[MoneyAuditEvent]:
    statement = select(MoneyAuditEvent)
    if query.transaction_id is not None:
        statement = statement.where(MoneyAuditEvent.transaction_id == query.transaction_id)
    if query.provider_reference:
        statement = statement.where(MoneyAuditEvent.provider_reference == query.provider_reference)
    if query.rail_name:
        statement = statement.where(MoneyAuditEvent.rail_name == query.rail_name)
    if query.action:
        statement = statement.where(MoneyAuditEvent.action == query.action)

    statement = (
        statement.order_by(MoneyAuditEvent.occurred_at.desc(), MoneyAuditEvent.id.desc())
        .limit(query.limit)
        .offset(query.offset)
    )
    return list(db.execute(statement).scalars().all())


def _audit_table_ready(db: Session) -> bool:
    info = getattr(db, "info", None)
    if isinstance(info, dict) and _TABLE_READY_KEY in info:
        return bool(info[_TABLE_READY_KEY])

    try:
        # Inspect via the session's own connection; a fresh engine connection can see a
        # different in-memory database and disturb the active transaction.
        ready = inspect(db.connection()).has_table(_AUDIT_TABLE)
    except (AttributeError, SQLAlchemyError):
        ready = False

    if isinstance(info, dict):
        info[_TABLE_READY_KEY] = ready
    return ready
