import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_session_token, verify_hashed_token
from app.core.settings import get_settings
from app.models.listing import Listing
from app.models.notification import Notification, NotificationEventType
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.schemas.transaction import (
    CreateTransactionRequest,
    PaymentCallbackRequest,
)
from app.services.payment import PaymentGateway, PaymentRequest, get_payment_gateway

audit_logger = logging.getLogger("app.transactions.audit")


class TransactionValidationError(Exception):
    pass


class ListingNotFoundForTransactionError(Exception):
    pass


class PaymentInitiationError(Exception):
    pass


class TransactionNotFoundForCallbackError(Exception):
    pass


class TransactionNotFoundForDispatchError(Exception):
    pass


class TransactionDispatchForbiddenError(Exception):
    pass


class TransactionDispatchInvalidStateError(Exception):
    pass


class TransactionNotFoundForArrivalError(Exception):
    pass


class TransactionArrivalForbiddenError(Exception):
    pass


class TransactionArrivalInvalidStateError(Exception):
    pass


class TransactionNotFoundForBuyerActionError(Exception):
    pass


class TransactionBuyerActionForbiddenError(Exception):
    pass


class TransactionBuyerActionInvalidStateError(Exception):
    pass


class InvalidTransactionOtpError(Exception):
    pass


@dataclass(frozen=True)
class PaymentCallbackResult:
    transaction: Transaction
    transitioned: bool
    duplicate: bool
    detail: str


@dataclass(frozen=True)
class TimeoutSweepResult:
    at_door_timeout_count: int
    no_dispatch_timeout_count: int


def create_transaction(
    db: Session,
    buyer: User,
    payload: CreateTransactionRequest,
    payment_gateway: PaymentGateway | None = None,
) -> Transaction:
    if buyer.role != UserRole.BUYER:
        raise TransactionValidationError("Only buyers can create transactions.")

    listing = db.get(Listing, payload.listing_id)
    if listing is None:
        raise ListingNotFoundForTransactionError("Listing not found.")

    amount = Decimal(payload.amount)

    transaction = Transaction(
        listing_id=listing.id,
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        amount=amount,
        status=TransactionStatus.AWAITING_PAYMENT,
    )
    db.add(transaction)
    db.commit()
    db.refresh(transaction)

    gateway = payment_gateway or get_payment_gateway()
    try:
        gateway.initiate_stk_push(
            PaymentRequest(
                transaction_id=str(transaction.id),
                amount=f"{transaction.amount:.2f}",
                phone_number=buyer.mpesa_phone,
                account_reference=str(transaction.id),
                transaction_desc=f"Escrow payment for listing {listing.id}",
            )
        )
    except Exception as exc:  # pragma: no cover
        raise PaymentInitiationError("Failed to initiate payment transport.") from exc

    return transaction


def _create_locked_notifications(
    db: Session,
    transaction: Transaction,
    callback: PaymentCallbackRequest,
) -> None:
    payload = {
        "transaction_id": str(transaction.id),
        "listing_id": str(transaction.listing_id),
        "result_code": callback.result_code,
        "result_desc": callback.result_desc,
        "checkout_request_id": callback.checkout_request_id,
        "merchant_request_id": callback.merchant_request_id,
        "provider_reference": callback.provider_reference,
        "locked_at": transaction.locked_at.isoformat() if transaction.locked_at else None,
    }

    buyer_notification = Notification(
        user_id=transaction.buyer_id,
        transaction_id=transaction.id,
        event_type=NotificationEventType.TRANSACTION_LOCKED,
        title="Payment confirmed",
        message="Your payment has been confirmed and the transaction is now locked.",
        payload=payload,
    )
    seller_notification = Notification(
        user_id=transaction.seller_id,
        transaction_id=transaction.id,
        event_type=NotificationEventType.TRANSACTION_LOCKED,
        title="Buyer payment confirmed",
        message="Buyer payment has been confirmed and the transaction is now locked.",
        payload=payload,
    )

    db.add(buyer_notification)
    db.add(seller_notification)


def _create_dispatched_notifications(
    db: Session,
    transaction: Transaction,
) -> None:
    payload = {
        "transaction_id": str(transaction.id),
        "listing_id": str(transaction.listing_id),
        "dispatched_at": (
            transaction.dispatched_at.isoformat() if transaction.dispatched_at else None
        ),
    }

    buyer_notification = Notification(
        user_id=transaction.buyer_id,
        transaction_id=transaction.id,
        event_type=NotificationEventType.TRANSACTION_DISPATCHED,
        title="Seller dispatched transaction",
        message="Your transaction has been dispatched and is out for delivery.",
        payload=payload,
    )
    seller_notification = Notification(
        user_id=transaction.seller_id,
        transaction_id=transaction.id,
        event_type=NotificationEventType.TRANSACTION_DISPATCHED,
        title="Dispatch confirmed",
        message="You have dispatched this transaction and it is now out for delivery.",
        payload=payload,
    )

    db.add(buyer_notification)
    db.add(seller_notification)


