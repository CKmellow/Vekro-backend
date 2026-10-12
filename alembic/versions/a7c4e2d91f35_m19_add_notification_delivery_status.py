"""m19 add notification delivery status

Revision ID: a7c4e2d91f35
Revises: f3a91c7d2b10
Create Date: 2026-10-12 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a7c4e2d91f35"
down_revision: Union[str, Sequence[str], None] = "f3a91c7d2b10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


delivery_status_enum = sa.Enum(
    "not_attempted",
    "sent",
    "failed",
    name="notification_delivery_status",
)


def upgrade() -> None:
    delivery_status_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "notifications",
        sa.Column(
            "delivery_status",
            delivery_status_enum,
            server_default=sa.text("'not_attempted'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("notifications", "delivery_status")
    delivery_status_enum.drop(op.get_bind(), checkfirst=True)
