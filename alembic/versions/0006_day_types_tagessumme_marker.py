"""day types on "Erfassen": time_entry.tagessumme marker, day_override.erfassung_typ

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-15 12:00:00

The "Erfassen" screen gets a day type per row (Unterricht, Krank, Fortbildung, Klassenfahrt,
Schulveranstaltung). The type sets the category of the day's Tagessumme (AZ, F or SV), so the Tagessumme can
no longer be recognised by its category AZ:

* ``time_entry.tagessumme`` (bool, default 0) marks the Tagessumme entry explicitly. It is a column and not an
  entry flag on purpose: flags are hash fields, and marking the existing entries here must not change the
  Prüf-Hash of closed months.
* ``day_override.erfassung_typ`` ('klassenfahrt' | 'schulveranstaltung' | NULL) keeps a type that is no
  calendar absence (Krank and Fortbildung are stored as ``absence_type`` as before), so a type can be chosen
  before any hours are entered. ``erfassung_typ_at`` is the device time of the last type change (offline
  queue: the last *input* wins).

Data: every existing Tagessumme is marked – per day the newest entry that met the old criteria (main category
AZ, no subcategory, without bounds, source 'manuell', status 'active', not deleted, finished). Further old
candidates of the same day already counted as individual entries and stay unmarked.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept literal on purpose (a migration must not change when the app changes); the same statement is used for
# JSON backups taken before 0006 (app.services.quick_entry.LEGACY_TAGESSUMME_SQL).
_OLD_CRITERIA = (
    "{t}.category_id = (SELECT id FROM category WHERE code = 'AZ' AND parent_id IS NULL) "
    "AND {t}.subcategory_id IS NULL AND {t}.has_bounds = 0 AND {t}.source = 'manuell' "
    "AND {t}.status = 'active' AND {t}.deleted_at IS NULL AND {t}.end_at IS NOT NULL"
)
MARK_SQL = (
    "UPDATE time_entry SET tagessumme = 1 WHERE id IN ("
    "SELECT t.id FROM time_entry t WHERE " + _OLD_CRITERIA.format(t="t") + " "
    "AND NOT EXISTS (SELECT 1 FROM time_entry n WHERE n.day = t.day AND " + _OLD_CRITERIA.format(t="n") + " "
    "AND (n.created_at > t.created_at OR (n.created_at = t.created_at AND n.id > t.id))))"
)


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Plain ALTER TABLE ADD/DROP COLUMN, no batch table copy: a copy drops and renames time_entry, which fails
    # with foreign_keys=ON as soon as time_entry_revision rows reference it. A first release of this migration
    # did exactly that and could leave its copy behind – remove it.
    bind = op.get_bind()
    bind.exec_driver_sql("DROP TABLE IF EXISTS _alembic_tmp_time_entry")
    bind.exec_driver_sql("DROP TABLE IF EXISTS _alembic_tmp_day_override")
    if "tagessumme" not in _columns("time_entry"):
        op.add_column('time_entry', sa.Column('tagessumme', sa.Boolean(), nullable=False, server_default=sa.text('0')))
    existing = _columns("day_override")
    if "erfassung_typ" not in existing:
        op.add_column('day_override', sa.Column('erfassung_typ', sa.String(length=24), nullable=True))
    if "erfassung_typ_at" not in existing:
        op.add_column('day_override', sa.Column('erfassung_typ_at', sa.DateTime(), nullable=True))
    bind.exec_driver_sql(MARK_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    # Rows that only carried an "Erfassen" type would become empty overrides.
    bind.exec_driver_sql(
        "DELETE FROM day_override WHERE absence_type IS NULL AND day_type IS NULL AND urlaub_manuell = 0 "
        "AND note IS NULL"
    )
    bind.exec_driver_sql("ALTER TABLE day_override DROP COLUMN erfassung_typ_at")
    bind.exec_driver_sql("ALTER TABLE day_override DROP COLUMN erfassung_typ")
    bind.exec_driver_sql("ALTER TABLE time_entry DROP COLUMN tagessumme")
