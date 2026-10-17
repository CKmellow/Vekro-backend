"""E4: eConfirm confirmation-code encryption (Fernet, rotatable, fail-loud)."""

from __future__ import annotations

import pytest
from app.core.settings import Settings
from app.services.custody.econfirm_crypto import (
    EconfirmCodeCipher,
    InvalidToken,
    build_cipher,
    is_valid_key_config,
    parse_keys,
)
from cryptography.fernet import Fernet
from pydantic import ValidationError


def test_round_trip():
    key = Fernet.generate_key().decode()
    cipher = build_cipher(key)
    token = cipher.encrypt("AB12CD34")
    assert token != "AB12CD34"  # ciphertext, not plaintext
    assert cipher.decrypt(token) == "AB12CD34"


def test_ciphertext_is_not_the_plaintext_substring():
    cipher = build_cipher(Fernet.generate_key().decode())
    token = cipher.encrypt("SECRET99")
    assert "SECRET99" not in token


def test_wrong_key_fails_loudly_not_garbage():
    token = build_cipher(Fernet.generate_key().decode()).encrypt("AB12CD34")
    other = build_cipher(Fernet.generate_key().decode())
    with pytest.raises(InvalidToken):
        other.decrypt(token)


def test_rotation_new_key_first_old_still_decrypts():
    old = Fernet.generate_key().decode()
    new = Fernet.generate_key().decode()

    old_token = build_cipher(old).encrypt("OLDCODE1")
    rotated = build_cipher(f"{new},{old}")  # new primary, old still accepted

    assert rotated.decrypt(old_token) == "OLDCODE1"  # old ciphertext still readable
    new_token = rotated.encrypt("NEWCODE2")
    # The new ciphertext must NOT be decryptable by the old key alone.
    with pytest.raises(InvalidToken):
        build_cipher(old).decrypt(new_token)


def test_parse_and_validate_keys():
    good = Fernet.generate_key().decode()
    assert parse_keys(f" {good} , {good} ") == [good, good]
    assert is_valid_key_config(good) is True
    assert is_valid_key_config("") is False
    assert is_valid_key_config("not-a-fernet-key") is False


def test_empty_key_cipher_raises():
    with pytest.raises(ValueError):
        EconfirmCodeCipher([])


def _settings(**overrides):
    base = dict(
        secret_key="x" * 32,
        database_url="postgresql://u:p@h/db",
        alembic_database_url="postgresql://u:p@h/db",
    )
    base.update(overrides)
    return Settings(**base)


def test_startup_refuses_when_enabled_without_key():
    with pytest.raises(ValidationError):
        _settings(econfirm_enabled=True, econfirm_base_url="https://x", econfirm_api_key="ek_x")


def test_startup_refuses_when_enabled_with_invalid_key():
    with pytest.raises(ValidationError):
        _settings(
            econfirm_enabled=True,
            econfirm_base_url="https://x",
            econfirm_api_key="ek_x",
            econfirm_code_encryption_key="not-valid",
        )


def test_startup_ok_when_enabled_with_valid_key():
    settings = _settings(
        econfirm_enabled=True,
        econfirm_base_url="https://x",
        econfirm_api_key="ek_x",
        econfirm_code_encryption_key=Fernet.generate_key().decode(),
    )
    assert settings.econfirm_enabled is True


def test_disabled_does_not_require_key():
    settings = _settings(econfirm_enabled=False)
    assert settings.econfirm_enabled is False
