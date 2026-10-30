"""services/timetable.py: timetable suggestions (Konzept §7.2, §6.3) for the example school year 2026/27."""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db.models import CalendarDay, Category, DayConfirmation, MonthClose, Settings, TimeEntry
from app.services import calendar_service as cal
from app.services import entries as entries_svc
from app.services import ledger
from app.services import timetable as tts
from app.util import local_to_utc

EXAMPLE_ICS = Path(__file__).resolve().parents[2] / "examples" / "ferien_bayern_BEISPIEL.ics"

MON, TUE, WED, THU, FRI = (date(2026, 10, d) for d in (26, 27, 28, 29, 30))


@pytest.fixture
def year(db):
    period = cal.create_period(db, "2026/27")
    cal.commit_ics(db, period, EXAMPLE_ICS.read_bytes())
    return period


@pytest.fixture
def plan(db, year):
    """Mo: 1–2 3b, Aufsicht in the break, 3–4 4a; Di: 1 Sprechstunde, 2 frei, 3 Konferenz."""
    return tts.create_timetable(db, valid_from=date(2026, 9, 15), slots=[
        {"weekday": 1, "slot_no": 1, "klasse": "3b", "fach": "Deutsch"},
        {"weekday": 1, "slot_no": 2, "klasse": "3b", "fach": "Mathematik"},
        {"weekday": 1, "slot_no": 21, "kind": "aufsicht", "start_time": "09:30", "end_time": "09:50"},
        {"weekday": 1, "slot_no": 3, "klasse": "4a", "fach": "HSU"},
        {"weekday": 1, "slot_no": 4, "klasse": "4a", "fach": "HSU"},
        {"weekday": 2, "slot_no": 1, "kind": "sprechstunde"},
        {"weekday": 2, "slot_no": 2, "kind": "frei"},
        {"weekday": 2, "slot_no": 3, "kind": "konferenz_fix"},
        {"weekday": 3, "slot_no": 1, "klasse": "2a"},
        {"weekday": 3, "slot_no": 2, "klasse": "2a"},
        {"weekday": 3, "slot_no": 3, "klasse": "2a"},
        {"weekday": 3, "slot_no": 5, "klasse": "2a"},
        {"weekday": 4, "slot_no": 1, "klasse": "1a"},
        {"weekday": 5, "slot_no": 1, "klasse": "1b"},
    ])


def gen(db, d: date) -> tts.SuggestionResult:
    return tts.generate_suggestions(db, d, now=local_to_utc(d, time(0, 5)))


def suggestions(db, d: date, *, include_deleted=False) -> list[TimeEntry]:
    q = select(TimeEntry).where(TimeEntry.day == d, TimeEntry.source == "stundenplan")
    if not include_deleted:
        q = q.where(TimeEntry.deleted_at.is_(None))
    return list(db.scalars(q.order_by(TimeEntry.start_at)))


def code(db, entry: TimeEntry) -> str:
    return db.get(Category, entry.category_id).code


def local_hm(dt: datetime) -> str:
    from app.util import to_local

    return to_local(dt).strftime("%H:%M")


# --------------------------------------------------------------------------- basics


def test_monday_suggestions_from_the_plan(db, plan):
    result = gen(db, MON)
    assert result.skipped_reason is None and result.warnings == [] and result.timetable_id == plan.id
    rows = suggestions(db, MON)
    assert [(local_hm(e.start_at), local_hm(e.end_at), code(db, e), e.klasse) for e in rows] == [
        ("08:00", "08:45", "U", "3b"),
        ("08:45", "09:30", "U", "3b"),
        ("09:30", "09:50", "AV", None),
        ("09:50", "10:35", "U", "4a"),
        ("10:35", "11:20", "U", "4a"),
    ]
    assert all(e.status == "vorschlag" and e.source == "stundenplan" for e in rows)
    assert all(e.timetable_slot_id for e in rows) and all(e.location == "schule" for e in rows)
    assert all("nachtraeglich" not in e.flags for e in rows)
    assert {e.id for e in result.created} == {e.id for e in rows}


