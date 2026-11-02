"""services/quick_entry.py: day totals ("Erfassen") on top of the evidence model of services/entries.py."""

from __future__ import annotations

from datetime import date, time as dtime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Category, MonthClose, Settings, TimeEntry, TimeEntryRevision
from app.services import calendar_service as cal
from app.services import entries as entries_svc
from app.services import quick_entry as svc
from app.util import local_midnight_utc, local_to_utc

H = 3600
MON = date(2026, 11, 2)  # KW 45
TUE = MON + timedelta(days=1)
NOW = local_to_utc(TUE, dtime(20, 0))  # evening of the next day: nothing is "nachträglich"


def cat_id(db, code: str) -> int:
    return db.scalar(select(Category.id).where(Category.code == code, Category.parent_id.is_(None)))


def bounded(db, day: date, start: dtime, hours: float, code: str = "VN", source: str = "manuell") -> TimeEntry:
    a = local_to_utc(day, start)
    (entry,) = entries_svc.create_entry(
        db, {"start_at": a, "end_at": a + timedelta(seconds=int(hours * H)), "category_id": cat_id(db, code)},
        source=source, now=NOW,
    )
    return entry


def tagessummen(db, day: date, *, include_deleted: bool = False) -> list[TimeEntry]:
    az = cat_id(db, "AZ")
    q = select(TimeEntry).where(TimeEntry.day == day, TimeEntry.category_id == az)
    if not include_deleted:
        q = q.where(TimeEntry.deleted_at.is_(None))
    return list(db.scalars(q))


def revisions(db, entry_id: str) -> list[TimeEntryRevision]:
    return list(db.scalars(select(TimeEntryRevision).where(TimeEntryRevision.entry_id == entry_id)))


# --------------------------------------------------------------------------- parsing


@pytest.mark.parametrize(("text", "seconds"), [
    ("7,5", 7 * H + 30 * 60), ("7.5", 7 * H + 30 * 60), ("7:30", 7 * H + 30 * 60), ("7", 7 * H), ("0", 0),
    ("", 0), ("  ", 0), (None, 0), ("7h30", 7 * H + 30 * 60), ("8,25 h", 8 * H + 15 * 60), ("24", 24 * H),
    ("7,333", 7 * H + 20 * 60), (",5", 30 * 60), ("450 min", 7 * H + 30 * 60),
])
def test_parse_total_accepts_common_inputs(text, seconds):
    assert svc.parse_total(text) == seconds


@pytest.mark.parametrize("text", ["abc", "25", "24,1", "-1", "7:75", "7,5,5", "1e3", True])
def test_parse_total_rejects_invalid_input(text):
    with pytest.raises(entries_svc.EntryValidationError):
        svc.parse_total(text)


# --------------------------------------------------------------------------- set / overwrite / clear


def test_set_creates_one_duration_only_entry(db):
    result = svc.set_day_total(db, MON, 8 * H, now=NOW, today=TUE)
    (entry,) = tagessummen(db, MON)
    assert entry.duration_seconds == 8 * H
    assert entry.has_bounds is False and entry.source == "manuell" and entry.status == "active"
    assert "ohne_zeitgrenzen" in entry.flags and "nachtraeglich" not in entry.flags
    assert entry.subcategory_id is None
    assert entry.start_at == local_midnight_utc(MON)  # duration-only entries start at local midnight
    assert result["action"] == "angelegt"
    assert result["day"]["ist"] == 8 * H and result["day"]["tagessumme_seconds"] == 8 * H
    assert result["day"]["has_detail_entries"] is False


def test_overwrite_updates_the_same_entry_with_revision(db):
    svc.set_day_total(db, MON, 8 * H, now=NOW, today=TUE)
    (first,) = tagessummen(db, MON)
    result = svc.set_day_total(db, MON, 9 * H + 15 * 60, now=NOW + timedelta(minutes=5), today=TUE)
    (entry,) = tagessummen(db, MON)
    assert entry.id == first.id and entry.duration_seconds == 9 * H + 15 * 60
    revs = revisions(db, entry.id)
    assert len(revs) == 1 and revs[0].snapshot_json["duration_seconds"] == 8 * H
    assert result["action"] == "geaendert" and result["day"]["ist"] == 9 * H + 15 * 60


