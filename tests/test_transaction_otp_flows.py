import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from app import main as app_main
from app.core.security import hash_session_token
from app.core.settings import get_settings
from app.main import app
from app.models.dispute import Dispute, DisputeStatus, DisputeType, SellerResolutionAction
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
    TransactionDispatchInvalidStateError,
    TransactionValidationError,
    run_timeout_jobs,
)
from app.services.transaction import (
    apply_seller_resolution_action as apply_seller_resolution_action_service,
)
from app.services.transaction import (
    confirm_buyer_delivery_otp as confirm_buyer_delivery_otp_service,
)
from app.services.transaction import (
    mark_buyer_sent_back as mark_buyer_sent_back_service,
)
from app.services.transaction import (
    mark_seller_received as mark_seller_received_service,
)
from app.services.transaction import (
    report_functional_issue as report_functional_issue_service,
)
from app.services.transaction import (
    submit_buyer_reconfirmation as submit_buyer_reconfirmation_service,
)
from app.services.transaction import (
    submit_buyer_resolution_confirmation as submit_buyer_resolution_confirmation_service,
)
from app.services.transaction import (
    submit_seller_resolution_confirmation as submit_seller_resolution_confirmation_service,
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


def _build_listing_with_allowed_actions(*, serialized: bool, allowed_actions: list[str]) -> Listing:
    listing = _build_listing(serialized=serialized)
    listing.dispute_policy = {
        "resolution": "seller_review",
        "allowed_seller_actions": allowed_actions,
    }
    return listing


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


def test_buyer_sent_back_requires_authentication() -> None:
    client = TestClient(app)
    response = client.post(f"/transactions/{uuid.uuid4()}/buyer-sent-back")

    assert response.status_code == 401


def test_buyer_sent_back_rejects_seller_user(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.SELLER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/buyer-sent-back",
        headers=_csrf_headers(),
    )

    assert response.status_code == 403


def test_buyer_sent_back_endpoint_returns_422_for_invalid_state(monkeypatch) -> None:
    def fake_mark_buyer_sent_back(_db, transaction_id, buyer):
        raise TransactionBuyerActionInvalidStateError(
            "Transaction must be in disputed_functional state for buyer sent-back action."
        )

    monkeypatch.setattr(
        transactions_router,
        "mark_buyer_sent_back",
        fake_mark_buyer_sent_back,
    )

    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/buyer-sent-back",
        headers=_csrf_headers(),
    )

    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "Transaction must be in disputed_functional state for buyer sent-back action."
    )


def test_buyer_sent_back_endpoint_returns_200(monkeypatch) -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.RETURN_IN_TRANSIT,
    )

    def fake_mark_buyer_sent_back(_db, transaction_id, buyer):
        return transaction

    monkeypatch.setattr(
        transactions_router,
        "mark_buyer_sent_back",
        fake_mark_buyer_sent_back,
    )

    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.post(
        f"/transactions/{transaction.id}/buyer-sent-back",
        headers=_csrf_headers(),
    )

    assert response.status_code == 200
    assert response.json()["status"] == TransactionStatus.RETURN_IN_TRANSIT.value


def test_seller_received_requires_authentication() -> None:
    client = TestClient(app)
    response = client.post(f"/transactions/{uuid.uuid4()}/seller-received")

    assert response.status_code == 401


def test_seller_received_rejects_buyer_user(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/seller-received",
        headers=_csrf_headers(),
    )

    assert response.status_code == 403


def test_seller_received_endpoint_returns_422_for_invalid_state(monkeypatch) -> None:
    def fake_mark_seller_received(_db, transaction_id, seller):
        raise TransactionDispatchInvalidStateError(
            "Transaction must be in return_in_transit state for seller received action."
        )

    monkeypatch.setattr(
        transactions_router,
        "mark_seller_received",
        fake_mark_seller_received,
    )

    client = _authenticated_client(monkeypatch, UserRole.SELLER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/seller-received",
        headers=_csrf_headers(),
    )

    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "Transaction must be in return_in_transit state for seller received action."
    )


