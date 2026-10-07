from functools import lru_cache
from typing import Any, Literal

from pydantic import ValidationError, field_validator, model_validator
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
    "pesapal": (
        "pesapal_base_url",
        "pesapal_consumer_key",
        "pesapal_consumer_secret",
        "pesapal_callback_url",
    ),
    "intasend": (
        "intasend_base_url",
        "intasend_publishable_key",
        "intasend_secret_key",
        "intasend_webhook_secret",
    ),
    "econfirm": (
        "econfirm_base_url",
        "econfirm_api_key",
        "econfirm_api_secret",
    ),
}


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

    africastalking_username: str = "sandbox"
    africastalking_api_key: str = ""
    africastalking_sender_id: str = ""

    frontend_url: str = "http://localhost:3000"
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

    custody_mode: Literal["tier_1", "tier_2"] = "tier_2"
    custody_collection_rail_priority: str = "simulated"
    custody_payout_rail_priority: str = "simulated"
    allow_live_payouts: bool = False
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
    econfirm_base_url: str = ""
    econfirm_api_key: str = ""
    econfirm_api_secret: str = ""

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

        if self.allow_live_payouts and environment != "production":
            violations.append("ALLOW_LIVE_PAYOUTS=true requires ENVIRONMENT=production")

        if violations:
            raise ValueError("Invalid runtime configuration: " + "; ".join(violations))

        return self

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

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
