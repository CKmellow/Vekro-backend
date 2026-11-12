"""M28 store lifecycle on PostgreSQL, where foreign keys are enforced (SQLite doesn't).

Regression: registering a seller wrote its store_opened row before the user row (#340).
Skipped unless TEST_POSTGRES_URL is set (CI's Migrations job sets it).
"""

import os
import uuid

import pytest
from app.db.session import get_db
from app.main import app
from app.models.anchor_event import AnchorEvent
from app.models.store_event import StoreEvent
from app.models.user import StoreStatus, User
from app.services.store import close_store, reopen_store
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL unset"),
]


@pytest.fixture
def factory():
    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    yield sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    engine.dispose()


def test_registering_a_seller_opens_an_anchored_store(factory) -> None:
    def override():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override
    try:
        phone = f"07{uuid.uuid4().int % 10**8:08d}"
        response = TestClient(app).post(
            "/auth/register",
            json={"name": "Wanjiru", "phone": phone, "password": "Strong-pass-1", "role": "seller"},
        )
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 201, response.text
    assert response.json()["role"] == "seller"

    with factory() as db:
        user = db.get(User, uuid.UUID(response.json()["id"]))
        assert user is not None and user.store_status == StoreStatus.ACTIVE
        [event] = db.execute(select(StoreEvent).where(StoreEvent.user_id == user.id)).scalars()
        anchored = db.execute(
            select(AnchorEvent).where(AnchorEvent.source_id == str(event.id))
        ).scalar_one()
        assert anchored.event_type == "store_opened"

        close_store(db, user)
        reopen_store(db, user)
        kinds = [
            e.kind
            for e in db.execute(
                select(StoreEvent).where(StoreEvent.user_id == user.id).order_by(StoreEvent.at)
            ).scalars()
        ]
        assert kinds == ["opened", "closed", "opened"]