def test_zero_soft_deletes_and_clearing_twice_is_a_noop(db):
    svc.set_day_total(db, MON, 6 * H, now=NOW, today=TUE)
    (entry,) = tagessummen(db, MON)
    result = svc.set_day_total(db, MON, 0, now=NOW, today=TUE)
    assert result["action"] == "geloescht" and result["day"]["ist"] == 0
    assert tagessummen(db, MON) == []
    db.refresh(entry)
    assert entry.deleted_at is not None  # soft delete with revision, nothing hard-deleted
    assert [r.snapshot_json.get("_action") for r in revisions(db, entry.id)] == ["delete"]
    assert svc.set_day_total(db, MON, 0, now=NOW, today=TUE)["action"] == "unveraendert"
    # a new value afterwards creates a fresh entry
    svc.set_day_total(db, MON, 2 * H, now=NOW, today=TUE)
    assert [e.duration_seconds for e in tagessummen(db, MON)] == [2 * H]


def test_repeat_is_idempotent(db):
    svc.set_day_total(db, MON, 7 * H, now=NOW, today=TUE, client_action_id="a1")
    (entry,) = tagessummen(db, MON)
    again = svc.set_day_total(db, MON, 7 * H, now=NOW + timedelta(hours=1), today=TUE, client_action_id="a1")
    assert again["action"] == "unveraendert" and again["client_action_id"] == "a1"
    assert [e.id for e in tagessummen(db, MON)] == [entry.id]
    assert revisions(db, entry.id) == []


# --------------------------------------------------------------------------- mixed with detail entries


def test_total_is_split_into_detail_and_tagessumme(db):
    bounded(db, MON, dtime(8), 5, code="U", source="timer")
    bounded(db, MON, dtime(15), 1.5, code="K")
    state = svc.day_state(db, MON)
    assert state.total == 6.5 * H and state.detail_count == 2 and state.tagessumme is None

    result = svc.set_day_total(db, MON, 9 * H, now=NOW, today=TUE)
    (entry,) = tagessummen(db, MON)
    assert entry.duration_seconds == 2.5 * H  # 9 h − 6:30 recorded individually
    day = result["day"]
    assert day["ist"] == 9 * H and day["detail_seconds"] == 6.5 * H and day["has_detail_entries"]

    # equal to the detail sum -> the Tagessumme disappears, the detail entries stay untouched
    svc.set_day_total(db, MON, int(6.5 * H), now=NOW, today=TUE)
    assert tagessummen(db, MON) == []
    assert svc.day_state(db, MON).total == 6.5 * H


def test_total_below_detail_is_rejected(db):
    bounded(db, MON, dtime(8), 5, code="U")
    with pytest.raises(svc.DayTotalBelowDetail) as err:
        svc.set_day_total(db, MON, 4 * H, now=NOW, today=TUE)
    assert err.value.status_code == 409 and err.value.detail_seconds == 5 * H
    assert "bereits 5:00 h einzeln erfasst" in err.value.message
    assert tagessummen(db, MON) == []


def test_running_timer_is_not_part_of_the_total(db):
    """A running timer is shown, but not part of the field: the stored value must not depend on the moment
    the request reaches the server (queue replay, second tab)."""
    from app.services import timer as timer_svc

    a = local_to_utc(TUE, dtime(8))
    timer_svc.start(db, cat_id(db, "U"), started_at=a, now=a)
    t1 = a + timedelta(hours=2)
    state = svc.day_state(db, TUE, now=t1)
    assert state.running and state.running_seconds == 2 * H and state.detail_seconds == 0 and state.total == 0
    r1 = svc.set_day_total(db, TUE, 8 * H, now=t1, today=TUE)
    assert r1["action"] == "angelegt" and r1["day"]["running"] and r1["day"]["running_seconds"] == 2 * H
    # the same value again an hour later (lost response, second tab): no change, no revision
    r2 = svc.set_day_total(db, TUE, 8 * H, now=t1 + timedelta(hours=1), today=TUE)
    (entry,) = tagessummen(db, TUE)
    assert r2["action"] == "unveraendert" and entry.duration_seconds == 8 * H and revisions(db, entry.id) == []
    # after the stop the timer entry counts on top – and the row asks once instead of rewriting anything
    stop = local_to_utc(TUE, dtime(16))
    timer_svc.stop(db, stopped_at=stop, now=stop)
    day = svc.week_view(db, MON, today=TUE, now=stop + timedelta(minutes=5))["days"][1]
    assert day["ist"] == 16 * H and day["nachgetragen_seconds"] == 8 * H
    assert day["prompt"] == {"later": 8 * H, "keep": 8 * H, "total": 16 * H}