def test_seller_received_endpoint_returns_200(monkeypatch) -> None:
    seller = _build_user(UserRole.SELLER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.RETURN_RECEIVED,
    )

    def fake_mark_seller_received(_db, transaction_id, seller):
        return transaction

    monkeypatch.setattr(
        transactions_router,
        "mark_seller_received",
        fake_mark_seller_received,
    )

    client = _authenticated_client(monkeypatch, UserRole.SELLER)
    response = client.post(
        f"/transactions/{transaction.id}/seller-received",
        headers=_csrf_headers(),
    )

    assert response.status_code == 200
    assert response.json()["status"] == TransactionStatus.RETURN_RECEIVED.value


def test_seller_resolution_action_requires_authentication() -> None:
    client = TestClient(app)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/seller-resolution-action",
        json={"action": "refund_issued"},
    )

    assert response.status_code == 401


def test_seller_resolution_action_rejects_buyer_user(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/seller-resolution-action",
        headers=_csrf_headers(),
        json={"action": "refund_issued"},
    )

    assert response.status_code == 403


def test_seller_resolution_action_endpoint_returns_422_for_invalid_state(monkeypatch) -> None:
    def fake_apply_seller_resolution_action(_db, transaction_id, seller, action, notes):
        raise TransactionDispatchInvalidStateError(
            "Transaction must be in return_received state for seller resolution actions."
        )

    monkeypatch.setattr(
        transactions_router,
        "apply_seller_resolution_action",
        fake_apply_seller_resolution_action,
    )

    client = _authenticated_client(monkeypatch, UserRole.SELLER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/seller-resolution-action",
        headers=_csrf_headers(),
        json={"action": "refund_issued"},
    )

    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "Transaction must be in return_received state for seller resolution actions."
    )


def test_seller_resolution_action_endpoint_returns_200(monkeypatch) -> None:
    seller = _build_user(UserRole.SELLER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.REFUNDED_BUYER,
    )

    def fake_apply_seller_resolution_action(_db, transaction_id, seller, action, notes):
        return transaction

    monkeypatch.setattr(
        transactions_router,
        "apply_seller_resolution_action",
        fake_apply_seller_resolution_action,
    )

    client = _authenticated_client(monkeypatch, UserRole.SELLER)
    response = client.post(
        f"/transactions/{transaction.id}/seller-resolution-action",
        headers=_csrf_headers(),
        json={"action": "refund_issued", "notes": "Refund processed"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == TransactionStatus.REFUNDED_BUYER.value


def test_buyer_reconfirmation_requires_authentication() -> None:
    client = TestClient(app)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/buyer-reconfirmation",
        json={"accepted": True},
    )

    assert response.status_code == 401


def test_buyer_reconfirmation_rejects_seller_user(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.SELLER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/buyer-reconfirmation",
        headers=_csrf_headers(),
        json={"accepted": True},
    )

    assert response.status_code == 403


def test_buyer_reconfirmation_endpoint_returns_422_for_invalid_state(monkeypatch) -> None:
    def fake_submit_buyer_reconfirmation(_db, transaction_id, buyer, accepted, notes):
        raise TransactionBuyerActionInvalidStateError(
            "Transaction must be in awaiting_buyer_reconfirmation state for buyer reconfirmation."
        )

    monkeypatch.setattr(
        transactions_router,
        "submit_buyer_reconfirmation",
        fake_submit_buyer_reconfirmation,
    )

    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/buyer-reconfirmation",
        headers=_csrf_headers(),
        json={"accepted": False},
    )

    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "Transaction must be in awaiting_buyer_reconfirmation state for buyer reconfirmation."
    )


def test_buyer_reconfirmation_endpoint_returns_200(monkeypatch) -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.RESOLVED_RELEASE,
    )

    def fake_submit_buyer_reconfirmation(_db, transaction_id, buyer, accepted, notes):
        return transaction

    monkeypatch.setattr(
        transactions_router,
        "submit_buyer_reconfirmation",
        fake_submit_buyer_reconfirmation,
    )

    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.post(
        f"/transactions/{transaction.id}/buyer-reconfirmation",
        headers=_csrf_headers(),
        json={"accepted": True, "notes": "Accepted repair shipment."},
    )

    assert response.status_code == 200
    assert response.json()["status"] == TransactionStatus.RESOLVED_RELEASE.value


