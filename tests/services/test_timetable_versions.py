"""services/timetable.py: timetable versions and slots (Konzept §7.2)."""

from __future__ import annotations

from datetime import date, datetime, time

import pytest
from sqlalchemy import select

from app.db.models import Category, Settings, TimeEntry, Timetable, TimetableSlot
from app.services import timetable as tts
from app.util import local_to_utc


def cat_id(db, code: str) -> int:
    return db.scalar(select(Category.id).where(Category.code == code))


def mk(db, valid_from=date(2026, 9, 15), valid_to=None, slots=None, **kw) -> Timetable:
    return tts.create_timetable(db, valid_from=valid_from, valid_to=valid_to, slots=slots, **kw)


# --------------------------------------------------------------------------- versions


def test_create_version_with_default_label_and_slots_from_zeitraster(db):
    tt = mk(db, slots=[
        {"weekday": 1, "slot_no": 1, "klasse": "3b", "fach": "Deutsch"},
        {"weekday": 1, "slot_no": 3, "kind": "aufsicht"},
        {"weekday": 2, "slot_no": 2, "kind": "sprechstunde"},
        {"weekday": 3, "slot_no": 6, "kind": "konferenz_fix"},
        {"weekday": 4, "slot_no": 4, "kind": "frei"},
    ])
    assert tt.label == "Stundenplan ab 15.09.2026"
    slots = {(s.weekday, s.slot_no): s for s in tt.slots}
    assert slots[(1, 1)].start_time == time(8, 0) and slots[(1, 1)].end_time == time(8, 45)
    assert slots[(1, 3)].start_time == time(9, 50)  # seed zeitraster: 3. Stunde 09:50–10:35
    assert slots[(1, 1)].category_id == cat_id(db, "U")
    assert slots[(1, 3)].category_id == cat_id(db, "AV")
    assert slots[(2, 2)].category_id == cat_id(db, "E")
    assert slots[(3, 6)].category_id == cat_id(db, "KO")
    assert slots[(4, 4)].category_id is None
    assert slots[(1, 1)].klasse == "3b" and slots[(1, 1)].fach == "Deutsch"


def test_explicit_times_and_subcategory_are_kept(db):
    sub = db.scalar(select(Category).where(Category.code == "AV.1"))
    tt = mk(db, slots=[{"weekday": 5, "slot_no": 0, "kind": "aufsicht", "start_time": "07:45",
                        "end_time": "08:00", "category_id": sub.id}])
    (slot,) = tt.slots
    assert (slot.start_time, slot.end_time) == (time(7, 45), time(8, 0))
    assert slot.category_id == sub.id


def test_slot_without_zeitraster_needs_times(db):
    with pytest.raises(tts.TimetableError, match="kein Zeitraster"):
        mk(db, slots=[{"weekday": 1, "slot_no": 9}])
    db.rollback()  # nothing was committed
    assert db.scalars(select(Timetable)).all() == []


@pytest.mark.parametrize(
    ("slot", "message"),
    [
        ({"weekday": 6, "slot_no": 1}, "Wochentag"),
        ({"weekday": 0, "slot_no": 1}, "Wochentag"),
        ({"slot_no": 1}, "Wochentag fehlt"),
        ({"weekday": 1, "slot_no": 100}, "Stundennummer"),
        ({"weekday": 1, "slot_no": 1, "kind": "pause"}, "Unbekannte Art"),
        ({"weekday": 1, "slot_no": 1, "klasse": "9z"}, "Unbekannte Klasse"),
        ({"weekday": 1, "slot_no": 1, "start_time": "09:00", "end_time": "08:00"}, "Ende muss nach"),
        ({"weekday": 1, "slot_no": 1, "start_time": "25:00", "end_time": "26:00"}, "HH:MM"),
        ({"weekday": 1, "slot_no": 1, "category_id": 99999}, "Unbekannte Kategorie"),
        ({"weekday": 1, "slot_no": 1, "foo": 1}, "Unbekannte Felder"),
    ],
)
def test_slot_validation(db, slot, message):
    tt = mk(db)
    with pytest.raises(tts.TimetableError, match=message):
        tts.add_slot(db, tt.id, slot)


