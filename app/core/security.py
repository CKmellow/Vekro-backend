import base64
import hashlib
import hmac
import secrets

from passlib.context import CryptContext  # type: ignore[import-untyped]
from passlib.exc import UnknownHashError  # type: ignore[import-untyped]

PASSWORD_HASH_CONTEXT = CryptContext(schemes=["argon2"], deprecated="auto")

LEGACY_PBKDF2_ALGORITHM = "sha256"
LEGACY_PBKDF2_ITERATIONS = 600000
LEGACY_HASH_SCHEME = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    return PASSWORD_HASH_CONTEXT.hash(password)


def _verify_legacy_pbkdf2_password(password: str, password_hash: str) -> bool:
    parts = password_hash.split("$", 3)
    if len(parts) != 4:
        return False

    scheme, iterations_raw, salt_b64, expected_hash_b64 = parts
    if scheme != LEGACY_HASH_SCHEME:
        return False

    try:
        iterations = int(iterations_raw)
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected_hash = base64.urlsafe_b64decode(expected_hash_b64.encode("ascii"))
    except (ValueError, TypeError):
        return False

    computed_hash = hashlib.pbkdf2_hmac(
        LEGACY_PBKDF2_ALGORITHM,
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(computed_hash, expected_hash)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return PASSWORD_HASH_CONTEXT.verify(password, password_hash)
    except (UnknownHashError, ValueError, TypeError):
        return _verify_legacy_pbkdf2_password(password, password_hash)


def _is_legacy_pbkdf2_hash(password_hash: str) -> bool:
    return password_hash.startswith(f"{LEGACY_HASH_SCHEME}$")


def password_hash_needs_upgrade(password_hash: str) -> bool:
    if _is_legacy_pbkdf2_hash(password_hash):
        return True

    try:
        return PASSWORD_HASH_CONTEXT.needs_update(password_hash)
    except (UnknownHashError, ValueError, TypeError):
        return True


def generate_session_token() -> str:
    return secrets.token_urlsafe(48)


def hash_session_token(token: str, secret_key: str) -> str:
    return hmac.new(
        secret_key.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_hashed_token(token: str, expected_hash: str, secret_key: str) -> bool:
    computed_hash = hash_session_token(token, secret_key)
    return hmac.compare_digest(computed_hash, expected_hash)