def test_confirm_resolved_buyer_requires_authentication() -> None:
    client = TestClient(app)
    response = client.post(f"/transactions/{uuid.uuid4()}/confirm-resolved-buyer")

    assert response.status_code == 401


def test_confirm_resolved_seller_requires_authentication() -> None:
    client = TestClient(app)
    response = client.post(f"/transactions/{uuid.uuid4()}/confirm-resolved-seller")

    assert response.status_code == 401


def test_confirm_resolved_buyer_rejects_seller_user(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.SELLER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/confirm-resolved-buyer",
        headers=_csrf_headers(),
    )

    assert response.status_code == 403


def test_confirm_resolved_seller_rejects_buyer_user(monkeypatch) -> None:
    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/confirm-resolved-seller",
        headers=_csrf_headers(),
    )

    assert response.status_code == 403


def test_confirm_resolved_buyer_endpoint_returns_422_for_invalid_state(monkeypatch) -> None:
    def fake_submit_buyer_resolution_confirmation(_db, transaction_id, buyer):
        raise TransactionBuyerActionInvalidStateError(
            "Transaction must be in resolved state for mutual confirmation closure."
        )

    monkeypatch.setattr(
        transactions_router,
        "submit_buyer_resolution_confirmation",
        fake_submit_buyer_resolution_confirmation,
    )

    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/confirm-resolved-buyer",
        headers=_csrf_headers(),
    )

    assert response.status_code == 422


def test_confirm_resolved_seller_endpoint_returns_422_for_invalid_state(monkeypatch) -> None:
    def fake_submit_seller_resolution_confirmation(_db, transaction_id, seller):
        raise TransactionDispatchInvalidStateError(
            "Transaction must be in resolved state for mutual confirmation closure."
        )

    monkeypatch.setattr(
        transactions_router,
        "submit_seller_resolution_confirmation",
        fake_submit_seller_resolution_confirmation,
    )

    client = _authenticated_client(monkeypatch, UserRole.SELLER)
    response = client.post(
        f"/transactions/{uuid.uuid4()}/confirm-resolved-seller",
        headers=_csrf_headers(),
    )

    assert response.status_code == 422


def test_confirm_resolved_buyer_endpoint_returns_200(monkeypatch) -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.RESOLVED_RELEASE,
    )

    def fake_submit_buyer_resolution_confirmation(_db, transaction_id, buyer):
        return transaction

    monkeypatch.setattr(
        transactions_router,
        "submit_buyer_resolution_confirmation",
        fake_submit_buyer_resolution_confirmation,
    )

    client = _authenticated_client(monkeypatch, UserRole.BUYER)
    response = client.post(
        f"/transactions/{transaction.id}/confirm-resolved-buyer",
        headers=_csrf_headers(),
    )

    assert response.status_code == 200


def test_confirm_resolved_seller_endpoint_returns_200(monkeypatch) -> None:
    seller = _build_user(UserRole.SELLER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.RESOLVED_RELEASE,
    )

    def fake_submit_seller_resolution_confirmation(_db, transaction_id, seller):
        return transaction

    monkeypatch.setattr(
        transactions_router,
        "submit_seller_resolution_confirmation",
        fake_submit_seller_resolution_confirmation,
    )

    client = _authenticated_client(monkeypatch, UserRole.SELLER)
    response = client.post(
        f"/transactions/{transaction.id}/confirm-resolved-seller",
        headers=_csrf_headers(),
    )

    assert response.status_code == 200


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


def test_service_buyer_sent_back_moves_disputed_to_return_in_transit() -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.DISPUTED_FUNCTIONAL,
    )

    db = _FakeDb(transactions=[transaction], listings=[listing])

    returned = mark_buyer_sent_back_service(
        cast(Session, db),
        transaction_id=transaction.id,
        buyer=buyer,
    )

    assert returned.status == TransactionStatus.RETURN_IN_TRANSIT
    assert db.committed is True

    notifications = [item for item in db.added if isinstance(item, Notification)]
    assert len(notifications) == 2
    assert all(item.event_type == NotificationEventType.DISPUTE_UPDATED for item in notifications)
    assert all(item.payload.get("reason") == "buyer_sent_back" for item in notifications)
    history = notifications[0].payload.get("transition_history")
    assert isinstance(history, list)
    assert history[0]["from"] == TransactionStatus.DISPUTED_FUNCTIONAL.value
    assert history[0]["to"] == TransactionStatus.RETURN_IN_TRANSIT.value


