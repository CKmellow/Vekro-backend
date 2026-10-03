from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from app.services.custody import pesapal_ipn_registration
from app.services.custody.pesapal_auth import PesapalHttpResponse


def test_register_ipn_extracts_nested_ipn_id(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        pesapal_ipn_registration,
        "PesapalTokenManager",
        lambda **kwargs: SimpleNamespace(get_access_token=lambda: "pesapal-token"),
    )

    def _transport(method, url, headers, payload, timeout_seconds):
        observed["method"] = method
        observed["url"] = url
        observed["headers"] = headers
        observed["payload"] = payload
        observed["timeout"] = timeout_seconds
        return PesapalHttpResponse(
            status_code=200,
            body={"data": {"ipn_id": "ipn-900"}},
            text="ok",
        )

    monkeypatch.setattr(pesapal_ipn_registration, "default_pesapal_http_transport", _transport)

    ipn_id = pesapal_ipn_registration.register_pesapal_ipn(
        base_url="https://cybqa.pesapal.com/pesapalv3",
        consumer_key="pesapal-key",
        consumer_secret="pesapal-secret",
        callback_url="https://backend.example/api/webhooks/pesapal/callback",
        notification_type="POST",
    )

    assert ipn_id == "ipn-900"
    assert observed["method"] == "POST"
    assert str(observed["url"]).endswith("/api/Transactions/RegisterIPN")
    headers = dict(observed["headers"] or {})
    payload = dict(observed["payload"] or {})
    assert headers["Authorization"] == "Bearer pesapal-token"
    assert payload["ipn_notification_type"] == "POST"


def test_register_ipn_raises_when_response_missing_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pesapal_ipn_registration,
        "PesapalTokenManager",
        lambda **kwargs: SimpleNamespace(get_access_token=lambda: "pesapal-token"),
    )
    monkeypatch.setattr(
        pesapal_ipn_registration,
        "default_pesapal_http_transport",
        lambda *args, **kwargs: PesapalHttpResponse(status_code=200, body={}, text="ok"),
    )

    with pytest.raises(
        pesapal_ipn_registration.PesapalIpnRegistrationError,
        match="ipn_id was missing",
    ):
        pesapal_ipn_registration.register_pesapal_ipn(
            base_url="https://cybqa.pesapal.com/pesapalv3",
            consumer_key="pesapal-key",
            consumer_secret="pesapal-secret",
            callback_url="https://backend.example/api/webhooks/pesapal/callback",
        )


def test_persist_pesapal_ipn_id_replaces_existing_value(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_NAME=Vekro Backend\nPESAPAL_IPN_ID=old-id\n", encoding="utf-8")

    pesapal_ipn_registration.persist_pesapal_ipn_id(env_file=env_file, ipn_id="new-id")

    text = env_file.read_text(encoding="utf-8")
    assert "PESAPAL_IPN_ID=new-id" in text
    assert "old-id" not in text


def test_persist_pesapal_ipn_id_appends_when_missing(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("APP_NAME=Vekro Backend\n", encoding="utf-8")

    pesapal_ipn_registration.persist_pesapal_ipn_id(env_file=env_file, ipn_id="ipn-xyz")

    text = env_file.read_text(encoding="utf-8")
    assert text.endswith("PESAPAL_IPN_ID=ipn-xyz\n")