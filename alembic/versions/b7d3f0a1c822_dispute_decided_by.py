"""dispute decided_by_user_id

Revision ID: b7d3f0a1c822
Revises: a6c2e9f4b711
Create Date: 2026-10-25 00:00:01.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7d3f0a1c822"
down_revision: Union[str, Sequence[str], None] = "a6c2e9f4b711"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "disputes",
        sa.Column("decided_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_disputes_decided_by_user_id_users",
        "disputes",
        "users",
        ["decided_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_disputes_decided_by_user_id_users", "disputes", type_="foreignkey")
    op.drop_column("disputes", "decided_by_user_id")
