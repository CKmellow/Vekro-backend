# pyright: reportAttributeAccessIssue=false

from logging.config import fileConfig
from typing import Any

from alembic import context as alembic_context
from app.core.settings import get_settings
from app.db.base import Base
from app.models import Listing, Transaction, User
from sqlalchemy import engine_from_config, pool

# Ensure SQLAlchemy model classes are imported so Base.metadata is populated.
_ = (User, Listing, Transaction)

ctx: Any = alembic_context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = ctx.config  # type: ignore[attr-defined]

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()


def _normalize_sync_sqlalchemy_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


config.set_main_option(
    "sqlalchemy.url", _normalize_sync_sqlalchemy_url(settings.alembic_database_url)
)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    ctx.configure(  # type: ignore[attr-defined]
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with ctx.begin_transaction():  # type: ignore[attr-defined]
        ctx.run_migrations()  # type: ignore[attr-defined]


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        ctx.configure(  # type: ignore[attr-defined]
            connection=connection, target_metadata=target_metadata
        )

        with ctx.begin_transaction():  # type: ignore[attr-defined]
            ctx.run_migrations()  # type: ignore[attr-defined]


if ctx.is_offline_mode():  # type: ignore[attr-defined]
    run_migrations_offline()
else:
    run_migrations_online()
