"""seller profile, logo and code purpose

Revision ID: d5a2c7e9b318
Revises: c3e8a5d1f427
Create Date: 2026-10-26 00:00:01.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5a2c7e9b318"
down_revision: Union[str, Sequence[str], None] = "c3e8a5d1f427"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("shop_name", sa.String(length=60), nullable=True))
    op.add_column("users", sa.Column("shop_area", sa.String(length=80), nullable=True))
    op.add_column("users", sa.Column("shop_about", sa.String(length=280), nullable=True))
    op.add_column("users", sa.Column("logo_content_type", sa.String(length=32), nullable=True))
    op.add_column("users", sa.Column("logo_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("logo_data", sa.LargeBinary(), nullable=True))
    op.add_column(
        "phone_codes",
        sa.Column(
            "purpose", sa.String(length=16), nullable=False, server_default=sa.text("'sign_in'")
        ),
    )


def downgrade() -> None:
    op.drop_column("phone_codes", "purpose")
    for column in (
        "logo_data",
        "logo_updated_at",
        "logo_content_type",
        "shop_about",
        "shop_area",
        "shop_name",
    ):
        op.drop_column("users", column)
