"""m19 add transaction reference

Revision ID: b8d15e3f7a42
Revises: a7c4e2d91f35
Create Date: 2026-10-12 00:00:00.000000

"""

import secrets
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b8d15e3f7a42"
down_revision: Union[str, Sequence[str], None] = "a7c4e2d91f35"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copy of the app generator so this migration never changes behaviour.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _reference() -> str:
    return "VK-" + "".join(secrets.choice(_ALPHABET) for _ in range(6))


def upgrade() -> None:
    op.add_column("transactions", sa.Column("reference", sa.String(length=16), nullable=True))

    bind = op.get_bind()
    used: set[str] = set()
    for (transaction_id,) in bind.execute(sa.text("SELECT id FROM transactions")).fetchall():
        reference = _reference()
        while reference in used:
            reference = _reference()
        used.add(reference)
        bind.execute(
            sa.text("UPDATE transactions SET reference = :reference WHERE id = :id"),
            {"reference": reference, "id": transaction_id},
        )

    op.alter_column("transactions", "reference", nullable=False)
    op.create_index("ix_transactions_reference", "transactions", ["reference"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_transactions_reference", table_name="transactions")
    op.drop_column("transactions", "reference")
