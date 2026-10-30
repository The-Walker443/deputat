"""Review fix: deleting timetable slots never rewrites entries silently through the foreign key."""

from __future__ import annotations

from datetime import date, datetime, time

import pytest
from sqlalchemy import select

from app.db.models import Category, MonthClose, TimeEntry
from app.services import entries as entries_svc
from app.services import timetable as tts
from app.util import local_to_utc


def cat_id(db, code: str) -> int:
    return db.scalar(select(Category.id).where(Category.code == code))


@pytest.fixture
def linked(db):
    tt = tts.create_timetable(db, valid_from=date(2026, 9, 14), slots=[
        {"weekday": 1, "slot_no": 1, "klasse": "3b", "fach": "Deutsch"},
        {"weekday": 2, "slot_no": 1, "klasse": "3b", "fach": "Mathe"},
    ])
    slot = next(s for s in tt.slots if s.weekday == 1)
    start = local_to_utc(date(2026, 10, 12), time(8))
    (entry,) = entries_svc.create_entry(
        db, {"start_at": start, "end_at": local_to_utc(date(2026, 10, 12), time(8, 45)), "category_id": cat_id(db, "U"),
             "timetable_slot_id": slot.id},
        source="stundenplan", now=datetime(2026, 10, 12, 12),
    )
    return tt, slot, entry


def test_deleting_slot_unlinks_entry_with_revision(db, linked):
    _tt, slot, entry = linked
    tts.delete_slot(db, slot.id, now=datetime(2026, 10, 20, 12))
    db.expire_all()
    assert db.get(TimeEntry, entry.id).timetable_slot_id is None
    (rev,) = entries_svc.revisions(db, entry.id)
    assert rev.change_reason == tts.UNLINK_REASON
    assert rev.snapshot_json["timetable_slot_id"] == slot.id


def test_deleting_slot_of_closed_month_is_refused(db, linked):
    tt, slot, entry = linked
    for e in entries_svc.month_hash_entries(db, "2026-10"):
        e.locked = True
    db.add(MonthClose(year_month="2026-10", closed_at=datetime(2026, 11, 1, 10),
                      entries_hash_sha256=entries_svc.month_entries_hash(db, "2026-10"),
                      soll_seconds=0, ist_seconds=0, saldo_seconds=0))
    db.commit()
    with pytest.raises(tts.TimetableConflict, match="gültig bis"):
        tts.delete_slot(db, slot.id)
    with pytest.raises(tts.TimetableConflict):
        tts.delete_timetable(db, tt.id)
    with pytest.raises(tts.TimetableConflict):
        tts.set_slots(db, tt.id, [{"weekday": 2, "slot_no": 1, "klasse": "3b", "fach": "Mathe"}])
    db.rollback()
    db.expire_all()
    assert db.get(TimeEntry, entry.id).timetable_slot_id == slot.id
    assert entries_svc.revisions(db, entry.id) == []
