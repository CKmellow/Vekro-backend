"""M28 role migration (docs/accounts-and-modes.md, section 2) on a scratch PostgreSQL database.

Skipped unless TEST_POSTGRES_URL is set (CI's Migrations job sets it). Locally:

    TEST_POSTGRES_URL=postgresql+psycopg://vekro:vekro@127.0.0.1:5432/vekro pytest -m postgres
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="TEST_POSTGRES_URL unset"),
]

BEFORE = "a7c3e9d2f4b1"
AFTER = "b8d1f3a5c7e2"
BACKEND = Path(__file__).resolve().parents[1]


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "ALEMBIC_DATABASE_URL": url, "DATABASE_URL": url}
    subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env, check=True, timeout=300
    )


@pytest.fixture
def scratch_url():
    server = make_url(os.environ["TEST_POSTGRES_URL"])
    name = f"vekro_m28_migration_{uuid.uuid4().hex[:8]}"
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield server.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _insert(conn, phone: str, role: str, **extra) -> uuid.UUID:
    user_id = uuid.uuid4()
    columns = {"id": user_id, "name": "Someone", "phone": phone, "role": role, **extra}
    names = ", ".join(columns)
    values = ", ".join(f":{key}" for key in columns)
    conn.execute(
        text(
            f"INSERT INTO users ({names}, password_hash, created_at, updated_at) "
            f"VALUES ({values}, 'x', now() - interval '30 days', now())"
        ),
        columns,
    )
    return user_id


def test_roles_map_to_member_and_admin_and_back(scratch_url: str) -> None:
    _alembic(scratch_url, "upgrade", BEFORE)
    engine = create_engine(scratch_url)
    with engine.begin() as conn:
        buyer = _insert(conn, "+254700000001", "buyer")
        seller = _insert(conn, "+254700000002", "seller", mpesa_phone="+254700000002")
        new_seller = _insert(conn, "+254700000003", "seller")
        google_seller = _insert(
            conn,
            "+254700000004",
            "seller",
            mpesa_phone="+254700000004",
            google_sub="sub-4",
            google_email="g@example.com",
            google_linked_at=None,
            has_password=False,
        )
        conn.execute(
            text("UPDATE users SET google_linked_at = now() WHERE id = :id"), {"id": google_seller}
        )
        admin = _insert(conn, "+254700000005", "admin")
        before = conn.execute(text("SELECT count(*) FROM users")).scalar_one()

    _alembic(scratch_url, "upgrade", AFTER)
    with engine.connect() as conn:
        rows = {
            row.id: row
            for row in conn.execute(
                text(
                    "SELECT id, role::text AS role, store_status::text AS store, store_opened_at, "
                    "created_at, seller_terms_version, payout_verified_at FROM users"
                )
            )
        }
    # Nothing merged, nothing lost.
    assert len(rows) == before == 5
    assert (rows[buyer].role, rows[buyer].store) == ("member", "none")
    assert (rows[admin].role, rows[admin].store) == ("admin", "none")
    for sold in (seller, new_seller, google_seller):
        row = rows[sold]
        assert (row.role, row.store) == ("member", "active")
        assert row.store_opened_at == row.created_at
        assert row.seller_terms_version == "legacy"
    # Only the Google sign-up proved its payout number by SMS; the others must confirm theirs.
    assert rows[google_seller].payout_verified_at is not None
    assert rows[seller].payout_verified_at is None
    assert rows[new_seller].payout_verified_at is None

    # An admin can never hold a store.
    with pytest.raises(Exception, match="ck_users_admin_has_no_store"):
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE users SET store_status = 'active' WHERE id = :id"), {"id": admin}
            )

    # A member who both bought and sold comes back as a seller; a member with no store as a buyer.
    _alembic(scratch_url, "downgrade", BEFORE)
    with engine.connect() as conn:
        roles = dict(conn.execute(text("SELECT id, role::text FROM users")).all())
    engine.dispose()
    assert roles == {
        buyer: "buyer",
        seller: "seller",
        new_seller: "seller",
        google_seller: "seller",
        admin: "admin",
    }
