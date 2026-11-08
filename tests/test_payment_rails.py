"""M22 P1: the collection rail for each payment method is stated by configuration alone."""

import pytest
from app.cli import main
from app.services.payment_rails import describe_collection_rails
from cryptography.fernet import Fernet

from tests.test_anchoring_batcher import _settings

DARAJA = {
    "daraja_enabled": True,
    "daraja_consumer_key": "k",
    "daraja_consumer_secret": "s",
    "daraja_passkey": "p",
    "daraja_callback_url": "https://demo.example/payments/mpesa/callback",
}
ECONFIRM = {
    "econfirm_enabled": True,
    "econfirm_api_key": "ek_test",
    "econfirm_code_encryption_key": Fernet.generate_key().decode(),
}
OFF = {"econfirm_enabled": False, "daraja_enabled": False}


def _mpesa(**overrides: object) -> list[tuple[str, str]]:
    routes = describe_collection_rails(_settings(**(OFF | overrides)))
    return [(r.rail, r.when) for r in routes if r.method == "mpesa"]


def test_simulated_is_the_default() -> None:
    assert _mpesa() == [("simulated", "always")]


def test_daraja_enabled_is_the_demo_profile() -> None:
    assert _mpesa(**DARAJA) == [("daraja", "always")]


def test_econfirm_stays_first_for_amounts_in_its_band() -> None:
    assert _mpesa(**DARAJA, **ECONFIRM) == [
        ("econfirm", "KES 100-1000, breaker closed"),
        ("daraja", "otherwise"),
    ]
    assert _mpesa(**ECONFIRM)[1] == ("simulated", "otherwise")


def test_card_and_ussd_are_not_offered_yet() -> None:
    routes = {r.method: r.rail for r in describe_collection_rails(_settings(**OFF))}
    assert routes["card"] == "none"
    assert routes["ussd"] == "none"


def test_daraja_needs_an_https_callback() -> None:
    with pytest.raises(ValueError) as error:
        _settings(
            **DARAJA | {"daraja_callback_url": "http://localhost:8000/payments/mpesa/callback"}
        )
    assert "DARAJA_CALLBACK_URL must use https://" in str(error.value)


def test_payment_rails_command_prints_the_table(capsys) -> None:
    assert main(["payment-rails"]) == 0
    assert capsys.readouterr().out.splitlines()[0].startswith("mpesa")
