"""category: add the main category AZ for day totals (screen "Erfassen")

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-02 12:00:00

Data migration only (no schema change). The "Erfassen" screen stores one duration-only entry per day
("Tagessumme") in the main category ``AZ – Arbeitszeit (pauschal, nicht aufgeschlüsselt)``
(``app.services.quick_entry``). ``app.db.seed`` adds missing catalogue rows on every start anyway; this
migration makes the category part of the schema history for existing databases, so a database is complete
right after ``alembic upgrade head`` (e.g. a restored backup) and a JSON backup taken afterwards carries it.

Fresh databases (empty catalogue) are left to the seed, which creates the whole catalogue in order.
Downgrade removes the category only while nothing references it (entries, timetable slots, paused timer).
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept literal on purpose (a migration must not change when app.db.seed changes); same values as
# app.db.seed.TAGESSUMME_* and the catalogue position after KR (sort 150).
CODE = "AZ"
NAME = "Arbeitszeit (pauschal, nicht aufgeschlüsselt)"
COLOR = "#57534E"
SORT = 150


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        "INSERT INTO category (code, name, parent_id, color, sort, is_unterricht, "
        "counts_as_mehrarbeit_candidate, active, is_pseudo, requires_note) "
        "SELECT ?, ?, NULL, ?, ?, 0, 0, 1, 0, 0 "
        "WHERE EXISTS (SELECT 1 FROM category) "
        "AND NOT EXISTS (SELECT 1 FROM category WHERE code = ?)",
        (CODE, NAME, COLOR, SORT, CODE),
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "DELETE FROM category WHERE code = ? AND parent_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM category c WHERE c.parent_id = category.id) "
        "AND NOT EXISTS (SELECT 1 FROM time_entry t WHERE t.category_id = category.id "
        "OR t.subcategory_id = category.id) "
        "AND NOT EXISTS (SELECT 1 FROM timetable_slot s WHERE s.category_id = category.id) "
        "AND NOT EXISTS (SELECT 1 FROM timer_state ts WHERE ts.paused_category_id = category.id)",
        (CODE,),
    )
