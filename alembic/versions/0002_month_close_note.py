"""month_close: reason and overridden items of a forced close

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-30 12:00:00

A forced month close (open items overridden with a reason, Konzept §10.1) stored its reason only in the
report file; the month overview could not show it. ``note`` keeps the reason, ``forced_items`` the
overridden open items ([{"kind", "message", "date"}]).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('month_close', schema=None) as batch_op:
        batch_op.add_column(sa.Column('note', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('forced_items', sa.JSON(), nullable=False, server_default=sa.text("'[]'")))


def downgrade() -> None:
    with op.batch_alter_table('month_close', schema=None) as batch_op:
        batch_op.drop_column('forced_items')
        batch_op.drop_column('note')