def test_tuesday_uses_kind_categories_and_skips_free_slots(db, plan):
    gen(db, TUE)
    rows = suggestions(db, TUE)
    assert [code(db, e) for e in rows] == ["E", "KO"]


def test_each_weekday_gets_its_own_slots(db, plan):
    for d, expected in ((WED, 4), (THU, 1), (FRI, 1)):
        gen(db, d)
        assert len(suggestions(db, d)) == expected
    assert {e.klasse for e in suggestions(db, WED)} == {"2a"}


def test_suggestions_do_not_count_until_confirmed(db, plan):
    gen(db, MON)
    detail = ledger.day_detail(db, MON)
    assert detail.ist == 0 and detail.entries == []
    assert len(detail.vorschlaege) == 5


def test_generation_is_idempotent(db, plan):
    first = gen(db, MON)
    second = gen(db, MON)
    assert second.created == [] and len(second.existing) == len(first.created) == 5
    assert db.scalar(select(func.count()).select_from(TimeEntry)) == 5


def test_nothing_for_days_before_the_first_version(db, year):
    tts.create_timetable(db, valid_from=date(2026, 10, 27), slots=[{"weekday": 1, "slot_no": 1}])
    result = gen(db, MON)
    assert result.created == [] and result.skipped_reason == "Für diesen Tag gilt kein Stundenplan."


def test_version_validity_picks_the_right_version(db, plan, year):
    copy = tts.copy_timetable(db, plan.id, valid_from=date(2027, 2, 15))
    tts.set_slots(db, copy.id, [{"weekday": 1, "slot_no": 5, "klasse": "1a", "fach": "Sport"}])
    winter = date(2027, 1, 25)  # Monday, old version
    spring = date(2027, 2, 15)  # Monday, new version
    assert gen(db, winter).timetable_id == plan.id
    assert len(suggestions(db, winter)) == 5
    assert gen(db, spring).timetable_id == copy.id
    rows = suggestions(db, spring)
    assert [(local_hm(e.start_at), e.fach) for e in rows] == [("11:35", "Sport")]


# --------------------------------------------------------------------------- no suggestions


@pytest.mark.parametrize(
    ("d", "reason"),
    [
        (date(2026, 11, 2), "Ferientag"),  # Herbstferien (Monday)
        (date(2026, 10, 31), "Wochenende"),
        (date(2027, 5, 6), "Feiertag"),  # Christi Himmelfahrt (Thursday)
        (date(2026, 11, 18), "Kein Schultag"),  # Buß- und Bettag (Wednesday)
    ],
)
def test_no_suggestions_on_non_school_days(db, plan, d, reason):
    result = gen(db, d)
    assert result.created == [] and reason in result.skipped_reason
    assert suggestions(db, d) == []


def test_no_suggestions_on_full_day_absences(db, plan):
    cal.set_override(db, TUE, absence_type="krank")
    cal.set_override(db, WED, absence_type="fortbildung")
    assert "krank" in gen(db, TUE).skipped_reason
    assert "Fortbildung" in gen(db, WED).skipped_reason
    assert suggestions(db, TUE) == [] and suggestions(db, WED) == []


def test_no_suggestions_on_leave_days(db, plan, year):
    leave_day = db.scalars(
        select(CalendarDay.date).where(CalendarDay.absence_type == "urlaub", CalendarDay.weekday <= 5)
    ).first()
    assert leave_day is not None
    result = gen(db, leave_day)
    assert result.created == [] and result.skipped_reason


def test_no_suggestions_outside_any_period(db):
    tts.create_timetable(db, valid_from=date(2020, 1, 1), slots=[{"weekday": 1, "slot_no": 1}])
    result = gen(db, MON)
    assert result.created == [] and "kein Schuljahr" in result.skipped_reason


