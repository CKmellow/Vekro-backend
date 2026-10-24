"""SQLite helpers for tests that build only the tables they need."""

from typing import Any, cast

from app.db.base import Base
from app.models.dispute import Dispute
from sqlalchemy import Table, text
from sqlalchemy.engine import Engine


def create_disputes_table(engine: Engine) -> None:
    """The order read looks up disputes; SQLite can't render the JSONB server default."""
    column = cast(Any, Dispute.__table__.c.evidence)
    original = column.server_default
    column.server_default = text("'{}'")
    try:
        Base.metadata.create_all(engine, tables=[cast(Table, Dispute.__table__)])
    finally:
        column.server_default = original
