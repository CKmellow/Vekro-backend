"""#305: the full audit reads one consistent snapshot on Postgres."""

from __future__ import annotations

from typing import Any

from app.services.anchoring import full_audit


class _Dialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _Bind:
    def __init__(self, name: str) -> None:
        self.dialect = _Dialect(name)


class _Session:
    def __init__(self, dialect: str, *, in_transaction: bool = False) -> None:
        self._dialect = dialect
        self._in_transaction = in_transaction
        self.options: list[dict[str, Any]] = []

    def in_transaction(self) -> bool:
        return self._in_transaction

    def get_bind(self) -> _Bind:
        return _Bind(self._dialect)

    def connection(self, execution_options: dict[str, Any]) -> None:
        self.options.append(execution_options)


def test_postgres_audits_run_in_one_read_only_snapshot() -> None:
    db = _Session("postgresql")
    full_audit._use_one_snapshot(db)  # type: ignore[arg-type]
    assert db.options == [{"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}]


def test_an_open_transaction_or_sqlite_is_left_alone() -> None:
    for db in (_Session("postgresql", in_transaction=True), _Session("sqlite")):
        full_audit._use_one_snapshot(db)  # type: ignore[arg-type]
        assert db.options == []
