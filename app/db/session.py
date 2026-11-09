from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import get_settings


def _normalize_sync_sqlalchemy_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


@dataclass(frozen=True)
class DatabaseTarget:
    """The database the sync session uses, and which setting chose it (#279).

    ALEMBIC_DATABASE_URL wins over DATABASE_URL when it is set, so overriding only DATABASE_URL
    does not move the app or the scripts to another database. ``url`` holds credentials: never
    print it; print ``describe()``.
    """

    setting: str
    url: str
    name: str

    def describe(self) -> str:
        if self.setting == "ALEMBIC_DATABASE_URL":
            return (
                f"database {self.name} (from ALEMBIC_DATABASE_URL, which is used before "
                "DATABASE_URL)"
            )
        if self.setting == "DATABASE_URL":
            return f"database {self.name} (from DATABASE_URL; ALEMBIC_DATABASE_URL is not set)"
        return f"database {self.name} ({self.setting})"

    def with_name(self, name: str, *, setting: str = "--database") -> DatabaseTarget:
        url = make_url(self.url).set(database=name).render_as_string(hide_password=False)
        setting = f"{setting}, credentials from {self.setting}"
        return DatabaseTarget(setting=setting, url=url, name=name)


def database_target(settings=None) -> DatabaseTarget:
    settings = settings or get_settings()
    # Prefer the migration URL because it is intended for sync/psycopg usage.
    setting = "ALEMBIC_DATABASE_URL" if settings.alembic_database_url else "DATABASE_URL"
    url = _normalize_sync_sqlalchemy_url(settings.alembic_database_url or settings.database_url)
    return DatabaseTarget(setting=setting, url=url, name=make_url(url).database or "")


def session_factory_for(target: DatabaseTarget) -> sessionmaker:
    """A session factory for one explicit database (scripts that take --database)."""
    engine = create_engine(target.url, pool_pre_ping=True)
    return sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, class_=Session
    )


def _resolve_sync_database_url() -> str:
    return database_target().url


@lru_cache
def _get_session_factory():
    settings = get_settings()
    engine = create_engine(
        _resolve_sync_database_url(),
        echo=settings.database_echo,
        pool_pre_ping=True,
    )
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )
    from app.services.notification_delivery import install_notification_sms_hooks, run_in_background

    install_notification_sms_hooks(factory, dispatch=run_in_background)
    return factory


def get_db() -> Generator[Session, None, None]:
    db = _get_session_factory()()
    try:
        yield db
    finally:
        db.close()
