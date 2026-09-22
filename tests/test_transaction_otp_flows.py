import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from app import main as app_main
from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.main import app
from app.models.dispute import Dispute, DisputeStatus, DisputeType
from app.models.listing import Listing
from app.models.notification import Notification, NotificationEventType
from app.models.transaction import Transaction, TransactionStatus
from app.models.user import User, UserRole
from app.models.user_session import UserSession
from app.routers import transactions as transactions_router
from app.services import transaction as transaction_service
from app.services.auth import ActiveSessionContext
from app.services.transaction import (
    InvalidTransactionOtpError,
    TransactionBuyerActionInvalidStateError,
    TransactionValidationError,
    run_timeout_jobs,
)
from app.services.transaction import (
    confirm_buyer_delivery_otp as confirm_buyer_delivery_otp_service,
)
from app.services.transaction import (
    report_functional_issue as report_functional_issue_service,
)
from app.services.transaction import (
    withhold_buyer_delivery_otp as withhold_buyer_delivery_otp_service,
)
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


class _FakeDb:
    def __init__(
        self,
        *,
        transactions: list[Transaction] | None = None,
        listings: list[Listing] | None = None,
    ) -> None:
        self.transactions = {transaction.id: transaction for transaction in transactions or []}
        self.listings = {listing.id: listing for listing in listings or []}
        self.added: list[Any] = []
        self.committed = False
        self.commit_count = 0
        self.refreshed = False

    def get(self, model: Any, model_id):
        if model is Transaction:
            return self.transactions.get(model_id)
        if model is Listing:
            return self.listings.get(model_id)
        return None

    def add(self, obj) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.committed = True
        self.commit_count += 1

    def refresh(self, obj) -> None:
        self.refreshed = True


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


def _build_listing(*, serialized: bool) -> Listing:
    return Listing(
        id=uuid.uuid4(),
        seller_id=uuid.uuid4(),
        title="Otp Listing",
        price=Decimal("4500.00"),
        is_serialized=serialized,
        unique_id="SER-001" if serialized else None,
        dispute_policy={"resolution": "seller_review"} if serialized else {},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _build_transaction(
    *,
    buyer_id: uuid.UUID,
    seller_id: uuid.UUID,
    listing_id: uuid.UUID,
    status: TransactionStatus,
    locked_at: datetime | None = None,
    at_door_at: datetime | None = None,
) -> Transaction:
    now = datetime.now(UTC)
    return Transaction(
        id=uuid.uuid4(),
        listing_id=listing_id,
        buyer_id=buyer_id,
        seller_id=seller_id,
        amount=Decimal("2200.00"),
        status=status,
        locked_at=locked_at,
        at_door_at=at_door_at,
        created_at=now,
        updated_at=now,
    )


def _build_session(user_id: uuid.UUID) -> UserSession:
    settings = get_settings()
    return UserSession(
        id=uuid.uuid4(),
        user_id=user_id,
        session_token_hash=hash_session_token("session-token", settings.secret_key),
        csrf_token_hash=hash_session_token("csrf-token", settings.secret_key),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        revoked_at=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _authenticated_client(monkeypatch, role: UserRole) -> TestClient:
    user = _build_user(role)
    session = _build_session(user.id)

    def fake_load_session_context(_session_token: str):
        return ActiveSessionContext(user=user, session=session)

    monkeypatch.setattr(app_main, "_load_session_context", fake_load_session_context)
    client = TestClient(app)
    settings = get_settings()
    client.cookies.set(settings.session_cookie_name, "session-token")
    client.cookies.set(settings.csrf_cookie_name, "csrf-token")
    return client


def _csrf_headers() -> dict[str, str]:
    settings = get_settings()
    return {settings.csrf_header_name: "csrf-token"}


def test_otp_give_requires_authentication() -> None:
    client = TestClient(app)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/otp-give",
        json={"otp_code": "123456"},
    )

    assert response.status_code == 401


def test_otp_give_rejects_seller_user(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.SELLER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/otp-give",
        headers=_csrf_headers(),
        json={"otp_code": "123456"},
    )

    assert response.status_code == 403


def test_otp_give_endpoint_returns_422_for_invalid_otp(monkeypatch) -> None:
    def fake_confirm_buyer_delivery_otp(_db, transaction_id, buyer, otp_code):
        raise InvalidTransactionOtpError("Invalid OTP provided.")

    monkeypatch.setattr(
        transactions_router,
        "confirm_buyer_delivery_otp",
        fake_confirm_buyer_delivery_otp,
    )

    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/otp-give",
        headers=_csrf_headers(),
        json={"otp_code": "999999"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid OTP provided."


def test_report_functional_issue_requires_authentication() -> None:
    client = TestClient(app)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/report-functional-issue",
        json={
            "category": "not_working",
            "description": "Power button does not respond.",
            "evidence": {},
        },
    )

    assert response.status_code == 401


def test_report_functional_issue_rejects_seller_user(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.SELLER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/report-functional-issue",
        headers=_csrf_headers(),
        json={
            "category": "not_working",
            "description": "Power button does not respond.",
            "evidence": {},
        },
    )

    assert response.status_code == 403


def test_report_functional_issue_endpoint_returns_422_for_invalid_state(monkeypatch) -> None:
    def fake_report_functional_issue(_db, transaction_id, buyer, category, description, evidence):
        raise TransactionBuyerActionInvalidStateError(
            "Transaction must be in hold_24h state for functional issue reporting."
        )

    monkeypatch.setattr(
        transactions_router,
        "report_functional_issue",
        fake_report_functional_issue,
    )

    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/report-functional-issue",
        headers=_csrf_headers(),
        json={
            "category": "not_working",
            "description": "Power button does not respond.",
            "evidence": {},
        },
    )

    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "Transaction must be in hold_24h state for functional issue reporting."
    )


