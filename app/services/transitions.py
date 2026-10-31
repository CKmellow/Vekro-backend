"""Status transitions, dispute events and admin decisions, each recorded as an anchor event in the
caller's DB transaction (M21 A3, docs/anchoring-design.md 3.3 to 3.5).

``transition_transaction`` is the only way the services change ``transactions.status``. The
flush guard in app/services/anchoring/recorder.py catches any other way. None of these commit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models.dispute import Dispute
from app.models.transaction import Transaction, TransactionStatus
from app.services.anchoring import payloads
from app.services.anchoring.recorder import note_status, record_anchor_event


def transition_transaction(
    db: Session,
    transaction: Transaction,
    to: TransactionStatus,
    *,
    actor_type: str,
    actor_id: uuid.UUID | None = None,
    reason: str,
    at: datetime | None = None,
    door_rejection_reason: str | None = None,
) -> None:
    previous = transaction.status
    transaction.status = to
    record_transition(
        db,
        transaction.id,
        from_status=getattr(previous, "value", previous),
        to_status=to.value,
        actor_type=actor_type,
        actor_id=actor_id,
        reason=reason,
        at=at,
        door_rejection_reason=door_rejection_reason,
    )


def record_transition(
    db: Session,
    transaction_id: uuid.UUID,
    *,
    from_status: str,
    to_status: str,
    actor_type: str,
    actor_id: uuid.UUID | None,
    reason: str,
    at: datetime | None = None,
    door_rejection_reason: str | None = None,
) -> None:
    record_anchor_event(
        db,
        event_type=payloads.TRANSACTION_TRANSITION,
        transaction_id=transaction_id,
        payload=payloads.transition_payload(
            transaction_id,
            from_status=from_status,
            to_status=to_status,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
            at=at or datetime.now(UTC),
            door_rejection_reason=door_rejection_reason,
        ),
    )
    note_status(db, transaction_id, to_status)


def record_dispute_event(
    db: Session,
    dispute: Dispute,
    event_type: str,
    *,
    actor_type: str,
    actor_id: uuid.UUID | None = None,
    at: datetime | None = None,
    **facts: Any,
) -> None:
    record_anchor_event(
        db,
        event_type=event_type,
        transaction_id=dispute.transaction_id,
        payload=payloads.dispute_payload(
            event_type,
            dispute.transaction_id,
            dispute.id,
            actor_type=actor_type,
            actor_id=actor_id,
            at=at or datetime.now(UTC),
            facts=facts,
        ),
    )


def record_admin_decision(
    db: Session,
    dispute: Dispute,
    *,
    decision: str,
    reason: str,
    admin_id: uuid.UUID | None,
    seller_share: Decimal | None,
    release_amount: Decimal | None,
    refund_amount: Decimal | None,
    at: datetime,
) -> None:
    record_anchor_event(
        db,
        event_type=payloads.ADMIN_DECISION,
        transaction_id=dispute.transaction_id,
        payload=payloads.admin_decision_payload(
            dispute.transaction_id,
            dispute.id,
            decision=decision,
            reason=reason,
            admin_id=admin_id,
            seller_share=seller_share,
            release_amount=release_amount,
            refund_amount=refund_amount,
            at=at,
        ),
    )
