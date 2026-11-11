"""m28 google sign-in

Continue with Google (#324): ``users.google_sub`` (unique, the only key a Google sign-in is matched
on) with the verified e-mail kept for display, ``users.has_password`` (false for a seller who signed
up with Google until they set one), ``oauth_states`` (single-use state, nonce and PKCE verifier, stored
hashed and short-lived) and ``google_signups`` (a Google identity waiting for an SMS-verified phone).

Revision ID: a7c3e9d2f4b1
Revises: f1b8d4e6a2c9
Create Date: 2026-11-11 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c3e9d2f4b1"
down_revision: Union[str, Sequence[str], None] = "f1b8d4e6a2c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("google_sub", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("google_email", sa.String(length=320), nullable=True))
    op.add_column("users", sa.Column("google_linked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users",
        sa.Column("has_password", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_users_google_sub", "users", ["google_sub"], unique=True)
    op.create_table(
        "oauth_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("state_hash", sa.String(length=128), nullable=False, unique=True),
        sa.Column("nonce", sa.String(length=128), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("return_to", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "google_signups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("token_hash", sa.String(length=128), nullable=False, unique=True),
        sa.Column("google_sub", sa.String(length=255), nullable=False),
        sa.Column("google_email", sa.String(length=320), nullable=False),
        sa.Column("google_name", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("google_signups")
    op.drop_table("oauth_states")
    op.drop_index("ix_users_google_sub", table_name="users")
    op.drop_column("users", "has_password")
    op.drop_column("users", "google_linked_at")
    op.drop_column("users", "google_email")
    op.drop_column("users", "google_sub")