def test_pseudo_category_is_rejected(db):
    tt = mk(db)
    with pytest.raises(tts.TimetableError, match="nicht geeignet"):
        tts.add_slot(db, tt.id, {"weekday": 1, "slot_no": 1, "category_id": cat_id(db, "KR")})


def test_duplicate_slot_number_and_time_overlap_are_conflicts(db):
    tt = mk(db, slots=[{"weekday": 1, "slot_no": 1}])
    with pytest.raises(tts.TimetableConflict, match="doppelt"):
        tts.add_slot(db, tt.id, {"weekday": 1, "slot_no": 1, "kind": "aufsicht"})
    with pytest.raises(tts.TimetableConflict, match="überschneiden"):
        tts.add_slot(db, tt.id, {"weekday": 1, "slot_no": 7, "start_time": "08:30", "end_time": "09:00"})
    # same slot on another weekday is fine; a free slot may overlap
    tts.add_slot(db, tt.id, {"weekday": 2, "slot_no": 1})
    tts.add_slot(db, tt.id, {"weekday": 1, "slot_no": 7, "kind": "frei", "start_time": "08:30",
                             "end_time": "09:00"})
    assert len(get(db, tt.id).slots) == 3


def get(db, timetable_id: str) -> Timetable:
    db.expire_all()
    return tts.get_timetable(db, timetable_id)


def test_versions_must_not_overlap(db):
    mk(db, valid_from=date(2026, 9, 15), valid_to=date(2027, 2, 12))
    with pytest.raises(tts.TimetableConflict, match="überschneidet"):
        mk(db, valid_from=date(2027, 2, 1))
    with pytest.raises(tts.TimetableConflict):
        mk(db, valid_from=date(2026, 9, 1), valid_to=date(2026, 9, 15))
    mk(db, valid_from=date(2027, 2, 13))  # adjacent is fine
    with pytest.raises(tts.TimetableError, match="vor"):
        mk(db, valid_from=date(2028, 1, 10), valid_to=date(2028, 1, 1))


def test_close_previous_ends_the_open_version(db):
    first = mk(db, valid_from=date(2026, 9, 15))
    second = mk(db, valid_from=date(2027, 2, 15), close_previous=True)
    assert first.valid_to == date(2027, 2, 14)
    assert second.valid_to is None
    # a version starting before the new one's start cannot be "closed" if it starts later
    with pytest.raises(tts.TimetableConflict):
        mk(db, valid_from=date(2027, 1, 1), close_previous=True)


def test_timetable_for_date(db):
    a = mk(db, valid_from=date(2026, 9, 15), valid_to=date(2027, 2, 12))
    b = mk(db, valid_from=date(2027, 2, 15))
    assert tts.timetable_for_date(db, date(2026, 9, 14)) is None
    assert tts.timetable_for_date(db, date(2026, 9, 15)).id == a.id
    assert tts.timetable_for_date(db, date(2027, 2, 12)).id == a.id
    assert tts.timetable_for_date(db, date(2027, 2, 13)) is None
    assert tts.timetable_for_date(db, date(2030, 1, 1)).id == b.id
    assert tts.resolve_timetable(db, "current", today=date(2027, 3, 1)).id == b.id
    with pytest.raises(tts.TimetableNotFound):
        tts.resolve_timetable(db, "current", today=date(2026, 1, 1))
    with pytest.raises(tts.TimetableNotFound):
        tts.get_timetable(db, "nope")