def test_day_turning_into_absence_retires_unconfirmed_suggestions(db, plan):
    gen(db, MON)
    cal.set_override(db, MON, absence_type="krank")
    result = gen(db, MON)
    assert len(result.retired) == 5
    assert suggestions(db, MON) == []
    deleted = suggestions(db, MON, include_deleted=True)
    assert all(e.deleted_at is not None for e in deleted)
    rev = entries_svc.revisions(db, deleted[0].id)[-1]
    assert rev.change_reason == tts.RETIRE_REASON


# --------------------------------------------------------------------------- große Pause


def test_breaks_are_not_suggested_by_default(db, plan):
    gen(db, WED)  # Mi: 1, 2, 3, 5 -> gaps 09:30–09:50 and 11:20–11:35 (and 10:35–11:35 is a free period)
    assert [local_hm(e.start_at) for e in suggestions(db, WED)] == ["08:00", "08:45", "09:50", "11:35"]


def test_breaks_count_when_grosse_pause_zaehlt(db, plan):
    db.get(Settings, 1).grosse_pause_zaehlt = True
    db.commit()
    gen(db, WED)
    rows = suggestions(db, WED)
    pauses = [e for e in rows if e.timetable_slot_id is None]
    assert [(local_hm(e.start_at), local_hm(e.end_at), code(db, e), e.note) for e in pauses] == [
        ("09:30", "09:50", "AV", tts.PAUSE_NOTE),
    ]  # the 60-minute gap 10:35–11:35 is a free period, not a break
    assert gen(db, WED).created == []  # idempotent for pauses too


def test_aufsicht_slot_fills_the_break(db, plan):
    db.get(Settings, 1).grosse_pause_zaehlt = True
    db.commit()
    gen(db, MON)
    assert all(e.timetable_slot_id for e in suggestions(db, MON))  # break is the Aufsicht slot itself


def test_switching_grosse_pause_off_retires_pause_suggestions(db, plan):
    settings = db.get(Settings, 1)
    settings.grosse_pause_zaehlt = True
    db.commit()
    gen(db, WED)
    settings.grosse_pause_zaehlt = False
    db.commit()
    result = gen(db, WED)
    assert len(result.retired) == 1
    assert all(e.timetable_slot_id for e in suggestions(db, WED))


# --------------------------------------------------------------------------- overlaps, stale, locks


def test_overlap_with_manual_entry_is_reported_not_blocked(db, plan):
    (manual,) = entries_svc.create_entry(
        db, {"start_at": local_to_utc(MON, time(8, 30)), "end_at": local_to_utc(MON, time(9, 0)),
             "category_id": db.scalar(select(Category.id).where(Category.code == "KV"))},
        source="manuell", now=local_to_utc(MON, time(9, 30)),
    )
    result = gen(db, MON)
    assert len(result.created) == 5
    assert [(c["start"], [o["entry_id"] for o in c["overlaps"]]) for c in result.conflicts] == [
        ("08:00", [manual.id]),
        ("08:45", [manual.id]),
    ]
    assert tts.suggestion_conflicts(db, MON) == result.conflicts


def test_running_timer_counts_as_open_ended_conflict(db, plan):
    from app.services import timer

    timer.start(db, db.scalar(select(Category.id).where(Category.code == "VN")),
                started_at=local_to_utc(MON, time(10, 0)), now=local_to_utc(MON, time(10, 0)))
    conflicts = gen(db, MON).conflicts
    assert [c["start"] for c in conflicts] == ["09:50", "10:35"]
    assert conflicts[0]["overlaps"][0]["running"] is True


def test_changed_slot_replaces_unconfirmed_suggestion(db, plan):
    gen(db, THU)
    (old,) = suggestions(db, THU)
    slot = next(s for s in plan.slots if s.weekday == 4)
    tts.update_slot(db, slot.id, {"klasse": "1b"})
    (new,) = gen(db, THU).created
    assert new.klasse == "1b" and new.id != old.id
    assert db.get(TimeEntry, old.id).deleted_at is not None


