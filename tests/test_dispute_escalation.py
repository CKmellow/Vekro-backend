# ruff: noqa: F811 - pytest fixtures are imported from the dispute summary tests and injected by name.
from typing import Any

import pytest
from app.core.settings import get_settings
from app.models.dispute import Dispute, DisputeStatus
from app.models.notification import Notification
from app.models.transaction import TransactionStatus
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.test_dispute_summary import _clear_overrides, case, db  # noqa: F401  (fixtures)
from tests.test_transaction_list import _client_as as _read_client
from tests.test_transaction_list import db as _db_fixture  # noqa: F401

REASON = "The buyer dropped the phone; the cracked corner wasn't there when I sent it."


def _client_as(monkeypatch, session: Session, user) -> TestClient:
    client = _read_client(monkeypatch, session, user)
    settings = get_settings()
    client.cookies.set(settings.csrf_cookie_name, "csrf-token")
    client.headers[settings.csrf_header_name] = "csrf-token"
    return client


@pytest.mark.parametrize("who", ["seller", "buyer"])
def test_a_party_sends_an_open_case_to_support(monkeypatch, db, case: dict[str, Any], who) -> None:
    client = _client_as(monkeypatch, db, case[who])

    response = client.post(f"/transactions/{case['order'].id}/escalate", json={"reason": REASON})

    assert response.status_code == 200
    assert response.json()["status"] == "escalated_admin_review"
    db.expire_all()
    dispute = db.query(Dispute).filter(Dispute.status == DisputeStatus.ESCALATED_ADMIN_REVIEW).one()
    assert dispute.escalated_at is not None
    assert dispute.evidence["escalation"]["by"] == who
    assert dispute.evidence["escalation"]["reason"] == REASON
    notes = db.query(Notification).filter(Notification.title == "Sent to Vekro support").all()
    assert sorted(n.user_id for n in notes) == sorted([case["buyer"].id, case["seller"].id])
    assert any(n.message.startswith("You asked") for n in notes)


def test_strangers_are_refused(monkeypatch, db, case) -> None:
    client = _client_as(monkeypatch, db, case["stranger"])

    response = client.post(f"/transactions/{case['order'].id}/escalate", json={"reason": REASON})

    assert response.status_code == 403


def test_only_open_cases_can_be_escalated(monkeypatch, db, case) -> None:
    client = _client_as(monkeypatch, db, case["seller"])

    response = client.post(f"/transactions/{case['other'].id}/escalate", json={"reason": REASON})

    assert response.status_code == 409
    db.expire_all()
    assert case["other"].status == TransactionStatus.LOCKED


def test_a_reason_is_required(monkeypatch, db, case) -> None:
    client = _client_as(monkeypatch, db, case["seller"])

    assert (
        client.post(
            f"/transactions/{case['order'].id}/escalate", json={"reason": "  too short  "}
        ).status_code
        == 422
    )
    assert client.post(f"/transactions/{case['order'].id}/escalate", json={}).status_code == 422


def test_escalation_needs_the_csrf_header(monkeypatch, db, case) -> None:
    client = _read_client(monkeypatch, db, case["seller"])

    assert (
        client.post(
            f"/transactions/{case['order'].id}/escalate", json={"reason": REASON}
        ).status_code
        == 403
    )