def test_service_buyer_sent_back_rejects_non_disputed_state() -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.HOLD_24H,
    )

    db = _FakeDb(transactions=[transaction], listings=[listing])

    with pytest.raises(
        TransactionBuyerActionInvalidStateError,
        match="Transaction must be in disputed_functional state for buyer sent-back action.",
    ):
        mark_buyer_sent_back_service(
            cast(Session, db),
            transaction_id=transaction.id,
            buyer=buyer,
        )


def test_service_seller_received_moves_return_in_transit_to_return_received() -> None:
    seller = _build_user(UserRole.SELLER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.RETURN_IN_TRANSIT,
    )

    db = _FakeDb(transactions=[transaction], listings=[listing])

    received = mark_seller_received_service(
        cast(Session, db),
        transaction_id=transaction.id,
        seller=seller,
    )

    assert received.status == TransactionStatus.RETURN_RECEIVED
    assert db.committed is True

    notifications = [item for item in db.added if isinstance(item, Notification)]
    assert len(notifications) == 2
    assert all(item.event_type == NotificationEventType.DISPUTE_UPDATED for item in notifications)
    assert all(item.payload.get("reason") == "seller_received" for item in notifications)
    history = notifications[0].payload.get("transition_history")
    assert isinstance(history, list)
    assert history[0]["from"] == TransactionStatus.RETURN_IN_TRANSIT.value
    assert history[0]["to"] == TransactionStatus.RETURN_RECEIVED.value


def test_service_seller_received_rejects_non_return_in_transit_state() -> None:
    seller = _build_user(UserRole.SELLER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.DISPUTED_FUNCTIONAL,
    )

    db = _FakeDb(transactions=[transaction], listings=[listing])

    with pytest.raises(
        TransactionDispatchInvalidStateError,
        match="Transaction must be in return_in_transit state for seller received action.",
    ):
        mark_seller_received_service(
            cast(Session, db),
            transaction_id=transaction.id,
            seller=seller,
        )