def test_pseudo_krank_entries_are_not_work(db):
    bounded(db, MON, dtime(8), 3, code="KR")
    assert svc.day_state(db, MON).total == 0
    svc.set_day_total(db, MON, 4 * H, now=NOW, today=TUE)
    assert [e.duration_seconds for e in tagessummen(db, MON)] == [4 * H]


def test_newest_matching_entry_is_the_tagessumme(db):
    az = cat_id(db, "AZ")
    for hours, minute in ((2, 0), (3, 1)):
        entries_svc.create_entry(db, {"has_bounds": False, "day": MON, "duration_seconds": hours * H,
                                      "category_id": az}, source="manuell", now=NOW + timedelta(minutes=minute))
    state = svc.day_state(db, MON)
    assert state.tagessumme_seconds == 3 * H and state.detail_seconds == 2 * H and state.total == 5 * H
    svc.set_day_total(db, MON, 6 * H, now=NOW, today=TUE)
    assert sorted(e.duration_seconds for e in tagessummen(db, MON)) == [2 * H, 4 * H]


# --------------------------------------------------------------------------- evidence rules


def test_late_entry_is_flagged_nachtraeglich(db):
    late = local_to_utc(MON + timedelta(days=3), dtime(0, 0, 1))  # Thursday 00:00:01 for Monday
    result = svc.set_day_total(db, MON, 8 * H, now=late, today=MON + timedelta(days=3))
    (entry,) = tagessummen(db, MON)
    assert "nachtraeglich" in entry.flags and result["day"]["nachtraeglich"] is True


def test_closed_month_needs_reason(db):
    oct_day = date(2026, 10, 14)
    svc.set_day_total(db, oct_day, 6 * H, now=local_to_utc(oct_day, dtime(20)), today=oct_day)
    db.add(MonthClose(year_month="2026-10", closed_at=NOW, entries_hash_sha256=entries_svc.month_entries_hash(db, "2026-10"),
                      soll_seconds=0, ist_seconds=0, saldo_seconds=0, hash_history=[]))
    db.commit()
    assert svc.week_view(db, svc.monday_of(oct_day), today=TUE)["days"][2]["locked"] is True
    with pytest.raises(entries_svc.EntryLocked):
        svc.set_day_total(db, oct_day, 7 * H, now=NOW, today=TUE)
    # same value: nothing to write, no lock error
    assert svc.set_day_total(db, oct_day, 6 * H, now=NOW, today=TUE)["action"] == "unveraendert"
    svc.set_day_total(db, oct_day, 7 * H, now=NOW, today=TUE, reason="Vergessene Konferenz nachgetragen")
    (entry,) = tagessummen(db, oct_day)
    assert entry.duration_seconds == 7 * H and "korrigiert" in entry.flags
    mc = db.get(MonthClose, "2026-10")
    assert mc.status == "nachtraeglich_korrigiert" and len(mc.hash_history) == 1


def test_dst_day_keeps_its_date(db):
    dst = date(2026, 10, 25)  # 25-hour day (end of summer time)
    svc.set_day_total(db, dst, 9 * H, now=local_to_utc(dst, dtime(23)), today=dst)
    (entry,) = tagessummen(db, dst)
    assert entry.day == dst and entry.duration_seconds == 9 * H
    assert entry.start_at == local_midnight_utc(dst)
    assert entry.end_at - entry.start_at == timedelta(hours=9)
    assert svc.day_state(db, dst).total == 9 * H


