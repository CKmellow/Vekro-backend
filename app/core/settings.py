from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal

from pydantic import SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_CUSTODY_RAILS = frozenset({"simulated", "loop", "pesapal", "intasend", "econfirm"})
SUPPORTED_SIMULATED_SCENARIOS = frozenset(
    {"success", "failed_definite", "timeout", "duplicate", "out_of_order", "unknown"}
)
ENABLED_CREDENTIAL_FIELDS: dict[str, tuple[str, ...]] = {
    "loop": (
        "loop_base_url",
        "loop_client_id",
        "loop_client_secret",
        "loop_shortcode",
        "loop_passkey",
    ),
    # PESAPAL_CALLBACK_URL is required only when Pesapal is a custody collection rail (below);
    # card checkout returns buyers to FRONTEND_URL instead (#253).
    "pesapal": (
        "pesapal_base_url",
        "pesapal_consumer_key",
        "pesapal_consumer_secret",
    ),
    "intasend": (
        "intasend_base_url",
        "intasend_publishable_key",
        "intasend_secret_key",
        "intasend_webhook_secret",
    ),
    # eConfirm v1 authenticates with a single Bearer API key; it has no secret.
    "econfirm": (
        "econfirm_base_url",
        "econfirm_api_key",
    ),
}


# The most codes one client may ask for per hour in production (anti SMS pumping).
PHONE_CODE_CLIENT_HOURLY_LIMIT_MAX = 50


