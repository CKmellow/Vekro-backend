import base64
import hashlib
import hmac
import os
import secrets

PBKDF2_ALGORITHM = "sha256"
PBKDF2_ITERATIONS = 600000
HASH_SCHEME = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITHM,
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    digest_b64 = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"{HASH_SCHEME}${PBKDF2_ITERATIONS}${salt_b64}${digest_b64}"


def verify_password(password: str, password_hash: str) -> bool:
    parts = password_hash.split("$", 3)
    if len(parts) != 4:
        return False

    scheme, iterations_raw, salt_b64, expected_hash_b64 = parts
    if scheme != HASH_SCHEME:
        return False

    try:
        iterations = int(iterations_raw)
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected_hash = base64.urlsafe_b64decode(expected_hash_b64.encode("ascii"))
    except (ValueError, TypeError):
        return False

    computed_hash = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITHM,
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(computed_hash, expected_hash)


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
