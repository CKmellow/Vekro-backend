"""m28 one account for buying and selling

docs/accounts-and-modes.md: ``users.role`` becomes ``member | admin``; selling is a store on the
account (``store_status`` none/active/closed, opened/closed times, accepted seller terms). Every
existing seller gets an active store opened at its sign-up (terms ``legacy``); buyers get none;
admins stay admins and can never hold a store (check constraint). No rows are merged.
``payout_verified_at`` records SMS proof of the payout number: only Google sign-ups, whose phone was
proven by a code and became the payout number, start verified; other stores must confirm theirs
before they take new orders. The
``store_opened`` anchor events for migrated stores come from the ``backfill-store-events`` CLI,
never from a migration.

Revision ID: b8d1f3a5c7e2
Revises: a7c3e9d2f4b1
Create Date: 2026-11-11 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8d1f3a5c7e2"
down_revision: Union[str, Sequence[str], None] = "a7c3e9d2f4b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STORE_STATUS = sa.Enum("none", "active", "closed", name="store_status")


def upgrade() -> None:
    bind = op.get_bind()
    STORE_STATUS.create(bind, checkfirst=True)
    op.add_column(
        "users",
        sa.Column("store_status", STORE_STATUS, nullable=False, server_default="none"),
    )
    op.add_column("users", sa.Column("store_opened_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("store_closed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("seller_terms_version", sa.String(length=32), nullable=True))
    op.add_column(
        "users", sa.Column("seller_terms_accepted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "users", sa.Column("payout_verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        "UPDATE users SET payout_verified_at = google_linked_at "
        "WHERE google_sub IS NOT NULL AND has_password = false AND mpesa_phone = phone"
    )

    # Every seller keeps selling: an active store, opened when the account was made.
    op.execute(
        "UPDATE users SET store_status = 'active', store_opened_at = created_at, "
        "seller_terms_version = 'legacy', seller_terms_accepted_at = created_at "
        "WHERE role = 'seller'"
    )
    # user_role: buyer/seller/admin -> member/admin.
    op.execute("ALTER TYPE user_role RENAME TO user_role_old")
    op.execute("CREATE TYPE user_role AS ENUM ('member', 'admin')")
    op.execute(
        "ALTER TABLE users ALTER COLUMN role TYPE user_role USING "
        "(CASE WHEN role::text = 'admin' THEN 'admin' ELSE 'member' END)::user_role"
    )
    op.execute("DROP TYPE user_role_old")
    op.create_check_constraint(
        "ck_users_admin_has_no_store", "users", "role <> 'admin' OR store_status = 'none'"
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_admin_has_no_store", "users", type_="check")
    op.execute("ALTER TYPE user_role RENAME TO user_role_new")
    op.execute("CREATE TYPE user_role AS ENUM ('buyer', 'seller', 'admin')")
    # An account that bought and sold comes back as a seller; its purchases stay readable because
    # order reads check the order's buyer_id, not the role.
    op.execute(
        "ALTER TABLE users ALTER COLUMN role TYPE user_role USING (CASE "
        "WHEN role::text = 'admin' THEN 'admin' "
        "WHEN store_status IN ('active', 'closed') THEN 'seller' "
        "ELSE 'buyer' END)::user_role"
    )
    op.execute("DROP TYPE user_role_new")
    op.drop_column("users", "payout_verified_at")
    op.drop_column("users", "seller_terms_accepted_at")
    op.drop_column("users", "seller_terms_version")
    op.drop_column("users", "store_closed_at")
    op.drop_column("users", "store_opened_at")
    op.drop_column("users", "store_status")
    STORE_STATUS.drop(op.get_bind(), checkfirst=True)