def _create_arrival_notifications(
    db: Session,
    transaction: Transaction,
    delivery_otp_preview: str | None,
) -> None:
    payload = {
        "transaction_id": str(transaction.id),
        "listing_id": str(transaction.listing_id),
        "at_door_at": transaction.at_door_at.isoformat() if transaction.at_door_at else None,
        "delivery_otp_preview": delivery_otp_preview,
    }

    buyer_notification = Notification(
        user_id=transaction.buyer_id,
        transaction_id=transaction.id,
        event_type=NotificationEventType.DELIVERY_ARRIVED,
        title="Delivery arrived",
        message="Your transaction has reached delivery and is pending inspection.",
        payload=payload,
    )
    seller_notification = Notification(
        user_id=transaction.seller_id,
        transaction_id=transaction.id,
        event_type=NotificationEventType.DELIVERY_ARRIVED,
        title="Arrival confirmed",
        message="Delivery arrival has been recorded and inspection is now pending.",
        payload=payload,
    )

    db.add(buyer_notification)
    db.add(seller_notification)


def _create_otp_given_notifications(
    db: Session,
    transaction: Transaction,
) -> None:
    target_status = transaction.status.value
    payload = {
        "transaction_id": str(transaction.id),
        "listing_id": str(transaction.listing_id),
        "hold_started_at": (
            transaction.hold_started_at.isoformat() if transaction.hold_started_at else None
        ),
        "released_at": transaction.released_at.isoformat() if transaction.released_at else None,
        "transition": {
            "from": TransactionStatus.AT_DOOR_PENDING_INSPECTION.value,
            "to": target_status,
        },
    }

    buyer_notification = Notification(
        user_id=transaction.buyer_id,
        transaction_id=transaction.id,
        event_type=NotificationEventType.OTP_GIVEN,
        title="OTP confirmed",
        message="OTP confirmed successfully and transaction moved to next stage.",
        payload=payload,
    )
    seller_notification = Notification(
        user_id=transaction.seller_id,
        transaction_id=transaction.id,
        event_type=NotificationEventType.OTP_GIVEN,
        title="Buyer confirmed OTP",
        message="Buyer OTP is confirmed and transaction moved to next stage.",
        payload=payload,
    )

    db.add(buyer_notification)
    db.add(seller_notification)


def _create_otp_withheld_notifications(
    db: Session,
    transaction: Transaction,
    transition_history: list[dict[str, str]],
    initiated_by: str,
    reason: str,
) -> None:
    payload = {
        "transaction_id": str(transaction.id),
        "listing_id": str(transaction.listing_id),
        "initiated_by": initiated_by,
        "reason": reason,
        "transition_history": transition_history,
        "refunded_at": transaction.refunded_at.isoformat() if transaction.refunded_at else None,
    }

    buyer_notification = Notification(
        user_id=transaction.buyer_id,
        transaction_id=transaction.id,
        event_type=NotificationEventType.OTP_WITHHELD,
        title="OTP withheld",
        message="OTP was withheld and the transaction moved to buyer refund resolution.",
        payload=payload,
    )
    seller_notification = Notification(
        user_id=transaction.seller_id,
        transaction_id=transaction.id,
        event_type=NotificationEventType.OTP_WITHHELD,
        title="Buyer withheld OTP",
        message="Buyer withheld OTP and the transaction moved to refund flow.",
        payload=payload,
    )

    db.add(buyer_notification)
    db.add(seller_notification)


def _create_timeout_notifications(
    db: Session,
    transaction: Transaction,
    transition_history: list[dict[str, str]],
    reason: str,
) -> None:
    payload = {
        "transaction_id": str(transaction.id),
        "listing_id": str(transaction.listing_id),
        "reason": reason,
        "transition_history": transition_history,
        "refunded_at": transaction.refunded_at.isoformat() if transaction.refunded_at else None,
    }

    buyer_notification = Notification(
        user_id=transaction.buyer_id,
        transaction_id=transaction.id,
        event_type=NotificationEventType.SYSTEM_TIMEOUT,
        title="System timeout transition",
        message="A timeout rule executed and updated your transaction workflow.",
        payload=payload,
    )
    seller_notification = Notification(
        user_id=transaction.seller_id,
        transaction_id=transaction.id,
        event_type=NotificationEventType.SYSTEM_TIMEOUT,
        title="System timeout transition",
        message="A timeout rule executed and updated this transaction workflow.",
        payload=payload,
    )

    db.add(buyer_notification)
    db.add(seller_notification)


