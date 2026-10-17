"""Reversible at-rest encryption for the eConfirm confirmation_code (M20 / E4).

The confirmation_code is returned by eConfirm only once (at create) and is
required to call release/reversal, so it must be stored reversibly. It is
encrypted with Fernet using a dedicated, rotatable key held in
``ECONFIRM_CODE_ENCRYPTION_KEY`` (NOT the app SECRET_KEY).

Rotation: the setting may hold several comma-separated Fernet keys. The FIRST is
the primary (used to encrypt); all are tried for decryption (MultiFernet), so a
new key can be rolled in front while old ciphertexts still decrypt.

Security rules (A3 / Hard Rule 7): the plaintext is produced only transiently
inside the release/reversal call, and is never logged, returned, cached, or
written to the DB in clear. A wrong/garbage key raises loudly (InvalidToken)
rather than returning corrupted data.

Operational warning: losing every key in ECONFIRM_CODE_ENCRYPTION_KEY makes all
stored eConfirm escrows impossible to release or reverse through the API. Back
the key up separately from the database (see docs/runbooks.md).
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

__all__ = [
    "InvalidToken",
    "EconfirmCodeCipher",
    "build_cipher",
    "parse_keys",
]


def parse_keys(raw: str) -> list[str]:
    """Split the comma-separated key setting into individual Fernet keys."""
    return [key.strip() for key in (raw or "").split(",") if key.strip()]


def is_valid_key_config(raw: str) -> bool:
    """True if `raw` holds at least one syntactically valid Fernet key."""
    keys = parse_keys(raw)
    if not keys:
        return False
    try:
        for key in keys:
            Fernet(key.encode("utf-8"))
    except (ValueError, TypeError):
        return False
    return True


class EconfirmCodeCipher:
    """Encrypt/decrypt confirmation codes with a (rotatable) MultiFernet."""

    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ValueError("ECONFIRM_CODE_ENCRYPTION_KEY must provide at least one Fernet key.")
        # First key is primary (encrypts); all keys can decrypt.
        self._fernet = MultiFernet([Fernet(key.encode("utf-8")) for key in keys])

    def encrypt(self, plaintext: str) -> str:
        if plaintext is None:
            raise ValueError("Cannot encrypt a null confirmation code.")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str) -> str:
        """Return the plaintext, or raise InvalidToken if no key can decrypt it."""
        return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")


def build_cipher(raw: str) -> EconfirmCodeCipher:
    """Construct the cipher from the raw comma-separated key setting."""
    return EconfirmCodeCipher(parse_keys(raw))
