"""listing content (description, category, buyer name) and photos

Revision ID: c3a7d9e4f215
Revises: e2b7c4d91f08
Create Date: 2026-10-20 00:00:01.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3a7d9e4f215"
down_revision: Union[str, Sequence[str], None] = "e2b7c4d91f08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "listings",
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column("listings", sa.Column("category", sa.String(length=64), nullable=True))
    op.add_column("listings", sa.Column("buyer_name", sa.String(length=120), nullable=True))

    op.create_table(
        "listing_photos",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("listing_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(length=32), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["listing_id"], ["listings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_listing_photos_listing_id"), "listing_photos", ["listing_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_listing_photos_listing_id"), table_name="listing_photos")
    op.drop_table("listing_photos")
    op.drop_column("listings", "buyer_name")
    op.drop_column("listings", "category")
    op.drop_column("listings", "description")