def _generate_delivery_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _hash_delivery_otp(otp_code: str) -> str:
    return hash_session_token(otp_code, get_settings().secret_key)


def _verify_delivery_otp(otp_code: str, otp_hash: str | None) -> bool:
    if otp_hash is None:
        return False
    return verify_hashed_token(otp_code, otp_hash, get_settings().secret_key)


def _apply_withheld_return_refund_flow(
    transaction: Transaction,
    *,
    now: datetime,
    initiated_by: str,
    reason: str,
) -> list[dict[str, str]]:
    transition_history = [
        {
            "from": TransactionStatus.AT_DOOR_PENDING_INSPECTION.value,
            "to": TransactionStatus.RETURN_IN_TRANSIT.value,
            "at": now.isoformat(),
            "initiated_by": initiated_by,
            "reason": reason,
        },
        {
            "from": TransactionStatus.RETURN_IN_TRANSIT.value,
            "to": TransactionStatus.RETURNED_TO_SELLER.value,
            "at": now.isoformat(),
            "initiated_by": initiated_by,
            "reason": reason,
        },
        {
            "from": TransactionStatus.RETURNED_TO_SELLER.value,
            "to": TransactionStatus.REFUNDED_BUYER.value,
            "at": now.isoformat(),
            "initiated_by": initiated_by,
            "reason": reason,
        },
    ]

    transaction.status = TransactionStatus.RETURN_IN_TRANSIT
    transaction.status = TransactionStatus.RETURNED_TO_SELLER
    transaction.status = TransactionStatus.REFUNDED_BUYER
    transaction.refunded_at = now
    transaction.delivery_otp_hash = None
    transaction.otp_failed_attempts = 0
    return transition_history


def confirm_payment_callback(
    db: Session,
    payload: PaymentCallbackRequest,
) -> PaymentCallbackResult:
    transaction = db.get(Transaction, payload.transaction_id)
    if transaction is None:
        raise TransactionNotFoundForCallbackError("Transaction not found.")

    if payload.result_code != 0:
        audit_logger.warning(
            "payment_callback_not_successful transaction_id=%s status=%s "
            "result_code=%s result_desc=%s",
            transaction.id,
            transaction.status.value,
            payload.result_code,
            payload.result_desc or "",
        )
        return PaymentCallbackResult(
            transaction=transaction,
            transitioned=False,
            duplicate=False,
            detail="Payment callback not successful; transition skipped.",
        )

    if transaction.status == TransactionStatus.LOCKED:
        audit_logger.info(
            "payment_callback_duplicate_locked transaction_id=%s checkout_request_id=%s",
            transaction.id,
            payload.checkout_request_id or "",
        )
        return PaymentCallbackResult(
            transaction=transaction,
            transitioned=False,
            duplicate=True,
            detail="Duplicate callback ignored; transaction already locked.",
        )

    if transaction.status != TransactionStatus.AWAITING_PAYMENT:
        audit_logger.warning(
            "payment_callback_invalid_state transaction_id=%s current_status=%s result_code=%s",
            transaction.id,
            transaction.status.value,
            payload.result_code,
        )
        return PaymentCallbackResult(
            transaction=transaction,
            transitioned=False,
            duplicate=False,
            detail="Callback ignored because transaction is not awaiting payment.",
        )

    transaction.status = TransactionStatus.LOCKED
    transaction.locked_at = datetime.now(UTC)
    _create_locked_notifications(db, transaction=transaction, callback=payload)
    db.commit()
    db.refresh(transaction)

    audit_logger.info(
        "transaction_transition transaction_id=%s "
        "from_status=awaiting_payment to_status=locked result_code=%s "
        "checkout_request_id=%s",
        transaction.id,
        payload.result_code,
        payload.checkout_request_id or "",
    )

    return PaymentCallbackResult(
        transaction=transaction,
        transitioned=True,
        duplicate=False,
        detail="Transaction moved to locked.",
    )


