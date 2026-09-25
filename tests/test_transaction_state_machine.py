import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from app.models.dispute import Dispute, DisputeStatus
from app.models.listing import Listing
from app.models.notification import Notification, NotificationEventType
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.schemas.transaction import PaymentCallbackRequest
from app.services import transaction as transaction_service
from app.services.transaction import (
    TransactionArrivalInvalidStateError,
    TransactionBuyerActionInvalidStateError,
    TransactionDispatchInvalidStateError,
    apply_seller_resolution_action,
    confirm_buyer_delivery_otp,
    confirm_payment_callback,
    dispatch_transaction,
    mark_buyer_sent_back,
    mark_seller_received,
    mark_transaction_arrived,
    report_functional_issue,
    run_timeout_jobs,
    submit_buyer_reconfirmation,
)
from sqlalchemy.orm import Session


class _QueryResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def scalars(self):
        return self

    def first(self):
        return self._items[0] if self._items else None

    def all(self):
        return list(self._items)


class _FakeDb:
    def __init__(
        self,
        *,
        transactions: list[Transaction] | None = None,
        listings: list[Listing] | None = None,
        disputes: list[Dispute] | None = None,
    ) -> None:
        self.transactions = {item.id: item for item in transactions or []}
        self.listings = {item.id: item for item in listings or []}
        self.disputes = {item.id: item for item in disputes or []}
        self.added: list[Any] = []
        self.committed = False
        self.commit_count = 0
        self.refreshed = False

    def get(self, model: Any, model_id):
        if model is Transaction:
            return self.transactions.get(model_id)
        if model is Listing:
            return self.listings.get(model_id)
        if model is Dispute:
            return self.disputes.get(model_id)
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, Dispute):
            self.disputes[obj.id] = obj

    def commit(self) -> None:
        self.committed = True
        self.commit_count += 1

    def refresh(self, _obj: Any) -> None:
        self.refreshed = True

    def execute(self, _query: Any):
        ordered = sorted(self.disputes.values(), key=lambda item: item.created_at, reverse=True)
        return _QueryResult(ordered)


