from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from app.services.custody.pesapal_auth import (
    PesapalAuthError,
    PesapalTokenManager,
    default_pesapal_http_transport,
)


class PesapalIpnRegistrationError(Exception):
    pass


def register_pesapal_ipn(
    *,
    base_url: str,
    consumer_key: str,
    consumer_secret: str,
    callback_url: str,
    notification_type: str = "GET",
) -> str:
    token_manager = PesapalTokenManager(
        base_url=base_url,
        consumer_key=consumer_key,
        consumer_secret=consumer_secret,
    )
    access_token = token_manager.get_access_token()

    endpoint = f"{base_url.strip().rstrip('/')}/api/Transactions/RegisterIPN"
    response = default_pesapal_http_transport(
        "POST",
        endpoint,
        {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        {
            "url": callback_url,
            "ipn_notification_type": notification_type.strip().upper() or "GET",
        },
        10.0,
    )

    if response.status_code < 200 or response.status_code >= 300:
        raise PesapalIpnRegistrationError(
            "Pesapal IPN registration failed. "
            f"HTTP {response.status_code} {response.text[:180]}"
        )

    ipn_id = _extract_ipn_id(response.body)
    if not ipn_id:
        raise PesapalIpnRegistrationError(
            "Pesapal IPN registration succeeded but ipn_id was missing in response payload."
        )

    return ipn_id


def persist_pesapal_ipn_id(*, env_file: Path, ipn_id: str) -> None:
    line_value = f"PESAPAL_IPN_ID={ipn_id.strip()}"
    if not ipn_id.strip():
        raise PesapalIpnRegistrationError("Cannot persist blank PESAPAL_IPN_ID value.")

    existing_lines: list[str] = []
    if env_file.exists():
        existing_lines = env_file.read_text(encoding="utf-8").splitlines()

    replaced = False
    updated_lines: list[str] = []
    for line in existing_lines:
        if line.startswith("PESAPAL_IPN_ID="):
            updated_lines.append(line_value)
            replaced = True
            continue
        updated_lines.append(line)

    if not replaced:
        if updated_lines and updated_lines[-1] != "":
            updated_lines.append("")
        updated_lines.append(line_value)

    env_file.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")


def _extract_ipn_id(payload: dict[str, Any]) -> str:
    direct_keys = ("ipn_id", "ipnId", "notification_id", "notificationId")
    nested_keys = ("data", "result", "response")

    for key in direct_keys:
        value = payload.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text

    for nested_key in nested_keys:
        nested = payload.get(nested_key)
        if not isinstance(nested, dict):
            continue
        for key in direct_keys:
            value = nested.get(key)
            if value is not None:
                text = str(value).strip()
                if text:
                    return text

    return ""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Register Pesapal IPN callback URL and persist PESAPAL_IPN_ID into a local env file."
        )
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to environment file that should receive PESAPAL_IPN_ID (default: .env)",
    )
    parser.add_argument(
        "--callback-url",
        default="",
        help="Callback URL to register (defaults to PESAPAL_CALLBACK_URL env).",
    )
    parser.add_argument(
        "--notification-type",
        default="GET",
        help="IPN notification type to register (GET or POST).",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    base_url = os.getenv("PESAPAL_BASE_URL", "").strip()
    consumer_key = os.getenv("PESAPAL_CONSUMER_KEY", "").strip()
    consumer_secret = os.getenv("PESAPAL_CONSUMER_SECRET", "").strip()
    callback_url = args.callback_url.strip() or os.getenv("PESAPAL_CALLBACK_URL", "").strip()

    missing: list[str] = []
    if not base_url:
        missing.append("PESAPAL_BASE_URL")
    if not consumer_key:
        missing.append("PESAPAL_CONSUMER_KEY")
    if not consumer_secret:
        missing.append("PESAPAL_CONSUMER_SECRET")
    if not callback_url:
        missing.append("PESAPAL_CALLBACK_URL")

    if missing:
        raise SystemExit(
            "Missing required values for Pesapal IPN registration: " + ", ".join(missing)
        )

    try:
        ipn_id = register_pesapal_ipn(
            base_url=base_url,
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            callback_url=callback_url,
            notification_type=args.notification_type,
        )
        persist_pesapal_ipn_id(env_file=Path(args.env_file), ipn_id=ipn_id)
    except (PesapalAuthError, PesapalIpnRegistrationError) as exc:
        raise SystemExit(str(exc)) from exc

    print(f"Pesapal IPN registered successfully: {ipn_id}")
    print(f"Persisted PESAPAL_IPN_ID in {args.env_file}")


if __name__ == "__main__":
    main()