def test_validation_of_seconds_and_dates(db):
    with pytest.raises(entries_svc.EntryValidationError):
        svc.set_day_total(db, MON, 24 * H + 60, now=NOW)
    with pytest.raises(entries_svc.EntryValidationError):
        svc.set_day_total(db, MON, -60, now=NOW)
    with pytest.raises(entries_svc.EntryValidationError):
        svc.set_day_total(db, date(1999, 12, 31), H, now=NOW)


# --------------------------------------------------------------------------- week view


def test_week_without_period(db):
    week = svc.week_view(db, MON, today=TUE)
    assert week["label"] == "KW 45 · 02.–08.11.2026"
    assert week["ref"] == "2026-W45" and week["prev_ref"] == "2026-W44" and week["next_ref"] == "2026-W46"
    assert week["has_period"] is False and week["has_any_period"] is False
    assert week["soll"] is None and week["delta"] is None
    days = week["days"]
    assert [d["weekday"] for d in days] == ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    assert days[1]["is_today"] and days[2]["future"] and not days[0]["future"]
    assert days[5]["weekend"] and days[6]["weekend"] and not days[4]["weekend"]
    assert days[0]["aria_label"] == "Montag, 2. November: Stunden"


def test_week_labels_across_months_and_years():
    assert svc.week_label(date(2026, 11, 30)) == "KW 49 · 30.11.–06.12.2026"
    assert svc.week_label(date(2026, 12, 28)) == "KW 53 · 28.12.2026–03.01.2027"
    assert svc.parse_week_ref("2026-W53") == date(2026, 12, 28)
    assert svc.parse_week_ref("2025-W53") is None and svc.parse_week_ref("kaputt") is None


@pytest.mark.usefixtures("no_ferien_catalog")
def test_week_summary_with_period(db):
    cal.create_period(db, "2026/27")
    for i, hours in enumerate((9, 8.5, 8)):  # Mo–Mi, today = Wednesday
        svc.set_day_total(db, MON + timedelta(days=i), int(hours * H), now=NOW, today=MON + timedelta(days=2))
    week = svc.week_view(db, MON, today=MON + timedelta(days=2))
    assert week["has_period"] and week["period_label"] == "2026/27"
    assert week["soll"] == 40 * H  # five school days à 8:00 (Allerheiligen was Sunday)
    assert week["ist"] == week["ist_erfasst"] == 25.5 * H
    assert week["delta"] == 1.5 * H and week["delta_partial"] is True  # Mo–Mi vs. 24 h soll
    assert week["is_schulwoche"] and week["saldo_konto"] is not None
    mon = week["days"][0]
    assert mon["soll"] == 8 * H and mon["delta"] == 1 * H and mon["badges"] == []
    assert week["days"][5]["soll"] == 0 and week["days"][5]["day_type"] == "wochenende"


@pytest.mark.usefixtures("no_ferien_catalog")
def test_week_day_types_holiday_krank_and_erfassung_ab(db):
    cal.create_period(db, "2026/27")
    cal.set_override(db, TUE, absence_type="krank")
    settings = db.get(Settings, 1)
    settings.erfassung_ab = MON + timedelta(days=1)
    db.commit()
    week = svc.week_view(db, MON, today=MON + timedelta(days=4))
    mon, tue = week["days"][0], week["days"][1]
    assert mon["vor_erfassung"] is True
    assert tue["krank_neutral"] is True and [b["label"] for b in tue["badges"]] == ["Krank"]
    assert tue["ist_konto"] == tue["soll"] == 8 * H  # neutralised
    svc.set_day_total(db, TUE, 2 * H, now=NOW, today=MON + timedelta(days=4))  # work on a sick day still counts
    tue = svc.day_view(db, TUE, today=MON + timedelta(days=4))
    assert tue["ist"] == 2 * H and tue["ist_konto"] == 10 * H

    allerheiligen = svc.week_view(db, date(2027, 11, 1), today=TUE)["days"][0]  # Mon 01.11.2027, no period
    assert allerheiligen["holiday_name"] == "Allerheiligen" and allerheiligen["badges"][0]["label"] == "Feiertag"
    assert allerheiligen["soll"] is None  # outside every period: day type only, no soll


