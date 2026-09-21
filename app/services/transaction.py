import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

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


@dataclass(frozen=True)
class PaymentCallbackResult:
    transaction: Transaction
    transitioned: bool
    duplicate: bool
    detail: str


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
) -> None:
    payload = {
        "transaction_id": str(transaction.id),
        "listing_id": str(transaction.listing_id),
        "at_door_at": transaction.at_door_at.isoformat() if transaction.at_door_at else None,
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

    transaction.status = TransactionStatus.AT_DOOR_PENDING_INSPECTION
    transaction.at_door_at = datetime.now(UTC)
    _create_arrival_notifications(db, transaction=transaction)
    db.commit()
    db.refresh(transaction)

    audit_logger.info(
        "transaction_transition transaction_id=%s from_status=out_for_delivery "
        "to_status=at_door_pending_inspection seller_id=%s",
        transaction.id,
        seller.id,
    )

    return transaction