def dispatch_transaction(
    db: Session,
    transaction_id: uuid.UUID,
    seller: User,
) -> Transaction:
    if seller.role != UserRole.SELLER:
        raise TransactionValidationError("Only sellers can dispatch transactions.")

    transaction = db.get(Transaction, transaction_id)
    if transaction is None:
        raise TransactionNotFoundForDispatchError("Transaction not found.")

    if transaction.seller_id != seller.id:
        raise TransactionDispatchForbiddenError(
            "Only the transaction seller can dispatch this transaction."
        )

    if transaction.status != TransactionStatus.LOCKED:
        raise TransactionDispatchInvalidStateError(
            "Transaction must be in locked state before dispatch."
        )

    transaction.status = TransactionStatus.OUT_FOR_DELIVERY
    transaction.dispatched_at = datetime.now(UTC)
    _create_dispatched_notifications(db, transaction=transaction)
    db.commit()
    db.refresh(transaction)

    audit_logger.info(
        "transaction_transition transaction_id=%s from_status=locked "
        "to_status=out_for_delivery seller_id=%s",
        transaction.id,
        seller.id,
    )

    return transaction


def mark_transaction_arrived(
    db: Session,
    transaction_id: uuid.UUID,
    seller: User,
) -> Transaction:
    if seller.role != UserRole.SELLER:
        raise TransactionValidationError("Only sellers can mark transaction arrival.")

    transaction = db.get(Transaction, transaction_id)
    if transaction is None:
        raise TransactionNotFoundForArrivalError("Transaction not found.")

    if transaction.seller_id != seller.id:
        raise TransactionArrivalForbiddenError(
            "Only the transaction seller can mark delivery arrival."
        )

    if transaction.status != TransactionStatus.OUT_FOR_DELIVERY:
        raise TransactionArrivalInvalidStateError(
            "Transaction must be in out_for_delivery state before arrival confirmation."
        )

    delivery_otp = _generate_delivery_otp()
    delivery_otp_preview = (
        delivery_otp if get_settings().environment.strip().lower() != "production" else None
    )

    transaction.status = TransactionStatus.AT_DOOR_PENDING_INSPECTION
    transaction.at_door_at = datetime.now(UTC)
    transaction.delivery_otp_hash = _hash_delivery_otp(delivery_otp)
    transaction.otp_failed_attempts = 0
    _create_arrival_notifications(
        db,
        transaction=transaction,
        delivery_otp_preview=delivery_otp_preview,
    )
    db.commit()
    db.refresh(transaction)

    audit_logger.info(
        "transaction_transition transaction_id=%s from_status=out_for_delivery "
        "to_status=at_door_pending_inspection seller_id=%s",
        transaction.id,
        seller.id,
    )

    return transaction


def confirm_buyer_delivery_otp(
    db: Session,
    transaction_id: uuid.UUID,
    buyer: User,
    otp_code: str,
) -> Transaction:
    if buyer.role != UserRole.BUYER:
        raise TransactionValidationError("Only buyers can confirm delivery OTP.")

    transaction = db.get(Transaction, transaction_id)
    if transaction is None:
        raise TransactionNotFoundForBuyerActionError("Transaction not found.")

    if transaction.buyer_id != buyer.id:
        raise TransactionBuyerActionForbiddenError(
            "Only the transaction buyer can confirm delivery OTP."
        )

    if transaction.status != TransactionStatus.AT_DOOR_PENDING_INSPECTION:
        raise TransactionBuyerActionInvalidStateError(
            "Transaction must be in at_door_pending_inspection state for OTP confirmation."
        )

    listing = db.get(Listing, transaction.listing_id)
    if listing is None:
        raise ListingNotFoundForTransactionError("Listing not found.")

    if not _verify_delivery_otp(otp_code, transaction.delivery_otp_hash):
        transaction.otp_failed_attempts = (transaction.otp_failed_attempts or 0) + 1
        db.commit()
        db.refresh(transaction)
        audit_logger.warning(
            "otp_confirmation_failed transaction_id=%s buyer_id=%s attempts=%s",
            transaction.id,
            buyer.id,
            transaction.otp_failed_attempts,
        )
        raise InvalidTransactionOtpError("Invalid OTP provided.")

    transitioned_at = datetime.now(UTC)
    if listing.is_serialized:
        transaction.status = TransactionStatus.HOLD_24H
        transaction.hold_started_at = transitioned_at
        transaction.released_at = None
        to_status = TransactionStatus.HOLD_24H.value
    else:
        transaction.status = TransactionStatus.RELEASED
        transaction.released_at = transitioned_at
        transaction.hold_started_at = None
        to_status = TransactionStatus.RELEASED.value

    transaction.delivery_otp_hash = None
    transaction.otp_failed_attempts = 0
    _create_otp_given_notifications(db, transaction=transaction)
    db.commit()
    db.refresh(transaction)

    audit_logger.info(
        "transaction_transition transaction_id=%s from_status=at_door_pending_inspection "
        "to_status=%s buyer_id=%s",
        transaction.id,
        to_status,
        buyer.id,
    )

    return transaction