def test_future_days_are_allowed(db):
    svc.set_day_total(db, MON + timedelta(days=10), 3 * H, now=NOW, today=TUE)
    assert svc.day_view(db, MON + timedelta(days=10), today=TUE)["ist"] == 3 * H


def test_holidays_and_unterrichtsfrei_in_period(db):
    cal.create_period(db, "2026/27")  # with the official ferien from the bundled catalogue
    xmas = svc.week_view(db, date(2026, 12, 21), today=TUE)["days"]
    assert xmas[4]["holiday_name"] == "1. Weihnachtstag" and xmas[4]["soll"] == 0
    assert [b["label"] for b in xmas[4]["badges"]] == ["Feiertag"]
    assert [b["label"] for b in xmas[3]["badges"]] == ["Ferien"] and xmas[3]["ferien_name"]  # 24.12.
    buss = svc.week_view(db, date(2026, 11, 16), today=TUE)["days"][2]  # Buß- und Bettag 18.11.2026
    assert [b["label"] for b in buss["badges"]] == ["unterrichtsfrei"] and buss["soll"] == 8 * H


# --------------------------------------------------------------------------- review fixes


@pytest.mark.parametrize(("text", "seconds"), [
    ("7,30", 7 * H + 30 * 60), ("7.30", 7 * H + 30 * 60), ("7 30", 7 * H + 30 * 60), ("7,45", 7 * H + 45 * 60),
    ("7.15", 7 * H + 15 * 60), ("7,05", 7 * H + 5 * 60), (",30", 30 * 60), ("7,30 h", 7 * H + 30 * 60),
    ("7,25", 7 * H + 15 * 60), ("7,50", 7 * H + 30 * 60), ("7,75", 7 * H + 45 * 60),  # quarter hours as decimals
    ("8 h", 8 * H), ("0,5", 30 * 60),
])
def test_two_digits_after_the_separator_are_minutes(text, seconds):
    """'7.30' / '7,30' is how many people write 7 h 30 min (as in '7.30 Uhr'); 7:18 would be silently wrong."""
    assert svc.parse_total(text) == seconds


def test_hhmm_without_separator_gets_a_helpful_hint():
    with pytest.raises(entries_svc.EntryValidationError) as err:
        svc.parse_total("730")
    assert "Meintest du 7:30?" in err.value.message and "7,30" in err.value.message
    with pytest.raises(entries_svc.EntryValidationError) as err:
        svc.parse_total("7 75")
    assert "Minuten" in err.value.message


def test_rounded_display_value_sent_back_is_a_noop(db):
    """The field shows the total rounded to the minute; tabbing through a day with second-precise timer
    entries sends that rounded value back. It must neither create a junk Tagessumme nor give a 409."""
    a = local_to_utc(MON, dtime(8))
    entries_svc.create_entry(db, {"start_at": a, "end_at": a + timedelta(seconds=3 * H + 20 * 60 + 30),
                                  "category_id": cat_id(db, "VN")}, source="timer", now=NOW)
    assert svc.set_day_total(db, MON, 3 * H + 21 * 60, now=NOW, today=TUE)["action"] == "unveraendert"  # "3:21"
    assert tagessummen(db, MON) == []
    b = local_to_utc(TUE, dtime(8))
    entries_svc.create_entry(db, {"start_at": b, "end_at": b + timedelta(seconds=3 * H + 20 * 60 + 20),
                                  "category_id": cat_id(db, "VN")}, source="timer", now=NOW)
    assert svc.set_day_total(db, TUE, 3 * H + 20 * 60, now=NOW, today=TUE)["action"] == "unveraendert"  # "3:20"
    assert tagessummen(db, TUE) == []
    with pytest.raises(svc.DayTotalBelowDetail) as err:
        svc.set_day_total(db, TUE, 3 * H + 19 * 60, now=NOW, today=TUE)
    assert err.value.min_seconds == 3 * H + 20 * 60 and "mindestens 3:20" in err.value.message
    # a real change still works and the total is exactly what was entered
    svc.set_day_total(db, TUE, 4 * H, now=NOW, today=TUE)
    assert svc.day_state(db, TUE).total == 4 * H


