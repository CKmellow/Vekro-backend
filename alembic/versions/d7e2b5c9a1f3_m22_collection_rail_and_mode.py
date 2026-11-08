"""m22 collection rail and payment mode

Additive columns on ``transactions`` (#257): the rail that collects the order's money (simulated,
daraja, pesapal, econfirm) and whether it is a sandbox or live payment, so the UI never calls
sandbox money real or real money a demo. Backfill from what existing rows already show: eConfirm
custody (eConfirm has only a production API), card orders (Pesapal, sandbox so far), and prompts
with a Daraja CheckoutRequestID (sandbox so far); everything else is simulated.

Revision ID: d7e2b5c9a1f3
Revises: c3f1a8d2e6b4
Create Date: 2026-11-08 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d7e2b5c9a1f3"
down_revision: Union[str, Sequence[str], None] = "c3f1a8d2e6b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column(
            "collection_rail",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'simulated'"),
        ),
    )
    op.add_column(
        "transactions",
        sa.Column(
            "payment_mode",
            sa.String(length=8),
            nullable=False,
            server_default=sa.text("'sandbox'"),
        ),
    )
    op.execute(
        "UPDATE transactions SET collection_rail = 'econfirm', payment_mode = 'live' "
        "WHERE custody_provider = 'econfirm'"
    )
    op.execute(
        "UPDATE transactions SET collection_rail = 'pesapal', payment_mode = 'sandbox' "
        "WHERE payment_method = 'card'"
    )
    op.execute(
        "UPDATE transactions SET collection_rail = 'daraja', payment_mode = 'sandbox' "
        "WHERE collection_rail = 'simulated' AND EXISTS ("
        "SELECT 1 FROM payment_prompts p WHERE p.transaction_id = transactions.id "
        "AND p.checkout_request_id LIKE 'ws_CO_%')"
    )


def downgrade() -> None:
    op.drop_column("transactions", "payment_mode")
    op.drop_column("transactions", "collection_rail")