def test_deleted_slot_retires_its_suggestion(db, plan):
    gen(db, FRI)
    (old,) = suggestions(db, FRI)
    tts.delete_slot(db, old.timetable_slot_id)
    assert suggestions(db, FRI) == []
    assert gen(db, FRI).created == []


def test_shortened_version_retires_suggestions_on_regeneration(db, plan):
    gen(db, FRI)
    tts.update_timetable(db, plan.id, {"valid_to": date(2026, 10, 29)})
    result = gen(db, FRI)
    assert len(result.retired) == 1 and suggestions(db, FRI) == []


def test_confirmed_day_is_left_alone(db, plan):
    gen(db, THU)
    tts.confirm_day(db, THU, now=local_to_utc(THU, time(15)), today=THU)
    tts.add_slot(db, plan.id, {"weekday": 4, "slot_no": 2, "klasse": "1a"})
    result = gen(db, THU)
    assert result.confirmed and result.created == [] and "bestätigt" in result.skipped_reason


def test_closed_month_is_skipped(db, plan):
    db.add(MonthClose(year_month="2026-10", entries_hash_sha256="0" * 64, soll_seconds=0, ist_seconds=0,
                      saldo_seconds=0))
    db.commit()
    result = gen(db, MON)
    assert result.created == [] and "abgeschlossen" in result.skipped_reason


def test_invalid_klasse_gives_warning_and_keeps_other_slots(db, plan):
    db.get(Settings, 1).klassen = ["4a"]  # 3b no longer exists
    db.commit()
    result = gen(db, MON)
    assert len(result.created) == 3  # 4a lessons + Aufsicht
    assert len(result.warnings) == 2 and "Unbekannte Klasse" in result.warnings[0]


def test_late_generation_marks_entries_nachtraeglich(db, plan):
    tts.generate_suggestions(db, MON, now=local_to_utc(date(2026, 11, 5), time(12)))
    assert all("nachtraeglich" in e.flags for e in suggestions(db, MON))


def test_result_to_dict_is_json_ready(db, plan):
    data = gen(db, MON).to_dict()
    assert data["date"] == MON and len(data["created"]) == 5
    assert data["created"][0]["status"] == "vorschlag" and data["retired"] == []


# --------------------------------------------------------------------------- scheduler hook


def test_generate_today_suggestions_uses_own_session(db, plan):
    result = tts.generate_today_suggestions(today=MON, now=local_to_utc(MON, time(0, 5)))
    assert result is not None and len(result.created) == 5
    again = tts.generate_today_suggestions(today=MON, now=local_to_utc(MON, time(7)))
    assert again.created == []
    db.expire_all()
    assert len(suggestions(db, MON)) == 5


def test_generate_today_suggestions_never_raises(db, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(tts, "generate_suggestions", boom)
    assert tts.generate_today_suggestions(today=MON) is None


def test_generate_today_without_period_reports_reason(db):
    result = tts.generate_today_suggestions(today=MON)
    assert result is not None and result.created == [] and result.skipped_reason


def test_register_scheduler_jobs():
    calls = []

    class FakeScheduler:
        def add_job(self, func, trigger, **kwargs):
            calls.append((func, trigger, kwargs))

    tts.register_scheduler_jobs(FakeScheduler())
    ((func, trigger, kwargs),) = calls
    assert func is tts.generate_today_suggestions
    assert kwargs["id"] == tts.SCHEDULER_JOB_ID and kwargs["replace_existing"] is True
    fields = {f.name: str(f) for f in trigger.fields}
    assert fields["hour"] == "0" and fields["minute"] == "5"
    assert str(trigger.timezone) == "Europe/Berlin"


def test_confirmation_row_absent_after_generation(db, plan):
    gen(db, MON)
    assert db.get(DayConfirmation, MON) is None