def test_report_functional_issue_endpoint_returns_200(monkeypatch) -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.DISPUTED_FUNCTIONAL,
    )

    def fake_report_functional_issue(_db, transaction_id, buyer, category, description, evidence):
        return transaction

    monkeypatch.setattr(
        transactions_router,
        "report_functional_issue",
        fake_report_functional_issue,
    )

    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.post(
        f"/transactions/{transaction.id}/report-functional-issue",
        headers=_csrf_headers(),
        json={
            "category": "not_working",
            "description": "Power button does not respond.",
            "evidence": {"video": "https://example.com/evidence.mp4"},
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == TransactionStatus.DISPUTED_FUNCTIONAL.value


def test_otp_withhold_endpoint_returns_200(monkeypatch) -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=False)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.REFUNDED_BUYER,
    )

    def fake_withhold_buyer_delivery_otp(_db, transaction_id, buyer):
        return transaction

    monkeypatch.setattr(
        transactions_router,
        "withhold_buyer_delivery_otp",
        fake_withhold_buyer_delivery_otp,
    )

    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.post(
        f"/transactions/{transaction.id}/otp-withhold",
        headers=_csrf_headers(),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "refunded_buyer"


def test_service_otp_give_moves_non_serialized_to_released() -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=False)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.AT_DOOR_PENDING_INSPECTION,
        at_door_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    otp_code = "123456"
    transaction.delivery_otp_hash = hash_session_token(otp_code, get_settings().secret_key)

    db = _FakeDb(transactions=[transaction], listings=[listing])

    released = confirm_buyer_delivery_otp_service(
        cast(Session, db),
        transaction_id=transaction.id,
        buyer=buyer,
        otp_code=otp_code,
    )

    assert released.status == TransactionStatus.RELEASED
    assert released.released_at is not None
    assert released.hold_started_at is None
    assert released.delivery_otp_hash is None
    assert released.otp_failed_attempts == 0
    assert db.committed is True

    notifications = [item for item in db.added if isinstance(item, Notification)]
    assert len(notifications) == 2
    assert all(item.event_type == NotificationEventType.OTP_GIVEN for item in notifications)


def test_service_otp_give_moves_serialized_to_hold_24h() -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.AT_DOOR_PENDING_INSPECTION,
        at_door_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    transaction.delivery_otp_hash = hash_session_token("123456", get_settings().secret_key)

    db = _FakeDb(transactions=[transaction], listings=[listing])

    hold = confirm_buyer_delivery_otp_service(
        cast(Session, db),
        transaction_id=transaction.id,
        buyer=buyer,
        otp_code="123456",
    )

    assert hold.status == TransactionStatus.HOLD_24H
    assert hold.hold_started_at is not None
    assert hold.released_at is None
    assert hold.delivery_otp_hash is None
    assert hold.otp_failed_attempts == 0
    assert db.committed is True

    notifications = [item for item in db.added if isinstance(item, Notification)]
    assert len(notifications) == 2
    assert all(item.event_type == NotificationEventType.OTP_GIVEN for item in notifications)
    assert all(
        item.payload.get("transition", {}).get("to") == TransactionStatus.HOLD_24H.value
        for item in notifications
    )


def test_service_otp_give_invalid_otp_increments_attempts() -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=False)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.AT_DOOR_PENDING_INSPECTION,
        at_door_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    transaction.delivery_otp_hash = hash_session_token("123456", get_settings().secret_key)

    db = _FakeDb(transactions=[transaction], listings=[listing])

    with pytest.raises(InvalidTransactionOtpError, match="Invalid OTP provided."):
        confirm_buyer_delivery_otp_service(
            cast(Session, db),
            transaction_id=transaction.id,
            buyer=buyer,
            otp_code="000000",
        )

    assert transaction.otp_failed_attempts == 1
    assert db.committed is True