def test_update_version(db):
    tt = mk(db, valid_from=date(2026, 9, 15))
    tts.update_timetable(db, tt.id, {"label": "1. Halbjahr", "valid_to": date(2027, 2, 12)})
    assert tt.label == "1. Halbjahr" and tt.valid_to == date(2027, 2, 12)
    other = mk(db, valid_from=date(2027, 2, 15))
    with pytest.raises(tts.TimetableConflict):
        tts.update_timetable(db, other.id, {"valid_from": date(2027, 2, 1)})
    db.rollback()
    tts.update_timetable(db, tt.id, {"label": ""})
    assert tt.label == "Stundenplan ab 15.09.2026"
    with pytest.raises(tts.TimetableError, match="Unbekannte Felder"):
        tts.update_timetable(db, tt.id, {"slots": []})


def test_update_version_open_end_overlapping_next_is_conflict(db):
    tt = mk(db, valid_from=date(2026, 9, 15), valid_to=date(2027, 2, 12))
    mk(db, valid_from=date(2027, 2, 15))
    with pytest.raises(tts.TimetableConflict):
        tts.update_timetable(db, tt.id, {"valid_to": None})


def test_copy_version_for_halbjahreswechsel(db):
    src = mk(db, slots=[
        {"weekday": 1, "slot_no": 1, "klasse": "3b", "fach": "Deutsch"},
        {"weekday": 2, "slot_no": 0, "kind": "aufsicht", "start_time": "07:45", "end_time": "08:00"},
    ])
    copy = tts.copy_timetable(db, src.id, valid_from=date(2027, 2, 15), label="2. Halbjahr")
    assert src.valid_to == date(2027, 2, 14)
    assert copy.label == "2. Halbjahr" and copy.valid_from == date(2027, 2, 15)
    a = sorted((s.weekday, s.slot_no, s.start_time, s.kind, s.klasse, s.fach, s.category_id) for s in src.slots)
    b = sorted((s.weekday, s.slot_no, s.start_time, s.kind, s.klasse, s.fach, s.category_id) for s in copy.slots)
    assert a == b
    assert {s.id for s in src.slots}.isdisjoint({s.id for s in copy.slots})


def test_copy_without_closing_source_conflicts(db):
    src = mk(db)
    with pytest.raises(tts.TimetableConflict):
        tts.copy_timetable(db, src.id, valid_from=date(2027, 2, 15), close_source=False)


# --------------------------------------------------------------------------- slots


def test_update_slot_takes_raster_times_for_new_number_and_default_category_for_new_kind(db):
    tt = mk(db, slots=[{"weekday": 1, "slot_no": 1, "klasse": "3b"}])
    slot = tt.slots[0]
    tts.update_slot(db, slot.id, {"slot_no": 2})
    assert (slot.start_time, slot.end_time) == (time(8, 45), time(9, 30))
    tts.update_slot(db, slot.id, {"kind": "aufsicht", "klasse": None})
    assert slot.category_id == cat_id(db, "AV") and slot.klasse is None
    tts.update_slot(db, slot.id, {"end_time": "09:40"})
    assert slot.end_time == time(9, 40) and slot.start_time == time(8, 45)
    tts.update_slot(db, slot.id, {"kind": "frei"})
    assert slot.category_id is None


def test_update_slot_checks_overlap_with_siblings(db):
    tt = mk(db, slots=[{"weekday": 1, "slot_no": 1}, {"weekday": 1, "slot_no": 2}])
    second = next(s for s in tt.slots if s.slot_no == 2)
    with pytest.raises(tts.TimetableConflict):
        tts.update_slot(db, second.id, {"start_time": "08:30"})
    with pytest.raises(tts.TimetableNotFound):
        tts.update_slot(db, "missing", {"slot_no": 3})


def test_set_slots_upserts_by_weekday_and_number(db):
    tt = mk(db, slots=[{"weekday": 1, "slot_no": 1, "klasse": "3b"}, {"weekday": 1, "slot_no": 2}])
    keep_id = next(s.id for s in tt.slots if s.slot_no == 1)
    drop_id = next(s.id for s in tt.slots if s.slot_no == 2)
    result = tts.set_slots(db, tt.id, [
        {"weekday": 1, "slot_no": 1, "klasse": "4a"},
        {"weekday": 3, "slot_no": 5, "fach": "Sport"},
    ])
    assert [(s.weekday, s.slot_no) for s in result] == [(1, 1), (3, 5)]
    assert result[0].id == keep_id and result[0].klasse == "4a"
    assert db.get(TimetableSlot, drop_id) is None
    with pytest.raises(tts.TimetableConflict):
        tts.set_slots(db, tt.id, [{"weekday": 1, "slot_no": 1}, {"weekday": 1, "slot_no": 1}])