class Settings(BaseSettings):
    app_name: str = "Vekro Backend"
    environment: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    secret_key: str = ""

    database_url: str = ""
    alembic_database_url: str = ""
    database_echo: bool = False

    daraja_env: str = "sandbox"
    daraja_consumer_key: str = ""
    daraja_consumer_secret: str = ""
    daraja_shortcode: str = "174379"
    daraja_passkey: str = ""
    daraja_callback_url: str = ""
    # #224: real STK push and STK-query confirmation. Off: the stub gateway, as before.
    daraja_enabled: bool = False
    daraja_timeout_seconds: float = 15.0

    # M23 USSD: AT's requests are unsigned, so the callback URL carries this secret
    # (/ussd/africastalking/<token>). Blank turns the USSD endpoint off.
    ussd_callback_token: SecretStr = SecretStr("")
    # Optional extra check: comma-separated client IPs allowed to call. Enforced only behind the
    # Cloudflare tunnel (#289): with USSD_CLIENT_IP_HEADER=cf-connecting-ip the caller is read from
    # CF-Connecting-IP, which Cloudflare's edge always overwrites. Without it the allowlist is off
    # (X-Forwarded-For can be set by the caller) and the path token alone protects the callback.
    ussd_allowed_ips: str = ""
    ussd_client_ip_header: str = ""
    ussd_session_seconds: int = 180

    # M24 B4: the state timeouts in minutes instead of hours and days, for a class demo. Refused
    # in production. The scheduler intervals are in docs/demo-modes.md.
    demo_fast_timeouts: bool = False

    africastalking_username: str = "sandbox"
    africastalking_api_key: str = ""
    africastalking_sender_id: str = ""

    frontend_url: str = "http://localhost:3000"

    # Continue with Google (#324). Off while either is blank. The redirect URI defaults to the web
    # app's /api proxy: {FRONTEND_URL}/api/auth/google/callback (registered in the Google client).
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    google_redirect_uri: str = ""
    cors_origins: str = "http://localhost:3000"
    cors_allow_credentials: bool = True

    session_cookie_name: str = "vekro_session"
    session_cookie_secure: bool = False
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    session_cookie_max_age_seconds: int = 60 * 60 * 24 * 7

    csrf_cookie_name: str = "vekro_csrf"
    csrf_cookie_secure: bool = False
    csrf_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    csrf_header_name: str = "X-CSRF-Token"

    login_rate_limit_max_attempts: int = 10
    login_rate_limit_window_seconds: int = 60
    login_lockout_max_attempts: int = 5
    login_lockout_seconds: int = 15 * 60

    # Buyer sign-in with an SMS code (POST /auth/phone/*).
    phone_code_ttl_seconds: int = 5 * 60
    phone_code_resend_seconds: int = 30
    phone_code_max_attempts: int = 5
    phone_code_lockout_seconds: int = 5 * 60
    phone_code_hourly_limit: int = 5
    # Codes one client (IP) may ask for across all numbers per hour, against SMS pumping. The e2e
    # suite raises it (every test seller verifies a payout number from 127.0.0.1); production
    # refuses more than PHONE_CODE_CLIENT_HOURLY_LIMIT_MAX.
    phone_code_client_hourly_limit: int = 20
    # Local development and end-to-end tests only: return the code in the request-code
    # response, since no SMS is sent without a gateway key. Refused in production.
    phone_code_dev_echo: bool = False

    custody_mode: Literal["tier_1", "tier_2"] = "tier_2"
    custody_collection_rail_priority: str = "simulated"
    custody_payout_rail_priority: str = "simulated"
    allow_live_payouts: bool = False
    # Shared secret for POST /transactions/payment-callback, sent as the
    # X-Payment-Callback-Secret header. Blank: the callback is refused in
    # production and open elsewhere (local simulated rail / e2e tests).
    payment_callback_secret: str = ""
    simulated_collection_default_scenario: str = "success"
    simulated_payout_default_scenario: str = "success"
    simulated_trigger_prefix: str = "sim:"

    loop_enabled: bool = False
    loop_base_url: str = ""
    loop_client_id: str = ""
    loop_client_secret: str = ""
    loop_shortcode: str = ""
    loop_passkey: str = ""

    pesapal_enabled: bool = False
    pesapal_base_url: str = ""
    pesapal_consumer_key: str = ""
    pesapal_consumer_secret: str = ""
    pesapal_callback_url: str = ""
    pesapal_ipn_id: str = ""

    intasend_enabled: bool = False
    intasend_base_url: str = ""
    intasend_publishable_key: str = ""
    intasend_secret_key: str = ""
    intasend_webhook_secret: str = ""

    econfirm_enabled: bool = False
    econfirm_base_url: str = "https://api.econfirm.co.ke/api/v1"
    econfirm_api_key: str = ""
    econfirm_api_secret: str = ""
    econfirm_timeout_seconds: float = 15.0
    # eConfirm sits behind Cloudflare, which 403-bans stdlib client User-Agents
    # (Error 1010). A browser UA clears that generic bot filter; keep it configurable.
    econfirm_user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    econfirm_min_amount: Decimal = Decimal("100")
    econfirm_max_amount: Decimal = Decimal("1000")
    econfirm_poll_interval_seconds: float = 10.0
    econfirm_fallback_email: str = ""
    # Dedicated, rotatable Fernet key(s) for encrypting the eConfirm
    # confirmation_code at rest (comma-separated; first is primary). NOT the app
    # SECRET_KEY. Required when ECONFIRM_ENABLED=true.
    econfirm_code_encryption_key: str = ""

    # M21 anchoring. HMAC key(s) for admin_ref in anchored payloads, so admin UUIDs never reach
    # buyers or sellers (decision D5). Comma-separated, newest first; each 32+ characters.
    # Required in production; elsewhere a fixed development key is used when blank.
    anchor_admin_ref_secret: str = ""
    # Raise (instead of logging and recording a fallback) when a status change or a ledger or
    # audit row has no anchor event. On for the test suite (tests/conftest.py).
    anchor_strict_events: bool = False
    # M21 A5: the batcher and submitter (python -m app.jobs.anchoring). With anchoring disabled,
    # events are still recorded and nothing is sent to a chain.
    anchor_enabled: bool = False
    anchor_rpc_url: str = ""
    # 31337 Anvil, 84532 Base Sepolia, 8453 Base.
    anchor_chain_id: int = 31337
    anchor_registry_address: str = ""
    # The anchorer's key (0x + 64 hex). Never logged; only its address is ever shown.
    anchor_private_key: SecretStr = SecretStr("")
    anchor_max_batch_size: int = 1000
    anchor_min_confirmations: int = 2
    anchor_poll_interval_seconds: float = 30.0
    anchor_fee_bump_after_seconds: int = 120
    anchor_max_fee_per_gas_gwei: Decimal = Decimal("5")
    anchor_retry_backoff_seconds: int = 60
    anchor_rpc_timeout_seconds: float = 10.0
    # M21 A8: no payout reaches a rail until its queued authorisation is anchored and its proof
    # matches the registry root (app/services/anchoring/payout_gate.py). Needs ANCHOR_ENABLED.
    anchor_gate_payouts: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("secret_key", "database_url", "alembic_database_url")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator(
        "session_cookie_max_age_seconds",
        "login_rate_limit_max_attempts",
        "login_rate_limit_window_seconds",
        "login_lockout_max_attempts",
        "login_lockout_seconds",
        "phone_code_ttl_seconds",
        "phone_code_resend_seconds",
        "phone_code_max_attempts",
        "phone_code_lockout_seconds",
        "phone_code_hourly_limit",
        "phone_code_client_hourly_limit",
    )
    @classmethod
    def _must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be greater than 0")
        return value

    @field_validator("csrf_header_name")
    @classmethod
    def _csrf_header_name_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator(
        "simulated_collection_default_scenario",
        "simulated_payout_default_scenario",
    )
    @classmethod
    def _validate_simulated_default_scenario(cls, value: str) -> str:
        normalized = value.strip().lower().replace("-", "_")
        aliases = {
            "succeeded": "success",
            "ok": "success",
            "failed": "failed_definite",
            "failure": "failed_definite",
            "declined": "failed_definite",
        }
        normalized = aliases.get(normalized, normalized)

        if normalized not in SUPPORTED_SIMULATED_SCENARIOS:
            raise ValueError("must be one of: " + ", ".join(sorted(SUPPORTED_SIMULATED_SCENARIOS)))

        return normalized

    @field_validator("simulated_trigger_prefix")
    @classmethod
    def _validate_simulated_trigger_prefix(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    def _parse_rail_priority(self, raw: str, setting_name: str) -> list[str]:
        rails = [value.strip().lower() for value in raw.split(",") if value.strip()]
        if not rails:
            raise ValueError(f"{setting_name} must include at least one rail.")
        if len(rails) != len(set(rails)):
            raise ValueError(f"{setting_name} must not contain duplicate rails.")
        return rails

    def _append_missing_credentials(
        self,
        *,
        provider_name: str,
        enabled: bool,
        violations: list[str],
    ) -> None:
        if not enabled:
            return

        for field_name in ENABLED_CREDENTIAL_FIELDS[provider_name]:
            value = str(getattr(self, field_name, "")).strip()
            if not value:
                violations.append(
                    f"{field_name.upper()} must be set when {provider_name.upper()}_ENABLED=true"
                )

    @model_validator(mode="after")
    def _validate_runtime_configuration(self) -> "Settings":
        environment = self.environment.strip().lower()
        violations: list[str] = []

        if environment == "production":
            if self.phone_code_dev_echo:
                violations.append("PHONE_CODE_DEV_ECHO=false")
            if self.phone_code_client_hourly_limit > PHONE_CODE_CLIENT_HOURLY_LIMIT_MAX:
                violations.append(
                    f"PHONE_CODE_CLIENT_HOURLY_LIMIT<={PHONE_CODE_CLIENT_HOURLY_LIMIT_MAX}"
                )
            if not self.session_cookie_secure:
                violations.append("SESSION_COOKIE_SECURE=true")
            if not self.csrf_cookie_secure:
                violations.append("CSRF_COOKIE_SECURE=true")
            if not self.cors_allow_credentials:
                violations.append("CORS_ALLOW_CREDENTIALS=true")
            if self.frontend_url.strip().lower().startswith("http://"):
                violations.append("FRONTEND_URL must use https://")

            insecure_origins = [
                origin for origin in self.cors_origins_list if origin.lower().startswith("http://")
            ]
            if insecure_origins:
                violations.append("CORS_ORIGINS must use https:// origins in production")

        collection_rails: list[str] = []
        payout_rails: list[str] = []
        try:
            collection_rails = self.custody_collection_rail_priority_list
            payout_rails = self.custody_payout_rail_priority_list
        except ValueError as exc:
            violations.append(str(exc))

        if (
            self.pesapal_enabled
            and "pesapal" in collection_rails
            and not self.pesapal_callback_url.strip()
        ):
            violations.append(
                "PESAPAL_CALLBACK_URL must be set when pesapal is a custody collection rail"
            )

        configured_rails = set(collection_rails + payout_rails)
        unknown_rails = sorted(configured_rails.difference(SUPPORTED_CUSTODY_RAILS))
        if unknown_rails:
            violations.append("Unknown rails in custody priorities: " + ", ".join(unknown_rails))

        if "pesapal" in payout_rails:
            violations.append(
                "PESAPAL is collection-only and must not appear in CUSTODY_PAYOUT_RAIL_PRIORITY"
            )

        self._append_missing_credentials(
            provider_name="loop",
            enabled=self.loop_enabled,
            violations=violations,
        )
        self._append_missing_credentials(
            provider_name="pesapal",
            enabled=self.pesapal_enabled,
            violations=violations,
        )
        self._append_missing_credentials(
            provider_name="intasend",
            enabled=self.intasend_enabled,
            violations=violations,
        )
        self._append_missing_credentials(
            provider_name="econfirm",
            enabled=self.econfirm_enabled,
            violations=violations,
        )

        if self.econfirm_enabled:
            from app.services.custody.econfirm_crypto import is_valid_key_config

            if not is_valid_key_config(self.econfirm_code_encryption_key):
                violations.append(
                    "ECONFIRM_CODE_ENCRYPTION_KEY must hold at least one valid Fernet key "
                    "when ECONFIRM_ENABLED=true"
                )

        if environment == "production" or self.anchor_enabled:
            from app.services.anchoring.admin_ref import is_valid_secret_config

            if not is_valid_secret_config(self.anchor_admin_ref_secret):
                violations.append(
                    "ANCHOR_ADMIN_REF_SECRET must hold at least one key of 32+ characters "
                    "in production and whenever ANCHOR_ENABLED=true"
                )

        if self.anchor_enabled:
            from app.services.anchoring.chain_config import anchor_config_violations

            violations.extend(anchor_config_violations(self, environment=environment))
            if environment == "production" and not self.anchor_gate_payouts:
                violations.append(
                    "ANCHOR_GATE_PAYOUTS=true is required with ANCHOR_ENABLED=true in production"
                )
        elif self.anchor_gate_payouts:
            violations.append(
                "ANCHOR_GATE_PAYOUTS=true needs ANCHOR_ENABLED=true (payouts would wait forever)"
            )

        if self.daraja_enabled:
            if self.daraja_env not in ("sandbox", "production"):
                violations.append("DARAJA_ENV must be sandbox or production")
            missing = [
                name.upper()
                for name in (
                    "daraja_consumer_key",
                    "daraja_consumer_secret",
                    "daraja_shortcode",
                    "daraja_passkey",
                    "daraja_callback_url",
                )
                if not str(getattr(self, name)).strip()
            ]
            if missing:
                violations.append(
                    "DARAJA_ENABLED=true requires " + ", ".join(missing) + " (values not shown)"
                )
            # Daraja posts results to a public HTTPS URL (locally a tunnel, docs/demo-modes.md).
            callback = self.daraja_callback_url.strip()
            if callback and not callback.startswith("https://"):
                violations.append("DARAJA_CALLBACK_URL must use https://")

        if self.demo_fast_timeouts and environment == "production":
            violations.append("DEMO_FAST_TIMEOUTS=true is never allowed in production")

        if self.allow_live_payouts and environment != "production":
            violations.append("ALLOW_LIVE_PAYOUTS=true requires ENVIRONMENT=production")

        if violations:
            raise ValueError("Invalid runtime configuration: " + "; ".join(violations))

        return self

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id.strip() and self.google_client_secret.get_secret_value())

    @property
    def google_callback_url(self) -> str:
        explicit = self.google_redirect_uri.strip()
        return explicit or f"{self.frontend_url.rstrip('/')}/api/auth/google/callback"

    @property
    def custody_collection_rail_priority_list(self) -> list[str]:
        return self._parse_rail_priority(
            self.custody_collection_rail_priority,
            "CUSTODY_COLLECTION_RAIL_PRIORITY",
        )

    @property
    def custody_payout_rail_priority_list(self) -> list[str]:
        return self._parse_rail_priority(
            self.custody_payout_rail_priority,
            "CUSTODY_PAYOUT_RAIL_PRIORITY",
        )

    @property
    def live_payouts_enabled(self) -> bool:
        return self.environment.strip().lower() == "production" and self.allow_live_payouts


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        missing_or_invalid: list[str] = []
        general_errors: list[str] = []
        for err in exc.errors():
            loc: list[Any] | tuple[Any, ...] = err.get("loc", ())
            message = str(err.get("msg", "invalid value"))
            if not loc:
                general_errors.append(message)
                continue
            missing_or_invalid.append(str(loc[0]).upper())
            if message:
                general_errors.append(f"{str(loc[0]).upper()}: {message}")

        details: list[str] = []
        if missing_or_invalid:
            var_list = ", ".join(sorted(set(missing_or_invalid)))
            details.append(f"Check required/validated variables: {var_list}.")
        if general_errors:
            details.append("; ".join(sorted(set(general_errors))))
        if details:
            raise RuntimeError("Invalid environment configuration. " + " ".join(details)) from exc

        raise RuntimeError("Invalid environment configuration.") from exc
