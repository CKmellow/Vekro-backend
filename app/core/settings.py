from functools import lru_cache
from typing import Any, Literal

from pydantic import ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @model_validator(mode="after")
    def _validate_production_security(self) -> "Settings":
        if self.environment.strip().lower() != "production":
            return self

        violations: list[str] = []
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

        if violations:
            raise ValueError("Insecure production configuration: " + "; ".join(violations))

        return self

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


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