def withhold_buyer_delivery_otp(
    db: Session,
    transaction_id: uuid.UUID,
    buyer: User,
) -> Transaction:
    if buyer.role != UserRole.BUYER:
        raise TransactionValidationError("Only buyers can withhold delivery OTP.")

    transaction = db.get(Transaction, transaction_id)
    if transaction is None:
        raise TransactionNotFoundForBuyerActionError("Transaction not found.")

    if transaction.buyer_id != buyer.id:
        raise TransactionBuyerActionForbiddenError(
            "Only the transaction buyer can withhold delivery OTP."
        )

    if transaction.status != TransactionStatus.AT_DOOR_PENDING_INSPECTION:
        raise TransactionBuyerActionInvalidStateError(
            "Transaction must be in at_door_pending_inspection state for OTP withhold action."
        )

    now = datetime.now(UTC)
    transition_history = _apply_withheld_return_refund_flow(
        transaction,
        now=now,
        initiated_by="buyer",
        reason="otp_withheld",
    )
    _create_otp_withheld_notifications(
        db,
        transaction=transaction,
        transition_history=transition_history,
        initiated_by="buyer",
        reason="otp_withheld",
    )
    db.commit()
    db.refresh(transaction)

    audit_logger.info(
        "transaction_withhold_flow transaction_id=%s buyer_id=%s history=%s",
        transaction.id,
        buyer.id,
        transition_history,
    )

    return transaction


def _get_due_at_door_timeout_transactions(db: Session, cutoff_time: datetime) -> list[Transaction]:
    return list(
        db.execute(
            select(Transaction).where(
                Transaction.status == TransactionStatus.AT_DOOR_PENDING_INSPECTION,
                Transaction.at_door_at.is_not(None),
                Transaction.at_door_at <= cutoff_time,
            )
        )
        .scalars()
        .all()
    )


def _get_due_locked_timeout_transactions(db: Session, cutoff_time: datetime) -> list[Transaction]:
    return list(
        db.execute(
            select(Transaction).where(
                Transaction.status == TransactionStatus.LOCKED,
                Transaction.locked_at.is_not(None),
                Transaction.locked_at <= cutoff_time,
            )
        )
        .scalars()
        .all()
    )


def run_timeout_jobs(
    db: Session,
    *,
    now: datetime | None = None,
) -> TimeoutSweepResult:
    current_time = now or datetime.now(UTC)
    at_door_cutoff = current_time - timedelta(hours=1)
    no_dispatch_cutoff = current_time - timedelta(hours=48)

    at_door_due = _get_due_at_door_timeout_transactions(db, at_door_cutoff)
    locked_due = _get_due_locked_timeout_transactions(db, no_dispatch_cutoff)

    at_door_timeout_count = 0
    no_dispatch_timeout_count = 0

    for transaction in at_door_due:
        transition_history = _apply_withheld_return_refund_flow(
            transaction,
            now=current_time,
            initiated_by="system",
            reason="at_door_timeout_1h",
        )
        _create_timeout_notifications(
            db,
            transaction=transaction,
            transition_history=transition_history,
            reason="at_door_timeout_1h",
        )
        at_door_timeout_count += 1
        audit_logger.info(
            "timeout_transition transaction_id=%s rule=at_door_timeout_1h history=%s",
            transaction.id,
            transition_history,
        )

    for transaction in locked_due:
        transition_history = [
            {
                "from": TransactionStatus.LOCKED.value,
                "to": TransactionStatus.REFUNDED_BUYER.value,
                "at": current_time.isoformat(),
                "initiated_by": "system",
                "reason": "locked_no_dispatch_timeout_48h",
            }
        ]
        transaction.status = TransactionStatus.REFUNDED_BUYER
        transaction.refunded_at = current_time
        transaction.delivery_otp_hash = None
        transaction.otp_failed_attempts = 0
        _create_timeout_notifications(
            db,
            transaction=transaction,
            transition_history=transition_history,
            reason="locked_no_dispatch_timeout_48h",
        )
        no_dispatch_timeout_count += 1
        audit_logger.info(
            "timeout_transition transaction_id=%s rule=locked_no_dispatch_timeout_48h history=%s",
            transaction.id,
            transition_history,
        )

    if at_door_timeout_count or no_dispatch_timeout_count:
        db.commit()

    return TimeoutSweepResult(
        at_door_timeout_count=at_door_timeout_count,
        no_dispatch_timeout_count=no_dispatch_timeout_count,
    )