def _build_user(role: UserRole) -> User:
    return User(
        id=uuid.uuid4(),
        name=f"{role.value.title()} User",
        phone="+254700123456",
        role=role,
        password_hash="pbkdf2_sha256$1$abc$xyz",
        mpesa_phone=None,
        mpesa_account_name=None,
        session_version=1,
        is_active=True,
        failed_login_attempts=0,
        locked_until=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _build_listing(*, seller_id: uuid.UUID, serialized: bool) -> Listing:
    return Listing(
        id=uuid.uuid4(),
        seller_id=seller_id,
        title="Transition Listing",
        price=Decimal("5000.00"),
        is_serialized=serialized,
        unique_id="SER-500" if serialized else None,
        dispute_policy={
            "resolution": "seller_review",
            "allowed_seller_actions": ["refund_issued", "repair_shipped", "replacement_shipped"],
        },
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _build_transaction(
    *,
    buyer_id: uuid.UUID,
    seller_id: uuid.UUID,
    listing_id: uuid.UUID,
    status: TransactionStatus,
) -> Transaction:
    now = datetime.now(UTC)
    return Transaction(
        id=uuid.uuid4(),
        listing_id=listing_id,
        buyer_id=buyer_id,
        seller_id=seller_id,
        amount=Decimal("1900.00"),
        status=status,
        created_at=now,
        updated_at=now,
    )


def _build_callback_payload(transaction_id: uuid.UUID) -> PaymentCallbackRequest:
    return PaymentCallbackRequest(
        transaction_id=transaction_id,
        result_code=0,
        result_desc="The service request is processed successfully.",
        checkout_request_id="ws_CO_123456",
        merchant_request_id="29115-34620561-1",
        provider_reference="mpesa-stub-ref",
    )


def test_state_machine_core_escrow_happy_path_non_serialized(monkeypatch) -> None:
    buyer = _build_user(UserRole.BUYER)
    seller = _build_user(UserRole.SELLER)
    listing = _build_listing(seller_id=seller.id, serialized=False)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.AWAITING_PAYMENT,
    )

    db = _FakeDb(transactions=[transaction], listings=[listing])

    callback_result = confirm_payment_callback(
        cast(Session, db),
        payload=_build_callback_payload(transaction.id),
    )
    assert callback_result.transitioned is True
    assert transaction.status == TransactionStatus.LOCKED

    dispatched = dispatch_transaction(
        cast(Session, db),
        transaction_id=transaction.id,
        seller=seller,
    )
    assert dispatched.status == TransactionStatus.OUT_FOR_DELIVERY

    monkeypatch.setattr(transaction_service, "_generate_delivery_otp", lambda: "123456")
    arrived = mark_transaction_arrived(
        cast(Session, db),
        transaction_id=transaction.id,
        seller=seller,
    )
    assert arrived.status == TransactionStatus.AT_DOOR_PENDING_INSPECTION
    assert arrived.delivery_otp_hash is not None

    released = confirm_buyer_delivery_otp(
        cast(Session, db),
        transaction_id=transaction.id,
        buyer=buyer,
        otp_code="123456",
    )
    assert released.status == TransactionStatus.RELEASED
    assert released.released_at is not None


def test_state_machine_dispute_flow_rejection_escalates_on_second_retry() -> None:
    buyer = _build_user(UserRole.BUYER)
    seller = _build_user(UserRole.SELLER)
    listing = _build_listing(seller_id=seller.id, serialized=True)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.HOLD_24H,
    )
    transaction.hold_started_at = datetime.now(UTC) - timedelta(hours=2)

    db = _FakeDb(transactions=[transaction], listings=[listing])

    disputed = report_functional_issue(
        cast(Session, db),
        transaction_id=transaction.id,
        buyer=buyer,
        category="not_working",
        description="Power button fails after warm-up.",
        evidence={"video": "https://example.com/evidence.mp4"},
    )
    assert disputed.status == TransactionStatus.DISPUTED_FUNCTIONAL

    returned = mark_buyer_sent_back(
        cast(Session, db),
        transaction_id=transaction.id,
        buyer=buyer,
    )
    assert returned.status == TransactionStatus.RETURN_IN_TRANSIT

    received = mark_seller_received(
        cast(Session, db),
        transaction_id=transaction.id,
        seller=seller,
    )
    assert received.status == TransactionStatus.RETURN_RECEIVED

    waiting = apply_seller_resolution_action(
        cast(Session, db),
        transaction_id=transaction.id,
        seller=seller,
        action="repair_shipped",
        notes="Repair sent.",
    )
    assert waiting.status == TransactionStatus.AWAITING_BUYER_RECONFIRMATION

    first_reject = submit_buyer_reconfirmation(
        cast(Session, db),
        transaction_id=transaction.id,
        buyer=buyer,
        accepted=False,
        notes="Still not working.",
    )
    assert first_reject.status == TransactionStatus.RETURN_RECEIVED

    waiting_again = apply_seller_resolution_action(
        cast(Session, db),
        transaction_id=transaction.id,
        seller=seller,
        action="replacement_shipped",
        notes="Replacement shipped.",
    )
    assert waiting_again.status == TransactionStatus.AWAITING_BUYER_RECONFIRMATION

    escalated = submit_buyer_reconfirmation(
        cast(Session, db),
        transaction_id=transaction.id,
        buyer=buyer,
        accepted=False,
        notes="Second rejection.",
    )
    assert escalated.status == TransactionStatus.ESCALATED_ADMIN_REVIEW

    dispute = sorted(db.disputes.values(), key=lambda item: item.created_at, reverse=True)[0]
    assert dispute.status == DisputeStatus.ESCALATED_ADMIN_REVIEW
    assert dispute.evidence.get("buyer_reconfirm_rejection_count") == 2


def test_state_machine_rejects_invalid_dispatch_transition() -> None:
    seller = _build_user(UserRole.SELLER)
    listing = _build_listing(seller_id=seller.id, serialized=False)
    transaction = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.AWAITING_PAYMENT,
    )
    db = _FakeDb(transactions=[transaction], listings=[listing])

    with pytest.raises(
        TransactionDispatchInvalidStateError,
        match="Transaction must be in locked state before dispatch.",
    ):
        dispatch_transaction(
            cast(Session, db),
            transaction_id=transaction.id,
            seller=seller,
        )


def test_state_machine_rejects_invalid_arrival_transition() -> None:
    seller = _build_user(UserRole.SELLER)
    listing = _build_listing(seller_id=seller.id, serialized=False)
    transaction = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.LOCKED,
    )
    db = _FakeDb(transactions=[transaction], listings=[listing])

    with pytest.raises(
        TransactionArrivalInvalidStateError,
        match="Transaction must be in out_for_delivery state before arrival confirmation.",
    ):
        mark_transaction_arrived(
            cast(Session, db),
            transaction_id=transaction.id,
            seller=seller,
        )


def test_state_machine_rejects_invalid_otp_confirmation_transition() -> None:
    buyer = _build_user(UserRole.BUYER)
    seller = _build_user(UserRole.SELLER)
    listing = _build_listing(seller_id=seller.id, serialized=False)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.OUT_FOR_DELIVERY,
    )
    db = _FakeDb(transactions=[transaction], listings=[listing])

    with pytest.raises(
        TransactionBuyerActionInvalidStateError,
        match=("Transaction must be in at_door_pending_inspection state for OTP confirmation."),
    ):
        confirm_buyer_delivery_otp(
            cast(Session, db),
            transaction_id=transaction.id,
            buyer=buyer,
            otp_code="123456",
        )