def test_service_seller_resolution_refund_issues_refund_and_closes() -> None:
    seller = _build_user(UserRole.SELLER)
    listing = _build_listing_with_allowed_actions(
        serialized=True,
        allowed_actions=["refund_issued", "repair_shipped", "replacement_shipped"],
    )
    transaction = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.RETURN_RECEIVED,
    )
    dispute = Dispute(
        id=uuid.uuid4(),
        transaction_id=transaction.id,
        opened_by_user_id=transaction.buyer_id,
        dispute_type=DisputeType.FUNCTIONAL,
        status=DisputeStatus.OPEN,
        reason="not_working",
        description="Does not boot.",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    class _QueryResult:
        def __init__(self, item):
            self._item = item

        def scalars(self):
            return self

        def first(self):
            return self._item

    class _FakeDbWithDispute(_FakeDb):
        def __init__(self, *, dispute_item, **kwargs):
            super().__init__(**kwargs)
            self._dispute_item = dispute_item

        def execute(self, query):
            _ = query
            return _QueryResult(self._dispute_item)

    db = _FakeDbWithDispute(
        transactions=[transaction],
        listings=[listing],
        dispute_item=dispute,
    )

    resolved = apply_seller_resolution_action_service(
        cast(Session, db),
        transaction_id=transaction.id,
        seller=seller,
        action="refund_issued",
        notes="Refund approved and sent.",
    )

    assert resolved.status == TransactionStatus.REFUNDED_BUYER
    assert resolved.refunded_at is not None
    assert db.committed is True
    assert dispute.seller_resolution_action == SellerResolutionAction.REFUND_ISSUED
    assert dispute.status == DisputeStatus.RESOLVED_REFUND
    assert dispute.resolved_at is not None
    assert dispute.seller_resolution_notes == "Refund approved and sent."

    notifications = [item for item in db.added if isinstance(item, Notification)]
    assert len(notifications) == 2
    assert all(item.event_type == NotificationEventType.DISPUTE_UPDATED for item in notifications)
    assert all(item.payload.get("action") == "refund_issued" for item in notifications)


def test_service_seller_resolution_repair_moves_to_awaiting_buyer_reconfirmation() -> None:
    seller = _build_user(UserRole.SELLER)
    listing = _build_listing_with_allowed_actions(
        serialized=True,
        allowed_actions=["refund_issued", "repair_shipped", "replacement_shipped"],
    )
    transaction = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.RETURN_RECEIVED,
    )
    dispute = Dispute(
        id=uuid.uuid4(),
        transaction_id=transaction.id,
        opened_by_user_id=transaction.buyer_id,
        dispute_type=DisputeType.FUNCTIONAL,
        status=DisputeStatus.OPEN,
        reason="missing_parts",
        description="Missing charger.",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    class _QueryResult:
        def __init__(self, item):
            self._item = item

        def scalars(self):
            return self

        def first(self):
            return self._item

    class _FakeDbWithDispute(_FakeDb):
        def __init__(self, *, dispute_item, **kwargs):
            super().__init__(**kwargs)
            self._dispute_item = dispute_item

        def execute(self, query):
            _ = query
            return _QueryResult(self._dispute_item)

    db = _FakeDbWithDispute(
        transactions=[transaction],
        listings=[listing],
        dispute_item=dispute,
    )

    awaiting = apply_seller_resolution_action_service(
        cast(Session, db),
        transaction_id=transaction.id,
        seller=seller,
        action="repair_shipped",
        notes="Sent repaired replacement board.",
    )

    assert awaiting.status == TransactionStatus.AWAITING_BUYER_RECONFIRMATION
    assert db.committed is True
    assert dispute.seller_resolution_action == SellerResolutionAction.REPAIR_SHIPPED
    assert dispute.status == DisputeStatus.AWAITING_BUYER_RECONFIRMATION
    assert dispute.resolved_at is None


def test_service_seller_resolution_rejects_action_not_allowed_by_policy() -> None:
    seller = _build_user(UserRole.SELLER)
    listing = _build_listing_with_allowed_actions(
        serialized=True,
        allowed_actions=["refund_issued"],
    )
    transaction = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.RETURN_RECEIVED,
    )

    class _QueryResult:
        def scalars(self):
            return self

        def first(self):
            return None

    class _FakeDbWithoutDispute(_FakeDb):
        def execute(self, query):
            _ = query
            return _QueryResult()

    db = _FakeDbWithoutDispute(transactions=[transaction], listings=[listing])

    with pytest.raises(
        TransactionValidationError,
        match="Seller resolution action is not allowed by listing dispute policy.",
    ):
        apply_seller_resolution_action_service(
            cast(Session, db),
            transaction_id=transaction.id,
            seller=seller,
            action="repair_shipped",
            notes=None,
        )


