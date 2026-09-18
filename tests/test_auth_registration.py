import uuid
from datetime import UTC, datetime

from app.main import app
from app.models.user import User, UserRole
from app.routers import auth as auth_router
from app.services.auth import PhoneAlreadyRegisteredError
from fastapi.testclient import TestClient


def _build_user(phone: str, role: UserRole) -> User:
    return User(
        id=uuid.uuid4(),
        name="Test User",
        phone=phone,
        role=role,
        password_hash="pbkdf2_sha256$1$abc$xyz",
        mpesa_phone=None,
        mpesa_account_name=None,
        session_version=1,
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def test_register_success(monkeypatch) -> None:
    def fake_register_user(db, payload):
        return _build_user(payload.phone, payload.role)

    monkeypatch.setattr(auth_router, "register_user", fake_register_user)
    client = TestClient(app)

    response = client.post(
        "/auth/register",
        json={
            "name": "Jane Buyer",
            "phone": "+254700123456",
            "role": "buyer",
            "password": "strong-pass-123",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Test User"
    assert body["phone"] == "+254700123456"
    assert body["role"] == "buyer"
    assert "password_hash" not in body


def test_register_duplicate_phone_returns_409(monkeypatch) -> None:
    def fake_register_user(db, payload):
        raise PhoneAlreadyRegisteredError

    monkeypatch.setattr(auth_router, "register_user", fake_register_user)
    client = TestClient(app)

    response = client.post(
        "/auth/register",
        json={
            "name": "Jane Buyer",
            "phone": "+254700123456",
            "role": "buyer",
            "password": "strong-pass-123",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Phone number is already registered."


def test_register_admin_role_is_rejected() -> None:
    client = TestClient(app)

    response = client.post(
        "/auth/register",
        json={
            "name": "Admin User",
            "phone": "+254799888777",
            "role": "admin",
            "password": "strong-pass-123",
        },
    )

    assert response.status_code == 422


def test_register_invalid_phone_is_rejected() -> None:
    client = TestClient(app)

    response = client.post(
        "/auth/register",
        json={
            "name": "Bad Phone",
            "phone": "abc",
            "role": "buyer",
            "password": "strong-pass-123",
        },
    )

    assert response.status_code == 422
