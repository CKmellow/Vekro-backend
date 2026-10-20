# ruff: noqa: F811 - pytest fixtures are imported from the timeline tests and injected by name.
from app.models.notification import Notification, NotificationEventType
from sqlalchemy import select

from tests.test_transaction_timeline import (  # noqa: F401 - fixtures
    _clear_overrides,
    _client_as,
    _walk_to_the_door,
    db,
    world,
)


def test_reject_with_a_reason_tells_the_seller(monkeypatch, db, world) -> None:
    order_id, _ = _walk_to_the_door(monkeypatch, db, world)
    buyer = _client_as(monkeypatch, db, world["buyer"])

    response = buyer.post(
        f"/transactions/{order_id}/otp-withhold",
        json={"reason": "wrong_item", "note": "  It's a size 40.  "},
    )

    assert response.status_code == 200
    detail = buyer.get(f"/transactions/{order_id}").json()
    assert detail["status"] == "refunded_buyer"
    assert detail["door_rejection"] == {"reason": "wrong_item", "note": "It's a size 40."}
    seller_note = next(
        n
        for n in db.execute(select(Notification)).scalars()
        if n.user_id == world["seller"].id and n.event_type == NotificationEventType.OTP_WITHHELD
    )
    assert "Reason: Wrong item." in seller_note.message
    assert "It's a size 40." in seller_note.message


def test_reject_without_a_body_still_works(monkeypatch, db, world) -> None:
    order_id, _ = _walk_to_the_door(monkeypatch, db, world)
    buyer = _client_as(monkeypatch, db, world["buyer"])

    assert buyer.post(f"/transactions/{order_id}/otp-withhold").status_code == 200
    assert buyer.get(f"/transactions/{order_id}").json()["door_rejection"] is None


def test_reason_must_be_one_the_seller_declared(monkeypatch, db, world) -> None:
    world["listing"].dispute_policy = {"door_reject_reasons": ["damaged"]}
    db.commit()
    order_id, _ = _walk_to_the_door(monkeypatch, db, world)
    buyer = _client_as(monkeypatch, db, world["buyer"])

    refused = buyer.post(f"/transactions/{order_id}/otp-withhold", json={"reason": "wrong_item"})
    assert refused.status_code == 422
    assert buyer.get(f"/transactions/{order_id}").json()["status"] == "at_door_pending_inspection"

    allowed = buyer.post(f"/transactions/{order_id}/otp-withhold", json={"reason": "other"})
    assert allowed.status_code == 200
