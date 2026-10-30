"""settings: add erfassung_ab (start of recording)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-30 18:00:00

Starting the app in the middle of a school year counted every earlier school day as "0 h worked". Days before
``settings.erfassung_ab`` are neutral in the account (Ist = Soll, not in the Prognose sample, not in the
to-do lists). NULL = recording covers the whole period (previous behaviour).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('erfassung_ab', sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('settings', schema=None) as batch_op:
        batch_op.drop_column('erfassung_ab')
