"""Service tests for app/services/entries.py (Konzept §4 invariants, §8.2/8.3, T13–T17)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.db.models import CalendarDay, Category, Holiday, MonthClose, Settings, TimeEntry, TimeEntryRevision
from app.services import entries as svc

BERLIN = ZoneInfo("Europe/Berlin")
H = 3600


def L(y, m, d, h=0, mi=0, s=0) -> datetime:  # noqa: N802 - local wall-clock helper
    return datetime(y, m, d, h, mi, s, tzinfo=BERLIN)


def utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None)


def cat(db, code: str) -> int:
    return db.scalar(select(Category.id).where(Category.code == code))


def make(db, start, end=None, *, code="K", source="manuell", now=None, **extra):
    data = {"start_at": start, "end_at": end, "category_id": cat(db, code), **extra}
    return svc.create_entry(db, data, source=source, now=now or (start + timedelta(hours=12)))


# --------------------------------------------------------------------------- creation basics


def test_create_bounded_entry_sets_duration_day_and_utc(db):
    (entry,) = make(db, L(2026, 11, 3, 13, 30), L(2026, 11, 3, 15, 10), klasse="3b", location="schule")
    assert entry.duration_seconds == H + 40 * 60
    assert entry.day == date(2026, 11, 3)
    assert entry.start_at == utc(L(2026, 11, 3, 13, 30))
    assert entry.start_at.tzinfo is None
    assert entry.has_bounds is True
    assert entry.flags == []
    assert entry.status == "active" and entry.source == "manuell"


def test_day_is_local_date_of_start(db):
    # 00:30 local on 4 Nov is still 3 Nov in UTC
    (entry,) = make(db, L(2026, 11, 4, 0, 30), L(2026, 11, 4, 1, 30))
    assert entry.start_at.date() == date(2026, 11, 3)
    assert entry.day == date(2026, 11, 4)


def test_start_plus_duration_gives_end(db):
    (entry,) = svc.create_entry(
        db, {"start_at": L(2026, 11, 3, 9), "duration_seconds": "1:30", "category_id": cat(db, "VN")},
        source="manuell", now=utc(L(2026, 11, 3, 12)),
    )
    assert entry.end_at == utc(L(2026, 11, 3, 10, 30))
    assert entry.has_bounds is True


def test_iso_strings_and_microseconds_are_normalised(db):
    (entry,) = svc.create_entry(
        db, {"start_at": "2026-11-03T09:00:00.123+01:00", "end_at": "2026-11-03T10:00:00.999+01:00",
             "category_id": cat(db, "VN")},
        source="manuell", now=utc(L(2026, 11, 3, 12)),
    )
    assert entry.start_at == datetime(2026, 11, 3, 8, 0)
    assert entry.duration_seconds == H


def test_subcategory_id_as_category_is_normalised(db):
    sub = db.scalar(select(Category).where(Category.code == "K.1"))
    (entry,) = make(db, L(2026, 11, 3, 9), L(2026, 11, 3, 10), category_id=sub.id)
    assert entry.category_id == cat(db, "K")
    assert entry.subcategory_id == sub.id


def test_mismatched_subcategory_rejected(db):
    sub = db.scalar(select(Category).where(Category.code == "K.1"))
    with pytest.raises(svc.EntryValidationError, match="Unterkategorie"):
        make(db, L(2026, 11, 3, 9), L(2026, 11, 3, 10), code="VN", subcategory_id=sub.id)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"end": "same"}, "Endzeit muss nach"),
        ({"code": "S"}, "Notiz Pflicht"),
        ({"klasse": "9z"}, "Unbekannte Klasse"),
        ({"location": "mond"}, "Ungültiger Ort"),
        ({"flags": ["erfunden"]}, "Unbekanntes Kennzeichen"),
        ({"status": "egal"}, "Ungültiger Status"),
    ],
)
def test_validation_errors(db, kwargs, message):
    start = L(2026, 11, 3, 9)
    end = start if kwargs.pop("end", None) == "same" else L(2026, 11, 3, 10)
    with pytest.raises(svc.EntryValidationError, match=message):
        make(db, start, end, **kwargs)


def test_unknown_field_and_category_rejected(db):
    with pytest.raises(svc.EntryValidationError, match="Unbekannte Felder"):
        svc.create_entry(db, {"start_at": L(2026, 11, 3, 9), "foo": 1, "category_id": 1}, source="manuell")
    with pytest.raises(svc.EntryValidationError, match="Unbekannte Kategorie"):
        svc.create_entry(db, {"start_at": L(2026, 11, 3, 9), "end_at": L(2026, 11, 3, 10), "category_id": 9999},
                         source="manuell")


def test_inactive_category_rejected(db):
    k = db.get(Category, cat(db, "K"))
    k.active = False
    db.commit()
    with pytest.raises(svc.EntryValidationError, match="deaktiviert"):
        make(db, L(2026, 11, 3, 9), L(2026, 11, 3, 10))


def test_sonstiges_with_note_and_free_klasse_when_list_empty(db):
    db.get(Settings, 1).klassen = []
    db.commit()
    (entry,) = make(db, L(2026, 11, 3, 9), L(2026, 11, 3, 10), code="S", note="  Inventur  ", klasse="7x")
    assert entry.note == "Inventur"
    assert entry.klasse == "7x"


def test_krank_pseudo_category_allowed(db):
    (entry,) = make(db, L(2026, 11, 3, 8), L(2026, 11, 3, 12), code="KR")
    assert entry.category.is_pseudo


def test_manual_entry_longer_than_24h_rejected(db):
    with pytest.raises(svc.EntryValidationError, match="24 Stunden"):
        make(db, L(2026, 11, 3, 8), L(2026, 11, 4, 9))


def test_manual_entry_without_end_or_duration_rejected(db):
    with pytest.raises(svc.EntryValidationError, match="Endzeit oder Dauer"):
        make(db, L(2026, 11, 3, 8), None)


# --------------------------------------------------------------------------- T13 midnight split


def test_t13_entry_over_midnight_is_split_and_sum_unchanged(db):
    start, end = L(2026, 11, 3, 22, 30), L(2026, 11, 4, 1, 15)
    parts = make(db, start, end, klasse="3b", now=utc(L(2026, 11, 4, 8)))
    assert len(parts) == 2
    a, b = parts
    assert (a.day, b.day) == (date(2026, 11, 3), date(2026, 11, 4))
    assert a.start_at == utc(start) and b.end_at == utc(end)
    assert a.end_at == b.start_at == utc(L(2026, 11, 4, 0, 0))
    assert (a.end_at - timedelta(seconds=1)).replace(tzinfo=UTC).astimezone(BERLIN).strftime("%H:%M:%S") == "23:59:59"
    assert a.duration_seconds + b.duration_seconds == 2 * H + 45 * 60
    assert a.klasse == b.klasse == "3b"

    # Parts are linked via revision 0 (creation note).
    for no, part in enumerate(parts, start=1):
        (rev,) = svc.revisions(db, part.id)
        assert rev.revision_no == 0
        assert rev.change_reason == svc.SPLIT_REASON
        assert rev.snapshot_json["split"]["parts"] == [a.id, b.id]
        assert rev.snapshot_json["split"]["part_no"] == no
        assert rev.snapshot_json["split"]["original_duration_seconds"] == 2 * H + 45 * 60
        assert svc.revision_kind(rev) == "erstellt"


def test_update_over_midnight_splits(db):
    (entry,) = make(db, L(2026, 11, 3, 20), L(2026, 11, 3, 22), now=utc(L(2026, 11, 3, 23)))
    result = svc.update_entry(db, entry.id, {"end_at": L(2026, 11, 4, 2)}, now=utc(L(2026, 11, 4, 8)))
    assert len(result) == 2
    assert result[0].id == entry.id
    assert result[0].duration_seconds == 4 * H
    assert result[1].day == date(2026, 11, 4) and result[1].duration_seconds == 2 * H
    assert [r.revision_no for r in svc.revisions(db, entry.id)] == [1]
    assert [r.revision_no for r in svc.revisions(db, result[1].id)] == [0]


def test_split_on_dst_end_day_keeps_exact_sum(db):
    # 24.10.2026 23:00 CEST -> 25.10.2026 03:00 CET = 5 real hours
    parts = make(db, L(2026, 10, 24, 23), L(2026, 10, 25, 3), now=utc(L(2026, 10, 25, 9)))
    assert [p.duration_seconds for p in parts] == [1 * H, 4 * H]


# --------------------------------------------------------------------------- T14 revisions


def test_t14_update_creates_revision_with_full_previous_state(db):
    (entry,) = make(db, L(2026, 11, 3, 13, 30), L(2026, 11, 3, 15, 10), klasse="3b", note="Proben")
    before = svc.entry_snapshot(entry)

    svc.update_entry(db, entry.id, {"end_at": L(2026, 11, 3, 15, 40)}, now=utc(L(2026, 11, 3, 18)))
    (rev1,) = svc.revisions(db, entry.id)
    assert rev1.revision_no == 1
    assert rev1.change_reason is None
    assert {k: v for k, v in rev1.snapshot_json.items() if not k.startswith("_")} == before
    assert rev1.snapshot_json["end_at"] == "2026-11-03T14:10:00Z"
    assert entry.duration_seconds == 2 * H + 10 * 60

    before2 = svc.entry_snapshot(entry)
    svc.update_entry(db, entry.id, {"note": "Proben 3b"}, reason="Tippfehler", now=utc(L(2026, 11, 3, 19)))
    revs = svc.revisions(db, entry.id)
    assert [r.revision_no for r in revs] == [1, 2]
    assert revs[1].change_reason == "Tippfehler"
    assert {k: v for k, v in revs[1].snapshot_json.items() if not k.startswith("_")} == before2
    assert svc.revision_kind(revs[1]) == "geaendert"


def test_noop_update_creates_no_revision(db):
    (entry,) = make(db, L(2026, 11, 3, 9), L(2026, 11, 3, 10), klasse="3b")
    svc.update_entry(db, entry.id, {"klasse": "3b", "start_at": L(2026, 11, 3, 9)})
    assert svc.revisions(db, entry.id) == []


def test_update_start_keeps_end_and_duration_sets_end(db):
    (entry,) = make(db, L(2026, 11, 3, 9), L(2026, 11, 3, 10))
    svc.update_entry(db, entry.id, {"start_at": L(2026, 11, 3, 8, 30)})
    assert entry.duration_seconds == 90 * 60
    svc.update_entry(db, entry.id, {"duration_seconds": 3600})
    assert entry.end_at == utc(L(2026, 11, 3, 9, 30))


def test_update_category_drops_old_subcategory(db):
    sub = db.scalar(select(Category).where(Category.code == "K.1"))
    (entry,) = make(db, L(2026, 11, 3, 9), L(2026, 11, 3, 10), subcategory_id=sub.id)
    svc.update_entry(db, entry.id, {"category_id": cat(db, "VN")})
    assert entry.category_id == cat(db, "VN") and entry.subcategory_id is None


def test_update_rejects_immutable_fields(db):
    (entry,) = make(db, L(2026, 11, 3, 9), L(2026, 11, 3, 10))
    with pytest.raises(svc.EntryValidationError):
        svc.update_entry(db, entry.id, {"source": "import"})
    with pytest.raises(svc.EntryNotFound):
        svc.update_entry(db, "nope", {"note": "x"})


def test_editing_timer_bounds_adds_korrigiert(db):
    (entry,) = make(db, L(2026, 11, 3, 9), L(2026, 11, 3, 10), source="timer")
    svc.update_entry(db, entry.id, {"note": "x"})
    assert "korrigiert" not in entry.flags
    svc.update_entry(db, entry.id, {"end_at": L(2026, 11, 3, 9, 45)})
    assert "korrigiert" in entry.flags
    svc.update_entry(db, entry.id, {"flags": []})  # sticky
    assert "korrigiert" in entry.flags


# --------------------------------------------------------------------------- T15 locked months


def _close_month(db, ym: str) -> MonthClose:
    for entry in svc.month_hash_entries(db, ym):
        entry.locked = True
    mc = MonthClose(year_month=ym, closed_at=datetime(2026, 11, 1, 10), entries_hash_sha256=svc.month_entries_hash(db, ym),
                    soll_seconds=0, ist_seconds=0, saldo_seconds=0)
    db.add(mc)
    db.commit()
    return mc


def test_t15_locked_month_requires_reason_and_keeps_old_hash(db):
    (entry,) = make(db, L(2026, 10, 12, 13, 30), L(2026, 10, 12, 15))
    make(db, L(2026, 10, 13, 9), L(2026, 10, 13, 10))
    mc = _close_month(db, "2026-10")
    old_hash = mc.entries_hash_sha256

    with pytest.raises(svc.EntryLocked, match="Monat abgeschlossen – Änderung nur mit Begründung"):
        svc.update_entry(db, entry.id, {"end_at": L(2026, 10, 12, 15, 30)}, now=utc(L(2026, 11, 2, 9)))
    db.rollback()
    assert svc.revisions(db, entry.id) == []
    assert mc.status == "abgeschlossen"

    svc.update_entry(db, entry.id, {"end_at": L(2026, 10, 12, 15, 30)}, reason="Konferenz vergessen",
                     now=utc(L(2026, 11, 2, 9)))
    (rev,) = svc.revisions(db, entry.id)
    assert rev.revision_no == 1 and rev.change_reason == "Konferenz vergessen"
    assert rev.snapshot_json["end_at"] == "2026-10-12T13:00:00Z"
    db.refresh(mc)
    assert mc.status == "nachtraeglich_korrigiert"
    assert mc.corrected_at == utc(L(2026, 11, 2, 9))
    assert mc.hash_history[0]["hash"] == old_hash
    assert mc.hash_history[0]["reason"] == "Konferenz vergessen"
    assert mc.entries_hash_sha256 != old_hash
    assert mc.entries_hash_sha256 == svc.month_entries_hash(db, "2026-10")
    assert entry.locked is True
    assert "korrigiert" in entry.flags


def test_t15_delete_and_create_in_closed_month(db):
    (entry,) = make(db, L(2026, 10, 12, 9), L(2026, 10, 12, 10))
    mc = _close_month(db, "2026-10")
    with pytest.raises(svc.EntryLocked):
        svc.delete_entry(db, entry.id)
    db.rollback()
    svc.delete_entry(db, entry.id, reason="Doppelt erfasst", now=utc(L(2026, 11, 2, 9)))
    db.refresh(mc)
    assert len(mc.hash_history) == 1 and mc.status == "nachtraeglich_korrigiert"

    with pytest.raises(svc.EntryLocked):
        make(db, L(2026, 10, 14, 9), L(2026, 10, 14, 10), now=utc(L(2026, 11, 2, 10)))
    db.rollback()
    (new,) = svc.create_entry(
        db, {"start_at": L(2026, 10, 14, 9), "end_at": L(2026, 10, 14, 10), "category_id": cat(db, "K")},
        source="manuell", reason="Nachtrag Elternabend", now=utc(L(2026, 11, 2, 10)),
    )
    assert new.locked is True
    (rev,) = svc.revisions(db, new.id)
    assert rev.revision_no == 0 and rev.change_reason == "Nachtrag Elternabend"
    db.refresh(mc)
    assert len(mc.hash_history) == 2
    assert mc.entries_hash_sha256 == svc.month_entries_hash(db, "2026-10")


def test_moving_entry_into_closed_month_needs_reason(db):
    (entry,) = make(db, L(2026, 11, 3, 9), L(2026, 11, 3, 10))
    _close_month(db, "2026-10")
    with pytest.raises(svc.EntryLocked):
        svc.update_entry(db, entry.id, {"start_at": L(2026, 10, 30, 9), "end_at": L(2026, 10, 30, 10)})
    db.rollback()
    svc.update_entry(db, entry.id, {"start_at": L(2026, 10, 30, 9), "end_at": L(2026, 10, 30, 10)}, reason="falscher Tag")
    assert entry.locked is True


def test_month_hash_is_deterministic_and_includes_soft_deleted(db):
    (a,) = make(db, L(2026, 10, 12, 9), L(2026, 10, 12, 10))
    make(db, L(2026, 11, 2, 9), L(2026, 11, 2, 10))  # other month
    h1 = svc.month_entries_hash(db, "2026-10")
    assert h1 == svc.month_entries_hash(db, "2026-10")
    assert [e.id for e in svc.month_hash_entries(db, "2026-10")] == [a.id]
    svc.delete_entry(db, a.id)
    assert [e.id for e in svc.month_hash_entries(db, "2026-10")] == [a.id]
    assert svc.month_entries_hash(db, "2026-10") != h1
    assert set(svc.entry_hash_dict(a)) == set(svc.HASH_FIELDS)


# --------------------------------------------------------------------------- T16 nachträglich


def test_t16_entry_created_three_days_later_is_nachtraeglich_and_sticky(db):
    (entry,) = make(db, L(2026, 10, 5, 14), L(2026, 10, 5, 16), now=utc(L(2026, 10, 8, 12)))
    assert "nachtraeglich" in entry.flags
    svc.update_entry(db, entry.id, {"flags": []}, now=utc(L(2026, 10, 9, 12)))
    assert "nachtraeglich" in entry.flags
    svc.update_entry(db, entry.id, {"flags": ["klassenfahrt"]}, now=utc(L(2026, 10, 9, 13)))
    assert set(entry.flags) == {"nachtraeglich", "klassenfahrt"}
    svc.update_entry(db, entry.id, {"start_at": L(2026, 10, 5, 13)}, now=utc(L(2026, 10, 9, 14)))
    assert "nachtraeglich" in entry.flags


def test_t16_timely_entry_not_nachtraeglich_but_moving_it_back_sets_flag(db):
    (entry,) = make(db, L(2026, 10, 5, 14), L(2026, 10, 5, 16), now=utc(L(2026, 10, 6, 9)))
    assert "nachtraeglich" not in entry.flags
    # Moving the entry to a day long before its creation makes it "nachträglich" (no gaming via edits).
    svc.update_entry(db, entry.id, {"start_at": L(2026, 9, 28, 14), "end_at": L(2026, 9, 28, 16)})
    assert "nachtraeglich" in entry.flags


# --------------------------------------------------------------------------- T17 without bounds


def test_t17_duration_only_entry(db):
    (entry,) = svc.create_entry(
        db, {"day": date(2026, 10, 7), "duration_seconds": "2,5", "has_bounds": False, "category_id": cat(db, "K")},
        source="import", now=utc(L(2026, 10, 7, 20)),
    )
    assert entry.duration_seconds == 2 * H + 30 * 60
    assert entry.has_bounds is False
    assert "ohne_zeitgrenzen" in entry.flags
    assert entry.day == date(2026, 10, 7)
    assert entry.start_at == utc(L(2026, 10, 7, 0, 0))
    assert entry.end_at == entry.start_at + timedelta(seconds=9000)


def test_duration_only_inferred_and_exempt_from_overlap(db):
    make(db, L(2026, 10, 7, 0, 30), L(2026, 10, 7, 3))
    (entry,) = svc.create_entry(db, {"day": "2026-10-07", "duration_seconds": 3600, "category_id": cat(db, "VN")},
                                source="manuell", now=utc(L(2026, 10, 7, 20)))
    assert entry.has_bounds is False
    # And a bounded entry over the unbounded one's nominal span is fine too.
    make(db, L(2026, 10, 7, 4), L(2026, 10, 7, 5))


def test_duration_only_validation(db):
    with pytest.raises(svc.EntryValidationError, match="24 Stunden"):
        svc.create_entry(db, {"day": date(2026, 10, 7), "duration_seconds": 25 * H, "category_id": cat(db, "K")},
                         source="manuell")
    with pytest.raises(svc.EntryValidationError, match="größer als 0"):
        svc.create_entry(db, {"day": date(2026, 10, 7), "duration_seconds": 0, "category_id": cat(db, "K")},
                         source="manuell")
    with pytest.raises(svc.EntryValidationError, match="Datum fehlt"):
        svc.create_entry(db, {"has_bounds": False, "duration_seconds": 60, "category_id": cat(db, "K")},
                         source="manuell")


def test_switch_bounded_to_unbounded_and_back(db):
    (entry,) = make(db, L(2026, 10, 7, 9), L(2026, 10, 7, 11))
    svc.update_entry(db, entry.id, {"has_bounds": False})
    assert entry.has_bounds is False and entry.duration_seconds == 2 * H and "ohne_zeitgrenzen" in entry.flags
    assert entry.start_at == utc(L(2026, 10, 7))
    svc.update_entry(db, entry.id, {"has_bounds": True, "start_at": L(2026, 10, 7, 14)})
    assert entry.has_bounds is True and entry.end_at == utc(L(2026, 10, 7, 16))
    assert "ohne_zeitgrenzen" not in entry.flags


# --------------------------------------------------------------------------- overlaps / running timer


def test_overlap_rejected_adjacent_allowed(db):
    make(db, L(2026, 11, 3, 10), L(2026, 11, 3, 11))
    with pytest.raises(svc.EntryConflict, match="Überschneidung mit einem bestehenden Eintrag am 03.11.2026, 10:00–11:00"):
        make(db, L(2026, 11, 3, 10, 30), L(2026, 11, 3, 11, 30))
    with pytest.raises(svc.EntryConflict):
        make(db, L(2026, 11, 3, 9), L(2026, 11, 3, 12))  # encloses
    make(db, L(2026, 11, 3, 11), L(2026, 11, 3, 12))
    make(db, L(2026, 11, 3, 9), L(2026, 11, 3, 10))


def test_overlap_exemptions(db):
    (base,) = make(db, L(2026, 11, 3, 10), L(2026, 11, 3, 11))
    make(db, L(2026, 11, 3, 10), L(2026, 11, 3, 11), source="import")  # imported: exempt
    make(db, L(2026, 11, 3, 10), L(2026, 11, 3, 11), status="vorschlag")  # suggestions do not count
    svc.delete_entry(db, base.id)
    make(db, L(2026, 11, 3, 10, 15), L(2026, 11, 3, 10, 45))  # deleted entries do not block
    with pytest.raises(svc.EntryConflict):
        svc.restore_entry(db, base.id)


def test_update_into_overlap_rejected(db):
    make(db, L(2026, 11, 3, 10), L(2026, 11, 3, 11))
    (other,) = make(db, L(2026, 11, 3, 12), L(2026, 11, 3, 13))
    with pytest.raises(svc.EntryConflict):
        svc.update_entry(db, other.id, {"start_at": L(2026, 11, 3, 10, 59)})
    db.rollback()
    assert svc.revisions(db, other.id) == []


def test_confirming_suggestion_checks_overlap(db):
    make(db, L(2026, 11, 3, 10), L(2026, 11, 3, 11))
    (sugg,) = make(db, L(2026, 11, 3, 10), L(2026, 11, 3, 10, 45), status="vorschlag", source="stundenplan")
    with pytest.raises(svc.EntryConflict):
        svc.update_entry(db, sugg.id, {"status": "active"})


def test_single_running_timer_and_running_blocks_later_entries(db):
    (running,) = make(db, L(2026, 11, 3, 10), None, source="timer", now=utc(L(2026, 11, 3, 10)))
    assert running.end_at is None and running.duration_seconds == 0
    assert svc.get_running_entry(db).id == running.id
    with pytest.raises(svc.EntryConflict, match="läuft bereits ein Timer"):
        make(db, L(2026, 11, 3, 11), None, source="timer", now=utc(L(2026, 11, 3, 11)))
    with pytest.raises(svc.EntryConflict, match="10:00–läuft"):
        make(db, L(2026, 11, 3, 12), L(2026, 11, 3, 13))
    make(db, L(2026, 11, 3, 8), L(2026, 11, 3, 9))  # before the timer: fine


def test_finish_running_entry_splits_without_revision_for_single_day(db):
    (running,) = make(db, L(2026, 11, 3, 10), None, source="timer", now=utc(L(2026, 11, 3, 10)))
    (done,) = svc.finish_running_entry(db, L(2026, 11, 3, 11), now=utc(L(2026, 11, 3, 11)))
    assert done.id == running.id and done.duration_seconds == H
    assert svc.revisions(db, done.id) == []
    with pytest.raises(svc.EntryConflict, match="Es läuft kein Timer"):
        svc.finish_running_entry(db, L(2026, 11, 3, 12))


# --------------------------------------------------------------------------- derived flags


def test_derived_flags_weekend_holiday_ferien(db):
    (sat,) = make(db, L(2026, 10, 31, 9), L(2026, 10, 31, 10))
    assert "wochenende" in sat.flags

    db.add(Holiday(date=date(2026, 12, 8), name="Mariä Empfängnis", scope="gesetzlich"))
    db.add(CalendarDay(date=date(2026, 11, 4), weekday=3, day_type="ferientag", soll_seconds=28800))
    db.add(CalendarDay(date=date(2026, 11, 5), weekday=4, day_type="feiertag", soll_seconds=0))
    db.commit()
    (hol,) = make(db, L(2026, 12, 8, 9), L(2026, 12, 8, 10))
    (fer,) = make(db, L(2026, 11, 4, 9), L(2026, 11, 4, 10))
    (fei,) = make(db, L(2026, 11, 5, 9), L(2026, 11, 5, 10), flags=["feiertag", "vertretung_angeordnet"])
    (normal,) = make(db, L(2026, 11, 6, 9), L(2026, 11, 6, 10), flags=["ferienarbeit"])  # derived input ignored
    assert hol.flags == ["feiertag"]
    assert fer.flags == ["ferienarbeit"]
    assert fei.flags == ["feiertag", "vertretung_angeordnet"]
    assert normal.flags == []


# --------------------------------------------------------------------------- delete / restore / list


def test_soft_delete_and_restore(db):
    (entry,) = make(db, L(2026, 11, 3, 9), L(2026, 11, 3, 10))
    svc.delete_entry(db, entry.id, reason="falsch", now=utc(L(2026, 11, 3, 12)))
    assert entry.deleted_at == utc(L(2026, 11, 3, 12))
    assert db.get(TimeEntry, entry.id) is not None  # never hard-deleted
    (rev,) = svc.revisions(db, entry.id)
    assert rev.revision_no == 1 and rev.snapshot_json["deleted_at"] is None
    assert svc.revision_kind(rev) == "geloescht"
    with pytest.raises(svc.EntryConflict):
        svc.delete_entry(db, entry.id)
    with pytest.raises(svc.EntryConflict, match="Gelöschte"):
        svc.update_entry(db, entry.id, {"note": "x"})
    with pytest.raises(svc.EntryNotFound):
        svc.get_entry(db, entry.id, include_deleted=False)

    svc.restore_entry(db, entry.id)
    assert entry.deleted_at is None
    revs = svc.revisions(db, entry.id)
    assert [svc.revision_kind(r) for r in revs] == ["geloescht", "wiederhergestellt"]
    with pytest.raises(svc.EntryConflict):
        svc.restore_entry(db, entry.id)


def test_list_entries_filters(db):
    (a,) = make(db, L(2026, 11, 2, 9), L(2026, 11, 2, 10), code="K", flags=["klassenfahrt"])
    (b,) = make(db, L(2026, 11, 3, 9), L(2026, 11, 3, 10), code="VN")
    (c,) = make(db, L(2026, 11, 4, 9), L(2026, 11, 4, 10), code="K", source="import")
    (d,) = make(db, L(2026, 11, 5, 9), L(2026, 11, 5, 10), code="K", status="vorschlag")
    svc.delete_entry(db, c.id)

    ids = lambda rows: [r.id for r in rows]  # noqa: E731
    assert ids(svc.list_entries(db)) == [a.id, b.id, d.id]
    assert ids(svc.list_entries(db, include_deleted=True)) == [a.id, b.id, c.id, d.id]
    assert ids(svc.list_entries(db, date_from=date(2026, 11, 3), date_to=date(2026, 11, 4))) == [b.id]
    assert ids(svc.list_entries(db, category_id=cat(db, "K"))) == [a.id, d.id]
    assert ids(svc.list_entries(db, source="import", include_deleted=True)) == [c.id]
    assert ids(svc.list_entries(db, status="active")) == [a.id, b.id]
    assert ids(svc.list_entries(db, flags=["klassenfahrt"])) == [a.id]
    assert ids(svc.list_entries(db, limit=1, offset=1)) == [b.id]
    sub = db.scalar(select(Category).where(Category.code == "K.2"))
    (e,) = make(db, L(2026, 11, 6, 9), L(2026, 11, 6, 10), subcategory_id=sub.id)
    assert ids(svc.list_entries(db, category_id=sub.id)) == [e.id]


def test_entry_to_dict_shape(db):
    (entry,) = make(db, L(2026, 11, 3, 9), L(2026, 11, 3, 10, 30), klasse="3b")
    data = svc.entry_to_dict(entry)
    assert data["start_at"] == "2026-11-03T08:00:00Z"
    assert data["start_local"] == "2026-11-03T09:00:00+01:00"
    assert data["duration_hm"] == "1:30"
    assert data["category_code"] == "K"
    assert data["running"] is False


def test_revisions_unknown_entry(db):
    with pytest.raises(svc.EntryNotFound):
        svc.revisions(db, "missing")
    assert db.scalar(select(TimeEntryRevision.id)) is None
