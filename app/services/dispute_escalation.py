"""Either party can hand an open return case to Vekro support."""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.dispute import DisputeStatus
from app.models.notification import Notification, NotificationEventType
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User
from app.services.dispute_summary import latest_dispute

audit_logger = logging.getLogger("app.transactions.audit")

OPEN_CASE = frozenset(
    {
        TransactionStatus.DISPUTED_FUNCTIONAL,
        TransactionStatus.RETURN_IN_TRANSIT,
        TransactionStatus.RETURN_RECEIVED,
        TransactionStatus.AWAITING_BUYER_RECONFIRMATION,
    }
)


class EscalationNotFoundError(Exception):
    pass


class EscalationForbiddenError(Exception):
    pass


class EscalationInvalidStateError(Exception):
    pass


def escalate_dispute(
    db: Session, transaction_id: uuid.UUID, user: User, *, reason: str
) -> Transaction:
    transaction = db.get(Transaction, transaction_id)
    if transaction is None:
        raise EscalationNotFoundError("Transaction not found.")
    if user.id == transaction.buyer_id:
        role = "buyer"
    elif user.id == transaction.seller_id:
        role = "seller"
    else:
        raise EscalationForbiddenError("Only the buyer or the seller can escalate this case.")
    if transaction.status not in OPEN_CASE:
        raise EscalationInvalidStateError("Only an open return case can go to Vekro support.")
    dispute = latest_dispute(db, transaction.id)
    if dispute is None:
        raise EscalationInvalidStateError("This order has no return case.")

    now = datetime.now(UTC)
    previous = transaction.status
    transaction.status = TransactionStatus.ESCALATED_ADMIN_REVIEW
    dispute.status = DisputeStatus.ESCALATED_ADMIN_REVIEW
    dispute.escalated_at = now
    evidence = dict(dispute.evidence or {})
    evidence["escalation"] = {"by": role, "reason": reason.strip(), "at": now.isoformat()}
    dispute.evidence = evidence

    payload = {
        "transaction_id": str(transaction.id),
        "listing_id": str(transaction.listing_id),
        "escalated_by": role,
        "transition_history": [
            {
                "from": previous.value,
                "to": TransactionStatus.ESCALATED_ADMIN_REVIEW.value,
                "at": now.isoformat(),
                "initiated_by": role,
                "reason": "party_escalated",
            }
        ],
    }
    held = "The money stays in escrow until they decide."
    for user_id, recipient in ((transaction.buyer_id, "buyer"), (transaction.seller_id, "seller")):
        mine = recipient == role
        db.add(
            Notification(
                user_id=user_id,
                transaction_id=transaction.id,
                event_type=NotificationEventType.DISPUTE_UPDATED,
                title="Sent to Vekro support",
                message=(
                    f"You asked Vekro support to review this case. {held}"
                    if mine
                    else f"The {role} asked Vekro support to review this case. {held}"
                ),
                payload={**payload, "recipient": recipient},
            )
        )
    db.commit()
    db.refresh(transaction)
    audit_logger.info(
        "transaction_transition transaction_id=%s from_status=%s to_status=escalated_admin_review "
        "actor=%s reason=party_escalated",
        transaction.id,
        previous.value,
        role,
    )
    return transaction
