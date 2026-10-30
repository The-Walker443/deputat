"""settings: drop the unused password_hash column

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-30 12:00:00

The app has no login (decision 29.10.2026, DEPLOY.md): external access is protected by
Cloudflare Access, the LAN is open. ``settings.password_hash`` was never written and is removed. Batch mode
(table copy) because SQLite cannot drop columns with ALTER TABLE on every version.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('settings', schema=None) as batch_op:
        batch_op.drop_column('password_hash')


def downgrade() -> None:
    with op.batch_alter_table('settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('password_hash', sa.String(length=255), nullable=True))