def test_service_report_functional_issue_moves_hold_serialized_to_disputed() -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.HOLD_24H,
    )
    transaction.hold_started_at = datetime.now(UTC) - timedelta(hours=2)

    db = _FakeDb(transactions=[transaction], listings=[listing])

    disputed = report_functional_issue_service(
        cast(Session, db),
        transaction_id=transaction.id,
        buyer=buyer,
        category="not working",
        description="Device powers off immediately after startup.",
        evidence={"video": "https://example.com/evidence.mp4"},
    )

    assert disputed.status == TransactionStatus.DISPUTED_FUNCTIONAL
    assert db.committed is True

    disputes = [item for item in db.added if isinstance(item, Dispute)]
    assert len(disputes) == 1
    dispute = disputes[0]
    assert dispute.dispute_type == DisputeType.FUNCTIONAL
    assert dispute.status == DisputeStatus.OPEN
    assert dispute.reason == "not_working"
    assert dispute.evidence == {"video": "https://example.com/evidence.mp4"}

    notifications = [item for item in db.added if isinstance(item, Notification)]
    assert len(notifications) == 2
    assert all(item.event_type == NotificationEventType.DISPUTE_OPENED for item in notifications)
    assert all(item.payload.get("category") == "not_working" for item in notifications)
    assert all(item.payload.get("route") == "seller_resolution" for item in notifications)


def test_service_report_functional_issue_routes_other_to_admin_escalation() -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.HOLD_24H,
    )
    transaction.hold_started_at = datetime.now(UTC) - timedelta(hours=3)

    db = _FakeDb(transactions=[transaction], listings=[listing])

    disputed = report_functional_issue_service(
        cast(Session, db),
        transaction_id=transaction.id,
        buyer=buyer,
        category="other",
        description="Unexpected intermittent failures observed.",
        evidence={"notes": "Happens after warmup."},
    )

    assert disputed.status == TransactionStatus.DISPUTED_FUNCTIONAL

    disputes = [item for item in db.added if isinstance(item, Dispute)]
    assert len(disputes) == 1
    dispute = disputes[0]
    assert dispute.status == DisputeStatus.ESCALATED_ADMIN_REVIEW
    assert dispute.escalated_at is not None

    notifications = [item for item in db.added if isinstance(item, Notification)]
    assert all(item.payload.get("route") == "admin_escalation" for item in notifications)


def test_service_report_functional_issue_rejects_non_hold_24h_state() -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.RELEASED,
    )

    db = _FakeDb(transactions=[transaction], listings=[listing])

    with pytest.raises(
        TransactionBuyerActionInvalidStateError,
        match="Transaction must be in hold_24h state for functional issue reporting.",
    ):
        report_functional_issue_service(
            cast(Session, db),
            transaction_id=transaction.id,
            buyer=buyer,
            category="not_working",
            description="No sound output.",
            evidence={},
        )


def test_service_report_functional_issue_rejects_non_serialized_listing() -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=False)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.HOLD_24H,
    )
    transaction.hold_started_at = datetime.now(UTC) - timedelta(hours=2)

    db = _FakeDb(transactions=[transaction], listings=[listing])

    with pytest.raises(
        TransactionBuyerActionInvalidStateError,
        match="Functional issue reporting is only supported for serialized listings.",
    ):
        report_functional_issue_service(
            cast(Session, db),
            transaction_id=transaction.id,
            buyer=buyer,
            category="not_working",
            description="No sound output.",
            evidence={},
        )


def test_service_report_functional_issue_rejects_unknown_category() -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.HOLD_24H,
    )
    transaction.hold_started_at = datetime.now(UTC) - timedelta(hours=2)

    db = _FakeDb(transactions=[transaction], listings=[listing])

    with pytest.raises(TransactionValidationError, match="Invalid issue category"):
        report_functional_issue_service(
            cast(Session, db),
            transaction_id=transaction.id,
            buyer=buyer,
            category="software_bug",
            description="App crashes repeatedly.",
            evidence={},
        )


def test_service_otp_withhold_moves_to_refunded_and_records_history() -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.AT_DOOR_PENDING_INSPECTION,
        at_door_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    db = _FakeDb(transactions=[transaction], listings=[listing])

    refunded = withhold_buyer_delivery_otp_service(
        cast(Session, db),
        transaction_id=transaction.id,
        buyer=buyer,
    )

    assert refunded.status == TransactionStatus.REFUNDED_BUYER
    assert refunded.refunded_at is not None
    assert db.committed is True

    notifications = [item for item in db.added if isinstance(item, Notification)]
    assert len(notifications) == 2
    assert all(item.event_type == NotificationEventType.OTP_WITHHELD for item in notifications)

    history = notifications[0].payload.get("transition_history")
    assert isinstance(history, list)
    assert [step["to"] for step in history] == [
        TransactionStatus.RETURN_IN_TRANSIT.value,
        TransactionStatus.RETURNED_TO_SELLER.value,
        TransactionStatus.REFUNDED_BUYER.value,
    ]