def test_delete_slot(db):
    tt = mk(db, slots=[{"weekday": 1, "slot_no": 1}, {"weekday": 1, "slot_no": 2}])
    slot_id = tt.slots[0].id
    tts.delete_slot(db, slot_id)
    assert db.get(TimetableSlot, slot_id) is None
    assert len(get(db, tt.id).slots) == 1
    with pytest.raises(tts.TimetableNotFound):
        tts.delete_slot(db, slot_id)


def _suggest_and_confirm(db, d: date):
    tts.generate_suggestions(db, d, now=local_to_utc(d, time(0, 5)))
    return tts.confirm_day(db, d, now=local_to_utc(d, time(16)), today=d)


@pytest.fixture
def year(db):
    from pathlib import Path

    from app.services import calendar_service as cal

    period = cal.create_period(db, "2026/27")
    ics = Path(__file__).resolve().parents[2] / "examples" / "ferien_bayern_BEISPIEL.ics"
    cal.commit_ics(db, period, ics.read_bytes())
    return period


def test_delete_version_keeps_confirmed_entries_and_retires_suggestions(db, year):
    tt = mk(db, slots=[{"weekday": 1, "slot_no": 1}, {"weekday": 2, "slot_no": 1}])
    _suggest_and_confirm(db, date(2026, 10, 26))  # Monday: confirmed
    tts.generate_suggestions(db, date(2026, 10, 27), now=local_to_utc(date(2026, 10, 27), time(0, 5)))
    tts.delete_timetable(db, tt.id)
    assert db.get(Timetable, tt.id) is None
    rows = db.scalars(select(TimeEntry).where(TimeEntry.source == "stundenplan")).all()
    confirmed = [e for e in rows if e.day == date(2026, 10, 26)]
    tuesday = [e for e in rows if e.day == date(2026, 10, 27)]
    assert len(confirmed) == 1 and confirmed[0].status == "active" and confirmed[0].deleted_at is None
    assert confirmed[0].timetable_slot_id is None  # FK ON DELETE SET NULL
    assert len(tuesday) == 1 and tuesday[0].deleted_at is not None


def test_timetable_to_dict(db):
    tt = mk(db, slots=[{"weekday": 1, "slot_no": 1, "klasse": "3b", "fach": "Deutsch"},
                       {"weekday": 1, "slot_no": 3, "kind": "aufsicht"}])
    data = tts.timetable_to_dict(db, tt, today=date(2026, 10, 29))
    assert data["aktuell"] is True and data["slot_count"] == 2 and data["unterricht_slots"] == 1
    first = data["slots"][0]
    assert first["start_time"] == "08:00" and first["duration_hm"] == "0:45"
    assert first["category_code"] == "U" and first["label"] == "1. Stunde · 3b Deutsch"
    assert data["slots"][1]["label"] == "Aufsicht (Stunde 3)"
    assert "slots" not in tts.timetable_to_dict(db, tt, include_slots=False)


def test_klassen_list_empty_allows_any_klasse(db):
    db.get(Settings, 1).klassen = []
    db.commit()
    tt = mk(db, slots=[{"weekday": 1, "slot_no": 1, "klasse": "7c"}])
    assert tt.slots[0].klasse == "7c"


def test_parse_time_accepts_datetime_and_seconds():
    assert tts._parse_time("8:05", "x") == time(8, 5)
    assert tts._parse_time("08:05:30", "x") == time(8, 5, 30)
    assert tts._parse_time(datetime(2026, 10, 26, 7, 0), "x") == time(8, 0)  # naive UTC -> Berlin
    assert tts._parse_time(None, "x") is None
