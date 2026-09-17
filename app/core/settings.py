from functools import lru_cache
from typing import Any

from pydantic import ValidationError, field_validator
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


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        missing_or_invalid: list[str] = []
        for err in exc.errors():
            loc: list[Any] | tuple[Any, ...] = err.get("loc", ())
            if not loc:
                continue
            missing_or_invalid.append(str(loc[0]).upper())

        if missing_or_invalid:
            var_list = ", ".join(sorted(set(missing_or_invalid)))
            raise RuntimeError(
                f"Invalid environment configuration. Check required variables: {var_list}."
            ) from exc

        raise RuntimeError("Invalid environment configuration.") from exc