def test_released_state_cannot_reopen_withhold_flow() -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=False)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.RELEASED,
    )

    db = _FakeDb(transactions=[transaction], listings=[listing])

    with pytest.raises(
        TransactionBuyerActionInvalidStateError,
        match="Transaction must be in at_door_pending_inspection state for OTP withhold action.",
    ):
        withhold_buyer_delivery_otp_service(
            cast(Session, db),
            transaction_id=transaction.id,
            buyer=buyer,
        )


def test_timeout_jobs_apply_at_door_locked_and_hold_rules(monkeypatch) -> None:
    now = datetime.now(UTC)
    listing = _build_listing(serialized=False)

    at_door_txn = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.AT_DOOR_PENDING_INSPECTION,
        at_door_at=now - timedelta(hours=2),
    )
    locked_txn = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.LOCKED,
        locked_at=now - timedelta(hours=60),
    )
    hold_txn = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.HOLD_24H,
    )
    hold_txn.hold_started_at = now - timedelta(hours=26)

    db = _FakeDb(transactions=[at_door_txn, locked_txn, hold_txn], listings=[listing])

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

    result = run_timeout_jobs(cast(Session, db), now=now)

    assert result.at_door_timeout_count == 1
    assert result.no_dispatch_timeout_count == 1
    assert result.hold_auto_release_count == 1
    assert at_door_txn.status == TransactionStatus.REFUNDED_BUYER
    assert locked_txn.status == TransactionStatus.REFUNDED_BUYER
    assert hold_txn.status == TransactionStatus.RELEASED
    assert hold_txn.released_at == now
    assert db.committed is True

    notifications = [item for item in db.added if isinstance(item, Notification)]
    assert len(notifications) == 6
    assert all(item.event_type == NotificationEventType.SYSTEM_TIMEOUT for item in notifications)

    hold_events = [
        item for item in notifications if item.payload.get("reason") == "hold_24h_auto_release_24h"
    ]
    assert len(hold_events) == 2
    history = hold_events[0].payload.get("transition_history")
    assert isinstance(history, list)
    assert history == [
        {
            "from": TransactionStatus.HOLD_24H.value,
            "to": TransactionStatus.RELEASED.value,
            "at": now.isoformat(),
            "initiated_by": "system",
            "reason": "hold_24h_auto_release_24h",
        }
    ]


def test_timeout_jobs_hold_auto_release_is_idempotent(monkeypatch) -> None:
    now = datetime.now(UTC)
    listing = _build_listing(serialized=True)
    hold_txn = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.HOLD_24H,
    )
    hold_txn.hold_started_at = now - timedelta(hours=25)

    db = _FakeDb(transactions=[hold_txn], listings=[listing])

    monkeypatch.setattr(
        transaction_service,
        "_get_due_at_door_timeout_transactions",
        lambda _db, _cutoff: [],
    )
    monkeypatch.setattr(
        transaction_service,
        "_get_due_locked_timeout_transactions",
        lambda _db, _cutoff: [],
    )
    monkeypatch.setattr(
        transaction_service,
        "_get_due_hold_auto_release_transactions",
        lambda _db, _cutoff: [hold_txn] if hold_txn.status == TransactionStatus.HOLD_24H else [],
    )

    first = run_timeout_jobs(cast(Session, db), now=now)
    second = run_timeout_jobs(cast(Session, db), now=now + timedelta(minutes=5))

    assert first.hold_auto_release_count == 1
    assert second.hold_auto_release_count == 0
    assert hold_txn.status == TransactionStatus.RELEASED
    assert db.commit_count == 1


def test_timeout_jobs_noop_when_no_due_transactions(monkeypatch) -> None:
    db = _FakeDb()

    monkeypatch.setattr(
        transaction_service,
        "_get_due_at_door_timeout_transactions",
        lambda _db, _cutoff: [],
    )
    monkeypatch.setattr(
        transaction_service,
        "_get_due_locked_timeout_transactions",
        lambda _db, _cutoff: [],
    )
    monkeypatch.setattr(
        transaction_service,
        "_get_due_hold_auto_release_transactions",
        lambda _db, _cutoff: [],
    )

    result = run_timeout_jobs(cast(Session, db), now=datetime.now(UTC))

    assert result.at_door_timeout_count == 0
    assert result.no_dispatch_timeout_count == 0
    assert result.hold_auto_release_count == 0
    assert db.committed is False
