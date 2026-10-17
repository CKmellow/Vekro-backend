"""m20 add escrow custody columns

Adds the eConfirm custody columns to ``escrows``:
  * custody_provider        - which provider holds the escrow (default 'simulated')
  * econfirm_confirmation_code - Fernet-encrypted confirmation code (nullable)
  * econfirm_payer_phone    - payer MSISDN used for funding/reversal (nullable)

Additive and non-destructive: all adds are nullable; custody_provider carries a
server default of 'simulated', so existing rows read as simulated custody
without any row rewrite. No existing data is modified.

Revision ID: c9f2a04d6b71
Revises: b8d15e3f7a42
Create Date: 2026-10-17 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9f2a04d6b71"
down_revision: Union[str, Sequence[str], None] = "b8d15e3f7a42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "escrows",
        sa.Column(
            "custody_provider",
            sa.String(length=40),
            nullable=True,
            server_default="simulated",
        ),
    )
    op.add_column(
        "escrows",
        sa.Column("econfirm_confirmation_code", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "escrows",
        sa.Column("econfirm_payer_phone", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("escrows", "econfirm_payer_phone")
    op.drop_column("escrows", "econfirm_confirmation_code")
    op.drop_column("escrows", "custody_provider")
