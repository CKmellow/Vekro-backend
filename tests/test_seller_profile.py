from typing import Any, cast

import pytest
from app.core.auth_context import require_seller_user
from app.core.settings import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.phone_code import PhoneCode
from app.models.user import User, UserRole
from app.routers import seller_profile as router
from app.services import phone_auth, seller_profile
from app.services.phone_auth import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    request_phone_code,
)
from app.services.seller_profile import PayoutCodeRequiredError, change_payout
from fastapi.testclient import TestClient
from sqlalchemy import Table, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

OLD = "+254722118904"
NEW = "+254733000210"


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, list[str]]] = []

    def send(self, message: str, recipients: Any) -> None:
        self.sent.append((message, list(recipients)))


@pytest.fixture
def factory() -> sessionmaker:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine, tables=[cast(Table, User.__table__), cast(Table, PhoneCode.__table__)]
    )
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@pytest.fixture
def seller(factory: sessionmaker) -> User:
    with factory() as db:
        user = User(
            name="Amina Wanjiru",
            phone=OLD,
            role=UserRole.SELLER,
            password_hash="x",
            mpesa_account_name="AMINA WANJIRU",
        )
        db.add(user)
        db.commit()
        return user


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    phone_auth._CLIENT_BUCKETS.clear()
    monkeypatch.setattr(phone_auth, "default_sender_provider", lambda: None)
    monkeypatch.setattr(seller_profile, "default_sender_provider", lambda: None)


def _orders(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    monkeypatch.setattr(seller_profile, "has_orders", lambda _db, _id: value)
    monkeypatch.setattr(router, "has_orders", lambda _db, _id: value)


def test_onboarding_sets_a_new_number_without_a_code(factory, seller, monkeypatch) -> None:
    _orders(monkeypatch, False)
    sender = FakeSender()
    with factory() as db:
        user = db.get(User, seller.id)
        change_payout(
            db,
            user,
            mpesa_phone=NEW,
            mpesa_account_name="AMINA W",
            code=None,
            sender_provider=lambda: sender,  # type: ignore[arg-type,return-value]
        )
        assert user.mpesa_phone == NEW and user.mpesa_account_name == "AMINA W"
    # The old number hears about it.
    assert sender.sent[0][1] == [OLD]
    assert "+254733 *** 210" in sender.sent[0][0]


def test_a_seller_with_orders_needs_the_code_sent_to_the_new_number(
    factory, seller, monkeypatch
) -> None:
    _orders(monkeypatch, True)
    with factory() as db:
        user = db.get(User, seller.id)
        with pytest.raises(PayoutCodeRequiredError):
            change_payout(db, user, mpesa_phone=NEW, mpesa_account_name="AMINA", code=None)
        with pytest.raises(PhoneCodeExpiredError):
            change_payout(db, user, mpesa_phone=NEW, mpesa_account_name="AMINA", code="123456")

        issued = request_phone_code(db, NEW, purpose="payout")
        wrong = "000000" if issued.code != "000000" else "111111"
        with pytest.raises(PhoneCodeInvalidError):
            change_payout(db, user, mpesa_phone=NEW, mpesa_account_name="AMINA", code=wrong)
        change_payout(db, user, mpesa_phone=NEW, mpesa_account_name="AMINA", code=issued.code)
        assert user.mpesa_phone == NEW


def test_a_sign_in_code_cannot_confirm_a_payout_number(factory, seller, monkeypatch) -> None:
    _orders(monkeypatch, True)
    with factory() as db:
        sign_in = request_phone_code(db, NEW, purpose="sign_in")
        with pytest.raises(PhoneCodeExpiredError):
            change_payout(
                db,
                db.get(User, seller.id),
                mpesa_phone=NEW,
                mpesa_account_name="A",
                code=sign_in.code,
            )


def test_keeping_the_number_only_renames(factory, seller, monkeypatch) -> None:
    _orders(monkeypatch, True)
    sender = FakeSender()
    with factory() as db:
        user = db.get(User, seller.id)
        change_payout(
            db,
            user,
            mpesa_phone=OLD,
            mpesa_account_name="NEW NAME",
            code=None,
            sender_provider=lambda: sender,  # type: ignore[arg-type,return-value]
        )
        assert user.mpesa_account_name == "NEW NAME"
    assert sender.sent == []


@pytest.fixture
def client(factory: sessionmaker, seller: User, monkeypatch: pytest.MonkeyPatch):
    def db_override():
        with factory() as session:
            yield session

    monkeypatch.setattr(router, "run_in_background", lambda job: job())
    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[require_seller_user] = lambda: seller
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(require_seller_user, None)


def test_profile_endpoints(client, monkeypatch) -> None:
    _orders(monkeypatch, False)
    profile = client.get("/me/profile").json()
    assert profile["shop_name"] is None and profile["mpesa_phone"] == OLD
    assert profile["has_orders"] is False and profile["logo_url"] is None

    patched = client.patch(
        "/me/profile", json={"shop_name": "Amina Gadgets", "shop_area": "Kilimani, Nairobi"}
    ).json()
    assert patched["shop_name"] == "Amina Gadgets" and patched["shop_area"] == "Kilimani, Nairobi"
    cleared = client.patch("/me/profile", json={"shop_area": ""}).json()
    assert cleared["shop_area"] is None and cleared["shop_name"] == "Amina Gadgets"
    assert client.patch("/me/profile", json={"shop_name": "A"}).status_code == 422


def test_logo_upload_serve_and_remove(client, seller, monkeypatch) -> None:
    _orders(monkeypatch, False)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    uploaded = client.put("/me/profile/logo", content=png, headers={"Content-Type": "image/png"})
    assert uploaded.status_code == 200
    url = uploaded.json()["logo_url"]
    assert url.startswith(f"/sellers/{seller.id}/logo?v=")
    served = client.get(url)
    assert served.status_code == 200 and served.content == png
    assert served.headers["content-type"] == "image/png"

    bad = client.put("/me/profile/logo", content=b"gif", headers={"Content-Type": "image/gif"})
    assert bad.status_code == 415
    big = client.put(
        "/me/profile/logo", content=b"0" * (1024 * 1024 + 1), headers={"Content-Type": "image/png"}
    )
    assert big.status_code == 413

    assert client.delete("/me/profile/logo").json()["logo_url"] is None
    assert client.get(f"/sellers/{seller.id}/logo").status_code == 404


def test_payout_endpoints(client, monkeypatch) -> None:
    _orders(monkeypatch, True)
    monkeypatch.setattr(get_settings(), "phone_code_dev_echo", True)
    body = {"mpesa_phone": "0733000210", "mpesa_account_name": "AMINA WANJIRU"}
    assert client.put("/me/mpesa-number", json=body).status_code == 428

    sent = client.post("/me/mpesa-number/request-code", json={"mpesa_phone": "0733000210"})
    assert sent.status_code == 202
    code = sent.json()["dev_code"]
    wrong = "000000" if code != "000000" else "111111"
    assert client.put("/me/mpesa-number", json={**body, "code": wrong}).status_code == 422
    done = client.put("/me/mpesa-number", json={**body, "code": code})
    assert done.status_code == 200 and done.json()["mpesa_phone"] == NEW
    assert client.put("/me/mpesa-number", json={**body, "code": code}).json()["mpesa_phone"] == NEW