def test_service_buyer_reconfirmation_accept_releases_to_seller() -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.AWAITING_BUYER_RECONFIRMATION,
    )
    dispute = Dispute(
        id=uuid.uuid4(),
        transaction_id=transaction.id,
        opened_by_user_id=buyer.id,
        dispute_type=DisputeType.FUNCTIONAL,
        status=DisputeStatus.AWAITING_BUYER_RECONFIRMATION,
        reason="repair_shipped",
        description="Repair shipped.",
        evidence={"buyer_reconfirm_rejection_count": 0},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    class _QueryResult:
        def __init__(self, item):
            self._item = item

        def scalars(self):
            return self

        def first(self):
            return self._item

    class _FakeDbWithDispute(_FakeDb):
        def __init__(self, *, dispute_item, **kwargs):
            super().__init__(**kwargs)
            self._dispute_item = dispute_item

        def execute(self, query):
            _ = query
            return _QueryResult(self._dispute_item)

    db = _FakeDbWithDispute(
        transactions=[transaction],
        listings=[listing],
        dispute_item=dispute,
    )

    resolved = submit_buyer_reconfirmation_service(
        cast(Session, db),
        transaction_id=transaction.id,
        buyer=buyer,
        accepted=True,
        notes="Works now.",
    )

    assert resolved.status == TransactionStatus.RESOLVED_RELEASE
    assert resolved.released_at is not None
    assert db.committed is True
    assert dispute.status == DisputeStatus.RESOLVED_RELEASE
    assert dispute.resolved_at is not None


def test_service_buyer_reconfirmation_first_no_reopens_once() -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.AWAITING_BUYER_RECONFIRMATION,
    )
    dispute = Dispute(
        id=uuid.uuid4(),
        transaction_id=transaction.id,
        opened_by_user_id=buyer.id,
        dispute_type=DisputeType.FUNCTIONAL,
        status=DisputeStatus.AWAITING_BUYER_RECONFIRMATION,
        reason="replacement_shipped",
        description="Replacement shipped.",
        evidence={"buyer_reconfirm_rejection_count": 0},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    class _QueryResult:
        def __init__(self, item):
            self._item = item

        def scalars(self):
            return self

        def first(self):
            return self._item

    class _FakeDbWithDispute(_FakeDb):
        def __init__(self, *, dispute_item, **kwargs):
            super().__init__(**kwargs)
            self._dispute_item = dispute_item

        def execute(self, query):
            _ = query
            return _QueryResult(self._dispute_item)

    db = _FakeDbWithDispute(
        transactions=[transaction],
        listings=[listing],
        dispute_item=dispute,
    )

    reopened = submit_buyer_reconfirmation_service(
        cast(Session, db),
        transaction_id=transaction.id,
        buyer=buyer,
        accepted=False,
        notes="Still failing.",
    )

    assert reopened.status == TransactionStatus.RETURN_RECEIVED
    assert db.committed is True
    assert dispute.status == DisputeStatus.RETURN_RECEIVED
    assert dispute.evidence.get("buyer_reconfirm_rejection_count") == 1


def test_service_buyer_reconfirmation_second_no_escalates() -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.AWAITING_BUYER_RECONFIRMATION,
    )
    dispute = Dispute(
        id=uuid.uuid4(),
        transaction_id=transaction.id,
        opened_by_user_id=buyer.id,
        dispute_type=DisputeType.FUNCTIONAL,
        status=DisputeStatus.AWAITING_BUYER_RECONFIRMATION,
        reason="replacement_shipped",
        description="Replacement shipped.",
        evidence={"buyer_reconfirm_rejection_count": 1},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    class _QueryResult:
        def __init__(self, item):
            self._item = item

        def scalars(self):
            return self

        def first(self):
            return self._item

    class _FakeDbWithDispute(_FakeDb):
        def __init__(self, *, dispute_item, **kwargs):
            super().__init__(**kwargs)
            self._dispute_item = dispute_item

        def execute(self, query):
            _ = query
            return _QueryResult(self._dispute_item)

    db = _FakeDbWithDispute(
        transactions=[transaction],
        listings=[listing],
        dispute_item=dispute,
    )

    escalated = submit_buyer_reconfirmation_service(
        cast(Session, db),
        transaction_id=transaction.id,
        buyer=buyer,
        accepted=False,
        notes="Second rejection.",
    )

    assert escalated.status == TransactionStatus.ESCALATED_ADMIN_REVIEW
    assert db.committed is True
    assert dispute.status == DisputeStatus.ESCALATED_ADMIN_REVIEW
    assert dispute.evidence.get("buyer_reconfirm_rejection_count") == 2
    assert dispute.escalated_at is not None


def test_service_mutual_confirmation_requires_both_parties() -> None:
    buyer = _build_user(UserRole.BUYER)
    seller = _build_user(UserRole.SELLER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=seller.id,
        listing_id=listing.id,
        status=TransactionStatus.RESOLVED_RELEASE,
    )
    dispute = Dispute(
        id=uuid.uuid4(),
        transaction_id=transaction.id,
        opened_by_user_id=buyer.id,
        dispute_type=DisputeType.FUNCTIONAL,
        status=DisputeStatus.RESOLVED_RELEASE,
        reason="resolved_release",
        description="Resolution accepted.",
        evidence={
            "buyer_confirmed_resolved": False,
            "seller_confirmed_resolved": False,
        },
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    class _QueryResult:
        def __init__(self, item):
            self._item = item

        def scalars(self):
            return self

        def first(self):
            return self._item

    class _FakeDbWithDispute(_FakeDb):
        def __init__(self, *, dispute_item, **kwargs):
            super().__init__(**kwargs)
            self._dispute_item = dispute_item

        def execute(self, query):
            _ = query
            return _QueryResult(self._dispute_item)

    db = _FakeDbWithDispute(
        transactions=[transaction],
        listings=[listing],
        dispute_item=dispute,
    )

    buyer_view = submit_buyer_resolution_confirmation_service(
        cast(Session, db),
        transaction_id=transaction.id,
        buyer=buyer,
    )

    assert buyer_view.status == TransactionStatus.RESOLVED_RELEASE
    assert dispute.evidence.get("buyer_confirmed_resolved") is True
    assert dispute.evidence.get("seller_confirmed_resolved") is False
    assert dispute.resolved_at is None

    seller_view = submit_seller_resolution_confirmation_service(
        cast(Session, db),
        transaction_id=transaction.id,
        seller=seller,
    )

    assert seller_view.status == TransactionStatus.RESOLVED_RELEASE
    assert dispute.evidence.get("buyer_confirmed_resolved") is True
    assert dispute.evidence.get("seller_confirmed_resolved") is True
    assert dispute.resolved_at is not None


def test_service_mutual_confirmation_no_unilateral_closure() -> None:
    buyer = _build_user(UserRole.BUYER)
    listing = _build_listing(serialized=True)
    transaction = _build_transaction(
        buyer_id=buyer.id,
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.RESOLVED_REFUND,
    )
    dispute = Dispute(
        id=uuid.uuid4(),
        transaction_id=transaction.id,
        opened_by_user_id=buyer.id,
        dispute_type=DisputeType.FUNCTIONAL,
        status=DisputeStatus.RESOLVED_REFUND,
        reason="resolved_refund",
        description="Refund closed.",
        evidence={
            "buyer_confirmed_resolved": False,
            "seller_confirmed_resolved": False,
        },
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    class _QueryResult:
        def __init__(self, item):
            self._item = item

        def scalars(self):
            return self

        def first(self):
            return self._item

    class _FakeDbWithDispute(_FakeDb):
        def __init__(self, *, dispute_item, **kwargs):
            super().__init__(**kwargs)
            self._dispute_item = dispute_item

        def execute(self, query):
            _ = query
            return _QueryResult(self._dispute_item)

    db = _FakeDbWithDispute(
        transactions=[transaction],
        listings=[listing],
        dispute_item=dispute,
    )

    submit_buyer_resolution_confirmation_service(
        cast(Session, db),
        transaction_id=transaction.id,
        buyer=buyer,
    )

    assert dispute.evidence.get("buyer_confirmed_resolved") is True
    assert dispute.evidence.get("seller_confirmed_resolved") is False
    assert dispute.resolved_at is None


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
    dispute_txn = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.DISPUTED_FUNCTIONAL,
    )
    dispute_txn.updated_at = now - timedelta(days=4)
    seller_timeout_txn = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=listing.seller_id,
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
    assert hold_txn.released_at == now
    assert dispute_txn.status == TransactionStatus.RELEASED
    assert dispute_txn.released_at == now
    assert seller_timeout_txn.status == TransactionStatus.ESCALATED_ADMIN_REVIEW
    assert db.committed is True

    notifications = [item for item in db.added if isinstance(item, Notification)]
    assert len(notifications) == 10
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

    dispute_events = [
        item
        for item in notifications
        if item.payload.get("reason") == "dispute_buyer_sent_back_timeout_3d"
    ]
    assert len(dispute_events) == 2
    dispute_history = dispute_events[0].payload.get("transition_history")
    assert isinstance(dispute_history, list)
    assert dispute_history == [
        {
            "from": TransactionStatus.DISPUTED_FUNCTIONAL.value,
            "to": TransactionStatus.RELEASED.value,
            "at": now.isoformat(),
            "initiated_by": "system",
            "reason": "dispute_buyer_sent_back_timeout_3d",
        }
    ]

    seller_timeout_events = [
        item for item in notifications if item.payload.get("reason") == "seller_received_timeout_3d"
    ]
    assert len(seller_timeout_events) == 2
    seller_timeout_history = seller_timeout_events[0].payload.get("transition_history")
    assert isinstance(seller_timeout_history, list)
    assert seller_timeout_history == [
        {
            "from": TransactionStatus.RETURN_IN_TRANSIT.value,
            "to": TransactionStatus.ESCALATED_ADMIN_REVIEW.value,
            "at": now.isoformat(),
            "initiated_by": "system",
            "reason": "seller_received_timeout_3d",
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
    second = run_timeout_jobs(cast(Session, db), now=now + timedelta(minutes=5))

    assert first.hold_auto_release_count == 1
    assert second.hold_auto_release_count == 0
    assert first.dispute_buyer_sent_back_timeout_count == 0
    assert second.dispute_buyer_sent_back_timeout_count == 0
    assert first.dispute_seller_received_timeout_count == 0
    assert second.dispute_seller_received_timeout_count == 0
    assert hold_txn.status == TransactionStatus.RELEASED
    assert db.commit_count == 1


def test_timeout_jobs_dispute_buyer_sent_back_auto_cancel_is_idempotent(monkeypatch) -> None:
    now = datetime.now(UTC)
    listing = _build_listing(serialized=True)
    dispute_txn = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.DISPUTED_FUNCTIONAL,
    )
    dispute_txn.updated_at = now - timedelta(days=4)

    db = _FakeDb(transactions=[dispute_txn], listings=[listing])

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
    monkeypatch.setattr(
        transaction_service,
        "_get_due_dispute_buyer_sent_back_timeout_transactions",
        lambda _db, _cutoff: (
            [dispute_txn] if dispute_txn.status == TransactionStatus.DISPUTED_FUNCTIONAL else []
        ),
    )
    monkeypatch.setattr(
        transaction_service,
        "_get_due_seller_received_timeout_transactions",
        lambda _db, _cutoff: [],
    )

    first = run_timeout_jobs(cast(Session, db), now=now)
    second = run_timeout_jobs(cast(Session, db), now=now + timedelta(minutes=5))

    assert first.dispute_buyer_sent_back_timeout_count == 1
    assert second.dispute_buyer_sent_back_timeout_count == 0
    assert first.dispute_seller_received_timeout_count == 0
    assert second.dispute_seller_received_timeout_count == 0
    assert dispute_txn.status == TransactionStatus.RELEASED
    assert db.commit_count == 1


def test_timeout_jobs_seller_received_auto_escalate_is_idempotent(monkeypatch) -> None:
    now = datetime.now(UTC)
    listing = _build_listing(serialized=True)
    seller_timeout_txn = _build_transaction(
        buyer_id=uuid.uuid4(),
        seller_id=listing.seller_id,
        listing_id=listing.id,
        status=TransactionStatus.RETURN_IN_TRANSIT,
    )
    seller_timeout_txn.updated_at = now - timedelta(days=4)

    db = _FakeDb(transactions=[seller_timeout_txn], listings=[listing])

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
    monkeypatch.setattr(
        transaction_service,
        "_get_due_dispute_buyer_sent_back_timeout_transactions",
        lambda _db, _cutoff: [],
    )
    monkeypatch.setattr(
        transaction_service,
        "_get_due_seller_received_timeout_transactions",
        lambda _db, _cutoff: (
            [seller_timeout_txn]
            if seller_timeout_txn.status == TransactionStatus.RETURN_IN_TRANSIT
            else []
        ),
    )

    first = run_timeout_jobs(cast(Session, db), now=now)
    second = run_timeout_jobs(cast(Session, db), now=now + timedelta(minutes=5))

    assert first.dispute_seller_received_timeout_count == 1
    assert second.dispute_seller_received_timeout_count == 0
    assert seller_timeout_txn.status == TransactionStatus.ESCALATED_ADMIN_REVIEW
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

    result = run_timeout_jobs(cast(Session, db), now=datetime.now(UTC))

    assert result.at_door_timeout_count == 0
    assert result.no_dispatch_timeout_count == 0
    assert result.hold_auto_release_count == 0
    assert result.dispute_buyer_sent_back_timeout_count == 0
    assert result.dispute_seller_received_timeout_count == 0
    assert db.committed is False