def test_state_machine_timeout_rules_cover_critical_paths(monkeypatch) -> None:
    now = datetime.now(UTC)
    seller = _build_user(UserRole.SELLER)
    listing = _build_listing(seller_id=seller.id, serialized=True)

    at_door_txn = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.AT_DOOR_PENDING_INSPECTION,
    )
    at_door_txn.at_door_at = now - timedelta(hours=2)

    locked_txn = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.LOCKED,
    )
    locked_txn.locked_at = now - timedelta(hours=60)

    hold_txn = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.HOLD_24H,
    )
    hold_txn.hold_started_at = now - timedelta(hours=26)

    dispute_txn = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.DISPUTED_FUNCTIONAL,
    )
    dispute_txn.updated_at = now - timedelta(days=4)

    seller_timeout_txn = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.RETURN_IN_TRANSIT,
    )
    seller_timeout_txn.updated_at = now - timedelta(days=4)

    db = _FakeDb(
        transactions=[at_door_txn, locked_txn, hold_txn, dispute_txn, seller_timeout_txn],
        listings=[listing],
    )

    monkeypatch.setattr(
        transaction_service,
        "_get_due_at_door_timeout_transactions",
        lambda _db, _cutoff: [at_door_txn],
    )
    monkeypatch.setattr(
        transaction_service,
        "_get_due_locked_timeout_transactions",
        lambda _db, _cutoff: [locked_txn],
    )
    monkeypatch.setattr(
        transaction_service,
        "_get_due_hold_auto_release_transactions",
        lambda _db, _cutoff: [hold_txn],
    )
    monkeypatch.setattr(
        transaction_service,
        "_get_due_dispute_buyer_sent_back_timeout_transactions",
        lambda _db, _cutoff: [dispute_txn],
    )
    monkeypatch.setattr(
        transaction_service,
        "_get_due_seller_received_timeout_transactions",
        lambda _db, _cutoff: [seller_timeout_txn],
    )

    result = run_timeout_jobs(cast(Session, db), now=now)

    assert result.at_door_timeout_count == 1
    assert result.no_dispatch_timeout_count == 1
    assert result.hold_auto_release_count == 1
    assert result.dispute_buyer_sent_back_timeout_count == 1
    assert result.dispute_seller_received_timeout_count == 1

    assert at_door_txn.status == TransactionStatus.REFUNDED_BUYER
    assert locked_txn.status == TransactionStatus.REFUNDED_BUYER
    assert hold_txn.status == TransactionStatus.RELEASED
    assert dispute_txn.status == TransactionStatus.RELEASED
    assert seller_timeout_txn.status == TransactionStatus.ESCALATED_ADMIN_REVIEW

    timeout_events = [item for item in db.added if isinstance(item, Notification)]
    assert len(timeout_events) == 10
    assert all(item.event_type == NotificationEventType.SYSTEM_TIMEOUT for item in timeout_events)


def test_state_machine_locked_timeout_rule_is_idempotent(monkeypatch) -> None:
    now = datetime.now(UTC)
    seller = _build_user(UserRole.SELLER)
    listing = _build_listing(seller_id=seller.id, serialized=False)
    locked_txn = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.LOCKED,
    )
    locked_txn.locked_at = now - timedelta(hours=60)

    db = _FakeDb(transactions=[locked_txn], listings=[listing])

    monkeypatch.setattr(
        transaction_service,
        "_get_due_at_door_timeout_transactions",
        lambda _db, _cutoff: [],
    )
    monkeypatch.setattr(
        transaction_service,
        "_get_due_locked_timeout_transactions",
        lambda _db, _cutoff: [locked_txn] if locked_txn.status == TransactionStatus.LOCKED else [],
    )
    monkeypatch.setattr(
        transaction_service,
        "_get_due_hold_auto_release_transactions",
        lambda _db, _cutoff: [],
    )
    monkeypatch.setattr(
        transaction_service,
        "_get_due_dispute_buyer_sent_back_timeout_transactions",
        lambda _db, _cutoff: [],
    )
    monkeypatch.setattr(
        transaction_service,
        "_get_due_seller_received_timeout_transactions",
        lambda _db, _cutoff: [],
    )

    first = run_timeout_jobs(cast(Session, db), now=now)
    second = run_timeout_jobs(cast(Session, db), now=now + timedelta(minutes=2))

    assert first.no_dispatch_timeout_count == 1
    assert second.no_dispatch_timeout_count == 0
    assert locked_txn.status == TransactionStatus.REFUNDED_BUYER
    assert db.commit_count == 1
