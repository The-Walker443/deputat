"""Review fixes in services/entries.py: 'nachtraeglich' on day moves, hash history validity, date range."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.db.models import Category, MonthClose
from app.services import entries as svc

BERLIN = ZoneInfo("Europe/Berlin")


def L(y, m, d, h=0, mi=0, s=0) -> datetime:  # noqa: N802 - local wall-clock helper
    return datetime(y, m, d, h, mi, s, tzinfo=BERLIN)


def utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None)


def cat(db, code: str) -> int:
    return db.scalar(select(Category.id).where(Category.code == code))


def make(db, start, end, *, now, code="K"):
    return svc.create_entry(db, {"start_at": start, "end_at": end, "category_id": cat(db, code)},
                            source="manuell", now=now)


def close(db, ym: str, closed_at: datetime) -> MonthClose:
    for entry in svc.month_hash_entries(db, ym):
        entry.locked = True
    mc = MonthClose(year_month=ym, closed_at=closed_at, entries_hash_sha256=svc.month_entries_hash(db, ym),
                    soll_seconds=0, ist_seconds=0, saldo_seconds=0)
    db.add(mc)
    db.commit()
    return mc


# ------------------------------------------------------------------ nachtraeglich


def test_moving_entry_to_a_past_day_late_sets_nachtraeglich(db):
    (entry,) = make(db, L(2026, 10, 1, 9), L(2026, 10, 1, 11), now=L(2026, 10, 1, 12))
    assert "nachtraeglich" not in entry.flags
    (moved,) = svc.update_entry(db, entry.id, {"start_at": L(2026, 10, 19, 9), "end_at": L(2026, 10, 19, 11)},
                                now=L(2026, 10, 25, 12))
    assert moved.day == date(2026, 10, 19)
    assert "nachtraeglich" in moved.flags


def test_moving_entry_within_48h_stays_timely(db):
    (entry,) = make(db, L(2026, 10, 1, 9), L(2026, 10, 1, 11), now=L(2026, 10, 1, 12))
    (moved,) = svc.update_entry(db, entry.id, {"start_at": L(2026, 10, 2, 9), "end_at": L(2026, 10, 2, 11)},
                                now=L(2026, 10, 3, 12))
    assert "nachtraeglich" not in moved.flags


def test_changing_times_on_the_same_day_later_does_not_set_nachtraeglich(db):
    (entry,) = make(db, L(2026, 10, 1, 9), L(2026, 10, 1, 11), now=L(2026, 10, 1, 12))
    (same,) = svc.update_entry(db, entry.id, {"end_at": L(2026, 10, 1, 12)}, now=L(2026, 10, 20, 12))
    assert "nachtraeglich" not in same.flags


# ------------------------------------------------------------------ hash history


def test_hash_history_records_when_each_hash_became_valid(db):
    (entry,) = make(db, L(2026, 10, 12, 9), L(2026, 10, 12, 10), now=L(2026, 10, 12, 12))
    close(db, "2026-10", datetime(2026, 11, 1, 10))
    svc.update_entry(db, entry.id, {"note": "r1"}, reason="Korrektur 1", now=utc(L(2026, 11, 5, 12)))
    svc.update_entry(db, entry.id, {"note": "r2"}, reason="Korrektur 2", now=utc(L(2026, 11, 20, 12)))
    mc = db.get(MonthClose, "2026-10")
    first, second = mc.hash_history
    assert first["closed_at"] == "2026-11-01T10:00:00Z"
    assert second["closed_at"] == first["replaced_at"] == "2026-11-05T11:00:00Z"
    assert second["replaced_at"] == "2026-11-20T11:00:00Z"


# ------------------------------------------------------------------ plausible dates


@pytest.mark.parametrize(
    "data",
    [
        {"day": "9999-12-31", "duration_seconds": 60},
        {"start_at": "9999-12-31T23:00:00", "end_at": "9999-12-31T23:30:00"},
        {"start_at": "0001-01-01T00:10:00", "end_at": "0001-01-01T00:20:00"},
        {"day": date(1999, 12, 31), "duration_seconds": 60},
    ],
)
def test_implausible_dates_are_validation_errors(db, data):
    with pytest.raises(svc.EntryValidationError, match="zwischen 2000 und 2099"):
        svc.create_entry(db, {"category_id": cat(db, "K"), **data}, source="manuell",
                         now=datetime(2026, 10, 1, 12))


# ------------------------------------------------------------------ derived flags after calendar changes


@pytest.mark.usefixtures("no_ferien_catalog")  # builds its own ferien (no catalogue)
def test_derived_flags_follow_later_calendar_changes(db):
    from pathlib import Path

    from app.services import calendar_service as cal

    ics = Path(__file__).resolve().parents[2] / "examples" / "ferien_bayern_BEISPIEL.ics"
    period = cal.create_period(db, "2026/27")
    (summer,) = make(db, L(2026, 8, 20, 9), L(2026, 8, 20, 12), now=L(2026, 8, 20, 13))
    (autumn,) = make(db, L(2026, 11, 3, 9), L(2026, 11, 3, 12), now=L(2026, 11, 3, 13))
    assert "ferienarbeit" not in summer.flags
    cal.commit_ics(db, period, ics.read_bytes())  # ferien imported after the entries
    db.expire_all()
    assert "ferienarbeit" in svc.get_entry(db, summer.id).flags
    assert [e.id for e in svc.list_entries(db, flags=["ferienarbeit"])] == [summer.id, autumn.id]
    assert svc.revisions(db, summer.id) == []
    # A closed month keeps its flags (hash fields).
    autumn_ferien = next(f for f in cal.list_ferien(db, period) if f.start <= date(2026, 11, 3) <= f.end)
    close(db, "2026-11", datetime(2026, 12, 1, 10))
    cal.delete_ferien(db, autumn_ferien.id, reason="Test")
    db.expire_all()
    assert "ferienarbeit" in svc.get_entry(db, autumn.id).flags


def test_entry_to_dict_shows_new_subcategory_after_update(db):
    (entry,) = make(db, L(2026, 10, 1, 9), L(2026, 10, 1, 10), now=L(2026, 10, 1, 12), code="U")
    sub = db.scalars(select(Category).where(Category.parent_id == entry.category_id)).first()
    assert svc.entry_to_dict(entry)["subcategory_name"] is None
    (updated,) = svc.update_entry(db, entry.id, {"subcategory_id": sub.id}, now=L(2026, 10, 1, 13))
    assert svc.entry_to_dict(updated)["subcategory_name"] == sub.name