def test_concurrent_requests_create_one_tagessumme(db, monkeypatch):
    """Two tabs flushing the same queued PUT at once must not double the day."""
    import threading
    import time

    from app.db.session import SessionLocal

    original = svc.day_state

    def slow_state(session, d, *, now=None):
        state = original(session, d, now=now)
        time.sleep(0.3)  # widen the read -> write window
        return state

    monkeypatch.setattr(svc, "day_state", slow_state)
    errors: list[str] = []

    def worker():
        session = SessionLocal()
        try:
            svc.set_day_total(session, MON, 8 * H, now=NOW, today=TUE)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(repr(exc))
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    db.expire_all()
    assert errors == []
    assert [e.duration_seconds for e in tagessummen(db, MON)] == [8 * H]


def test_device_time_decides_nachtraeglich_and_last_write(db):
    """Typed offline on Monday evening, synced on Friday: recorded on time. A queued value older than the
    last change of the Tagessumme (from another device) does not overwrite it."""
    monday_evening = local_to_utc(MON, dtime(21))
    friday = local_to_utc(MON + timedelta(days=4), dtime(8))
    svc.set_day_total(db, MON, 8 * H, at=monday_evening, now=friday, today=MON + timedelta(days=4))
    (entry,) = tagessummen(db, MON)
    assert "nachtraeglich" not in entry.flags and entry.created_at == monday_evening

    # Tuesday: 6 h entered on the laptop at 18:00 (online) ...
    svc.set_day_total(db, TUE, 6 * H, now=local_to_utc(TUE, dtime(18)), today=TUE)
    # ... a value typed offline on the phone at 12:00 arrives at 19:00: older -> ignored
    stale = svc.set_day_total(db, TUE, 3 * H, at=local_to_utc(TUE, dtime(12)), now=local_to_utc(TUE, dtime(19)),
                              today=TUE)
    assert stale["action"] == "veraltet" and stale["day"]["ist"] == 6 * H
    # a fresh request (input time = server time) always wins
    fresh = svc.set_day_total(db, TUE, 5 * H, at=local_to_utc(TUE, dtime(19)), now=local_to_utc(TUE, dtime(19)),
                              today=TUE)
    assert fresh["action"] == "geaendert" and fresh["day"]["ist"] == 5 * H


def test_device_time_plausibility(db):
    with pytest.raises(entries_svc.EntryValidationError):
        svc.set_day_total(db, MON, 8 * H, at=NOW + timedelta(hours=1), now=NOW, today=TUE)
    ancient = NOW - timedelta(days=30)  # implausible queue age -> server time
    svc.set_day_total(db, MON - timedelta(days=7), 8 * H, at=ancient, now=NOW, today=TUE)
    (entry,) = tagessummen(db, MON - timedelta(days=7))
    assert entry.created_at == NOW and "nachtraeglich" in entry.flags


def test_changing_a_tagessumme_late_is_flagged(db):
    """Pre-filling (or entering on time) and changing the value weeks later must not avoid the flag."""
    svc.set_day_total(db, TUE, 1 * H, now=local_to_utc(MON, dtime(8)), today=MON)
    later = local_to_utc(TUE + timedelta(days=21), dtime(8))
    svc.set_day_total(db, TUE, 9 * H, now=later, today=TUE + timedelta(days=21))
    (entry,) = tagessummen(db, TUE)
    assert entry.duration_seconds == 9 * H and "nachtraeglich" in entry.flags
    # a change within 48 h stays unflagged
    svc.set_day_total(db, MON, 2 * H, now=NOW, today=TUE)
    svc.set_day_total(db, MON, 3 * H, now=NOW + timedelta(hours=1), today=TUE)
    assert "nachtraeglich" not in tagessummen(db, MON)[0].flags


