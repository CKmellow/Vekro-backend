import base64
import hashlib
import os

from app.core.security import hash_password, password_hash_needs_upgrade, verify_password

LEGACY_PBKDF2_ALGORITHM = "sha256"
LEGACY_PBKDF2_ITERATIONS = 600000
LEGACY_HASH_SCHEME = "pbkdf2_sha256"


def _legacy_hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        LEGACY_PBKDF2_ALGORITHM,
        password.encode("utf-8"),
        salt,
        LEGACY_PBKDF2_ITERATIONS,
    )
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    digest_b64 = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"{LEGACY_HASH_SCHEME}${LEGACY_PBKDF2_ITERATIONS}${salt_b64}${digest_b64}"


def test_hash_password_never_stores_plaintext() -> None:
    password = "StrongPass123x"

    password_hash = hash_password(password)

    assert password_hash != password
    assert password_hash.startswith("$argon2")
    assert verify_password(password, password_hash)


def test_verify_password_rejects_invalid_password() -> None:
    password_hash = hash_password("StrongPass123x")

    assert not verify_password("WrongPass123x", password_hash)


def test_verify_password_supports_legacy_hashes_and_flags_upgrade() -> None:
    password = "StrongPass123x"
    legacy_hash = _legacy_hash_password(password)

    assert verify_password(password, legacy_hash)
    assert not verify_password("WrongPass123x", legacy_hash)
    assert password_hash_needs_upgrade(legacy_hash)


def test_argon2_hash_does_not_require_upgrade() -> None:
    password_hash = hash_password("StrongPass123x")

    assert not password_hash_needs_upgrade(password_hash)
