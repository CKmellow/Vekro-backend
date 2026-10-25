# ruff: noqa: F811 - pytest fixtures are imported from the dispute summary tests and injected by name.
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.models.dispute import AdminDecision, Dispute, DisputeStatus, DisputeType
from app.models.notification import Notification, NotificationEventType
from app.models.transaction import TransactionStatus
from app.models.user import UserRole
from app.services.admin_cases import CaseTrigger, case_counters, case_trigger
from sqlalchemy.orm import Session

from tests.test_dispute_summary import _clear_overrides, db  # noqa: F401  (fixtures)
from tests.test_transaction_list import _client_as, _listing, _order, _user
from tests.test_transaction_list import db as _db_fixture  # noqa: F401

NOW = datetime.now(UTC)


def _dispute(order, buyer, **fields) -> Dispute:
    base = dict(
        id=uuid.uuid4(),
        transaction_id=order.id,
        opened_by_user_id=buyer.id,
        dispute_type=DisputeType.FUNCTIONAL,
        status=DisputeStatus.ESCALATED_ADMIN_REVIEW,
        reason="not_working",
        description="The screen flickers.",
        evidence={},
        opened_at=NOW - timedelta(days=3),
        escalated_at=NOW - timedelta(hours=2),
        created_at=NOW - timedelta(days=3),
        updated_at=NOW,
    )
    base.update(fields)
    return Dispute(**base)


@pytest.fixture
def console(db: Session) -> dict[str, Any]:
    admin = _user(UserRole.ADMIN, "Vekro Support", "+254700000001")
    seller = _user(UserRole.SELLER, "Amina Gadgets", "+254722118904")
    buyer = _user(UserRole.BUYER, "Brian Otieno", "+254712345678")
    db.add_all([admin, seller, buyer])
    db.flush()
    listings = [_listing(seller, f"Item {i}", "52000.00") for i in range(4)]
    listings[0].unique_id = "352098114371234"
    db.add_all(listings)
    db.flush()
    orders = [
        _order(
            listings[i], buyer, TransactionStatus.ESCALATED_ADMIN_REVIEW, f"VK-CASE0{i}", minutes=i
        )
        for i in range(3)
    ] + [_order(listings[3], buyer, TransactionStatus.RESOLVED_REFUND, "VK-DONE00", minutes=9)]
    db.add_all(orders)
    db.flush()
    disputes = [
        _dispute(
            orders[0],
            buyer,
            escalated_at=NOW - timedelta(hours=30),
            evidence={"escalation": {"by": "seller", "reason": "The crack wasn't there."}},
        ),
        _dispute(orders[1], buyer, reason="other", escalated_at=NOW - timedelta(hours=5)),
        _dispute(orders[2], buyer, evidence={"buyer_reconfirm_rejection_count": 2}),
        _dispute(
            orders[3],
            buyer,
            status=DisputeStatus.RESOLVED_REFUND,
            admin_decision=AdminDecision.REFUND,
            admin_reason="Seller never confirmed receipt.",
            decided_by_user_id=admin.id,
            escalated_at=NOW - timedelta(hours=6),
            resolved_at=NOW - timedelta(hours=1),
        ),
    ]
    db.add_all(disputes)
    db.add(
        Notification(
            id=uuid.uuid4(),
            user_id=seller.id,
            transaction_id=orders[0].id,
            event_type=NotificationEventType.TRANSACTION_LOCKED,
            title="Buyer payment confirmed",
            message="Paid.",
            payload={"delivery_code": "905531"},
            created_at=NOW - timedelta(days=4),
        )
    )
    db.commit()
    return {
        "admin": admin,
        "seller": seller,
        "buyer": buyer,
        "orders": orders,
        "disputes": disputes,
    }


def test_queue_lists_open_cases_oldest_first_with_triggers_and_counters(
    monkeypatch, db, console
) -> None:
    client = _client_as(monkeypatch, db, console["admin"])

    body = client.get("/admin/cases").json()

    assert [i["reference"] for i in body["items"]] == ["VK-CASE00", "VK-CASE01", "VK-CASE02"]
    first = body["items"][0]
    assert first["trigger"] == "party_escalated"
    assert first["escalated_by"] == "seller"
    assert (first["item_title"], first["buyer_name"], first["seller_name"]) == (
        "Item 0",
        "Brian Otieno",
        "Amina Gadgets",
    )
    assert [i["trigger"] for i in body["items"][1:]] == ["other_category", "buyer_not_fixed_twice"]
    assert body["counters"]["open"] == 3
    assert body["counters"]["older_than_24h"] == 1
    assert body["counters"]["average_decision_seconds"] == 5 * 3600


def test_decided_filter(monkeypatch, db, console) -> None:
    client = _client_as(monkeypatch, db, console["admin"])

    body = client.get("/admin/cases", params={"status": "decided"}).json()

    assert [i["reference"] for i in body["items"]] == ["VK-DONE00"]
    assert body["items"][0]["admin_decision"] == "refund"


def test_case_detail_has_parties_policy_accounts_and_no_payloads(monkeypatch, db, console) -> None:
    client = _client_as(monkeypatch, db, console["admin"])

    body = client.get(f"/admin/cases/{console['disputes'][0].id}").json()

    assert body["buyer"]["phone"] == "+254712345678"
    assert body["buyer"]["orders"] == 4
    assert body["seller"]["phone"] == "+254722118904"
    assert body["seller"]["dispute_rate"] == 1.0
    assert body["unique_id"] == "352098114371234"
    assert body["escalation_reason"] == "The crack wasn't there."
    assert body["decision"] is None
    assert [e["title"] for e in body["timeline"]][:2] == [
        "Buyer payment confirmed",
        "Problem reported",
    ]
    assert "905531" not in str(body)
    assert "payload" not in str(body["timeline"])


def test_decided_case_names_the_admin(monkeypatch, db, console) -> None:
    client = _client_as(monkeypatch, db, console["admin"])

    body = client.get(f"/admin/cases/{console['disputes'][3].id}").json()

    assert body["decision"]["decision"] == "refund"
    assert body["decision"]["decided_by"] == "Vekro Support"
    assert body["decision"]["reason"] == "Seller never confirmed receipt."


def test_console_reads_are_admin_only(monkeypatch, db, console) -> None:
    client = _client_as(monkeypatch, db, console["seller"])

    assert client.get("/admin/cases").status_code == 403
    assert client.get(f"/admin/cases/{console['disputes'][0].id}").status_code == 403


def test_missing_case_is_not_found(monkeypatch, db, console) -> None:
    client = _client_as(monkeypatch, db, console["admin"])

    assert client.get(f"/admin/cases/{uuid.uuid4()}").status_code == 404


def test_trigger_defaults_to_receipt_timeout_and_counts_today_in_eat(db, console) -> None:
    assert case_trigger(_dispute(console["orders"][0], console["buyer"])) == (
        CaseTrigger.RECEIPT_TIMEOUT,
        None,
    )
    assert case_counters(db).decided_today in {0, 1}