def test_entries_added_later_are_offered_not_rewritten(db):
    svc.set_day_total(db, MON, 8 * H, now=NOW, today=TUE)
    later = NOW + timedelta(hours=1)
    a = local_to_utc(MON, dtime(8))
    entries_svc.create_entry(db, {"start_at": a, "end_at": a + timedelta(hours=5), "category_id": cat_id(db, "U")},
                             source="manuell", now=later)
    day = svc.week_view(db, MON, today=TUE, now=later)["days"][0]
    assert day["ist"] == 13 * H and day["tagessumme_seconds"] == 8 * H  # nothing rewritten
    assert day["prompt"] == {"later": 5 * H, "keep": 8 * H, "total": 13 * H}
    # "13:00 stimmt": confirms, the question disappears, no content change / revision
    ok = svc.set_day_total(db, MON, 13 * H, now=later + timedelta(minutes=1), today=TUE)
    (entry,) = tagessummen(db, MON)
    assert ok["action"] == "bestaetigt" and ok["day"]["prompt"] is None and entry.duration_seconds == 8 * H
    assert revisions(db, entry.id) == []
    # "Auf 8:00 zurück" (another day): the Tagessumme shrinks to the rest
    svc.set_day_total(db, TUE, 8 * H, now=NOW, today=TUE)
    b = local_to_utc(TUE, dtime(8))
    entries_svc.create_entry(db, {"start_at": b, "end_at": b + timedelta(hours=5), "category_id": cat_id(db, "U")},
                             source="manuell", now=later)
    back = svc.set_day_total(db, TUE, 8 * H, now=later + timedelta(minutes=1), today=TUE)
    assert back["day"]["ist"] == 8 * H and back["day"]["tagessumme_seconds"] == 3 * H and back["day"]["prompt"] is None


def test_dst_day_length_caps_the_total(db):
    spring = date(2026, 3, 29)  # 23 hours
    autumn = date(2026, 10, 25)  # 25 hours
    assert svc.day_length(spring) == 23 * H and svc.day_length(autumn) == 25 * H and svc.day_length(MON) == 24 * H
    with pytest.raises(entries_svc.EntryValidationError) as err:
        svc.set_day_total(db, spring, 24 * H, now=NOW, today=TUE)
    assert "23 Stunden" in err.value.message
    svc.set_day_total(db, spring, 23 * H, now=NOW, today=TUE)
    with pytest.raises(entries_svc.EntryValidationError):
        svc.parse_total("24", max_seconds=svc.day_length(spring))
    assert svc.parse_total("24,5", max_seconds=svc.day_length(autumn)) == 24.5 * H


@pytest.mark.usefixtures("no_ferien_catalog")
def test_saldo_leaves_out_today_until_something_is_entered(db):
    cal.create_period(db, "2026/27")
    week = svc.week_view(db, MON, today=MON)  # Monday morning, nothing recorded
    assert week["delta"] is None and week["delta_until"] is None  # no red "-8:00" first thing in the morning
    assert week["days"][0]["in_saldo"] is False
    week = svc.week_view(db, MON, today=TUE)  # Tuesday, Monday empty
    assert week["delta"] == -8 * H and week["delta_until"] == "gestern"
    svc.set_day_total(db, TUE, 3 * H, now=NOW, today=TUE)
    week = svc.week_view(db, MON, today=TUE)
    assert week["delta"] == -13 * H and week["delta_until"] == "heute"


def test_week_across_two_periods_uses_each_days_period(db):
    """A boundary week (31.07.–06.08.) shows soll and day types of both periods."""
    from app.services import ledger

    p1 = cal.create_period(db, "2027/28")
    cal.create_period(db, "2028/29")
    mon = date(2028, 7, 31)
    week = svc.week_view(db, mon, today=date(2028, 8, 10))
    old = week["days"][0]
    row = ledger.load_context(db, p1, today=date(2028, 8, 10)).row(mon)
    assert old["in_period"] and old["soll"] == row.soll
    assert all(d["in_period"] for d in week["days"])
    assert week["soll"] == sum(d["soll"] for d in week["days"])


def test_az_cannot_be_deactivated_or_need_a_note(db):
    from app.services import categories as cats

    az = cat_id(db, "AZ")
    with pytest.raises(cats.CategoryError):
        cats.update_category(db, az, active=False)
    with pytest.raises(cats.CategoryError):
        cats.update_category(db, az, requires_note=True)
    db.rollback()
    cats.update_category(db, az, name="Pauschal", color="#123456", active=True, requires_note=False)
    svc.set_day_total(db, MON, 8 * H, now=NOW, today=TUE)
    assert tagessummen(db, MON)[0].duration_seconds == 8 * H
