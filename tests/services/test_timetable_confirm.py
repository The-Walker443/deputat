"""services/timetable.py: day confirmation (Konzept §7.2), Mehrarbeit via Vertretung flags (§6.6) and
the open-days to-do list."""

from __future__ import annotations

from datetime import date, time
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db.models import Category, DayConfirmation, MonthClose, Settings, TimeEntry, TimeEntryRevision
from app.services import calendar_service as cal
from app.services import entries as entries_svc
from app.services import ledger
from app.services import timetable as tts
from app.util import local_to_utc, to_local

EXAMPLE_ICS = Path(__file__).resolve().parents[2] / "examples" / "ferien_bayern_BEISPIEL.ics"

MON, TUE, WED, THU, FRI = (date(2026, 10, d) for d in (26, 27, 28, 29, 30))
TODAY = THU


@pytest.fixture
def year(db):
    period = cal.create_period(db, "2026/27")
    cal.commit_ics(db, period, EXAMPLE_ICS.read_bytes())
    return period


@pytest.fixture
def plan(db, year):
    return tts.create_timetable(db, valid_from=date(2026, 9, 15), slots=[
        {"weekday": 1, "slot_no": 1, "klasse": "3b", "fach": "Deutsch"},
        {"weekday": 1, "slot_no": 2, "klasse": "3b", "fach": "Mathematik"},
        {"weekday": 1, "slot_no": 3, "kind": "frei"},
        {"weekday": 1, "slot_no": 4, "klasse": "4a", "fach": "HSU"},
        {"weekday": 2, "slot_no": 1, "klasse": "1a"},
        {"weekday": 2, "slot_no": 2, "klasse": "1a"},
        {"weekday": 3, "slot_no": 1, "klasse": "2a"},
        {"weekday": 3, "slot_no": 2, "klasse": "2a"},
        {"weekday": 3, "slot_no": 3, "klasse": "2a"},
        {"weekday": 4, "slot_no": 1, "klasse": "1b"},
        {"weekday": 4, "slot_no": 2, "klasse": "1b"},
        {"weekday": 5, "slot_no": 1, "klasse": "1b"},
    ])


def slot(plan, weekday: int, slot_no: int):
    return next(s for s in plan.slots if s.weekday == weekday and s.slot_no == slot_no)


def gen(db, d: date):
    return tts.generate_suggestions(db, d, now=local_to_utc(d, time(0, 5)))


def confirm(db, d: date, deviations=None, **kw):
    kw.setdefault("now", local_to_utc(d, time(16)))
    kw.setdefault("today", max(d, TODAY))
    return tts.confirm_day(db, d, deviations, **kw)


def day_entries(db, d: date, status: str | None = None) -> list[TimeEntry]:
    db.expire_all()
    q = select(TimeEntry).where(TimeEntry.day == d, TimeEntry.deleted_at.is_(None))
    if status:
        q = q.where(TimeEntry.status == status)
    return list(db.scalars(q.order_by(TimeEntry.start_at)))


def cat(db, code: str) -> Category:
    return db.scalars(select(Category).where(Category.code == code)).one()


def hm(dt) -> str:
    return to_local(dt).strftime("%H:%M")


def revision_count(db) -> int:
    return db.scalar(select(func.count()).select_from(TimeEntryRevision))


# --------------------------------------------------------------------------- Tag wie geplant


def test_tag_wie_geplant_activates_all_suggestions_with_revisions(db, plan):
    gen(db, MON)
    result = confirm(db, MON)
    assert result.first is True and len(result.active) == 3 and result.verworfen == []
    active = day_entries(db, MON, "active")
    assert [(hm(e.start_at), e.klasse) for e in active] == [("08:00", "3b"), ("08:45", "3b"), ("10:35", "4a")]
    for entry in active:
        (rev,) = entries_svc.revisions(db, entry.id)
        assert rev.snapshot_json["status"] == "vorschlag" and rev.snapshot_json["_action"] == "update"
    row = db.get(DayConfirmation, MON)
    assert row is not None and row.confirmed_at == local_to_utc(MON, time(16))
    items = row.deviations_json["items"]
    assert items[slot(plan, 1, 1).id]["planned"]["start"] == "08:00:00"
    assert items[slot(plan, 1, 1).id]["deviation"] is None
    assert slot(plan, 1, 3).id not in items  # untouched free slot
    detail = ledger.day_detail(db, MON)
    assert detail.ist == 3 * 45 * 60 and detail.vorschlaege == []


def test_confirm_generates_missing_suggestions_itself(db, plan):
    result = confirm(db, TUE)
    assert len(result.active) == 2


def test_confirm_school_day_without_timetable(db, year):
    result = confirm(db, MON)
    assert result.active == [] and db.get(DayConfirmation, MON).deviations_json["items"] == {}


# --------------------------------------------------------------------------- deviations


def test_entfallen_sets_verworfen_and_does_not_count(db, plan):
    gen(db, MON)
    result = confirm(db, MON, [{"slot_id": slot(plan, 1, 2).id, "type": "entfallen"}])
    assert [hm(e.start_at) for e in result.verworfen] == ["08:45"]
    assert len(day_entries(db, MON, "active")) == 2
    assert ledger.day_detail(db, MON).ist == 2 * 45 * 60
    dev = db.get(DayConfirmation, MON).deviations_json["items"][slot(plan, 1, 2).id]
    assert dev["deviation"] == {"type": "entfallen"} and dev["status"] == "verworfen"


def test_vertretung_sets_flag_klasse_and_subcategory(db, plan):
    gen(db, MON)
    confirm(db, MON, [
        {"slot_id": slot(plan, 1, 1).id, "type": "vertretung", "klasse": "4b", "fach": "Englisch"},
        {"slot_id": slot(plan, 1, 2).id, "type": "vertretung", "klasse": "2b", "art": "genehmigt"},
    ])
    first, second, _ = day_entries(db, MON, "active")
    sub = db.get(Category, first.subcategory_id)
    assert first.flags == ["vertretung_angeordnet"] and (first.klasse, first.fach) == ("4b", "Englisch")
    assert sub.name == tts.VERTRETUNG_SUBCATEGORY_NAME and first.category_id == cat(db, "U").id
    assert second.flags == ["vertretung_genehmigt"] and (second.klasse, second.fach) == ("2b", None)


def test_vertretung_flags_feed_mehrarbeit(db, plan):
    # Mo 2 + Di 2 Vertretungen = 4 UStd > Bagatellgrenze 3
    for d, weekday in ((MON, 1), (TUE, 2)):
        gen(db, d)
    confirm(db, MON, [{"slot_id": slot(plan, 1, 1).id, "type": "vertretung", "klasse": "4b"},
                      {"slot_id": slot(plan, 1, 2).id, "type": "vertretung", "klasse": "4b"}])
    confirm(db, TUE, [{"slot_id": slot(plan, 2, 1).id, "type": "vertretung", "klasse": "2b", "art": "genehmigt"},
                      {"slot_id": slot(plan, 2, 2).id, "type": "vertretung", "klasse": "2b", "art": "genehmigt"}])
    ctx = ledger.load_context(db, today=TODAY)
    month = ctx.mehrarbeit[(2026, 10)]
    assert (month.ustd_angeordnet, month.ustd_genehmigt, month.total, month.ausgleichsfaehig) == (2.0, 2.0, 4.0, 4.0)


def test_unconfirmed_vertretung_does_not_count_as_mehrarbeit(db, plan):
    gen(db, MON)
    ctx = ledger.load_context(db, today=TODAY)
    assert (2026, 10) not in ctx.mehrarbeit


def test_zeit_angepasst_creates_revision_with_old_times(db, plan):
    gen(db, MON)
    target = slot(plan, 1, 4)
    confirm(db, MON, [{"slot_id": target.id, "type": "zeit_angepasst", "start": "10:35", "end": "11:35"}])
    entry = next(e for e in day_entries(db, MON, "active") if e.timetable_slot_id == target.id)
    assert (hm(entry.start_at), hm(entry.end_at), entry.duration_seconds) == ("10:35", "11:35", 3600)
    (rev,) = entries_svc.revisions(db, entry.id)
    assert rev.snapshot_json["end_at"] == "2026-10-26T10:20:00Z"  # 11:20 local before the change
    assert rev.snapshot_json["status"] == "vorschlag"


def test_exkursion_moves_slot_to_sv(db, plan):
    gen(db, MON)
    confirm(db, MON, [{"slot_id": slot(plan, 1, 4).id, "type": "exkursion", "end": "12:20",
                       "note": "Unterrichtsgang Wald"}])
    entry = day_entries(db, MON, "active")[-1]
    assert entry.category_id == cat(db, "SV").id and entry.note == "Unterrichtsgang Wald"
    assert (hm(entry.start_at), hm(entry.end_at)) == ("10:35", "12:20")
    assert entry.klasse == "4a"


def test_exkursion_with_explicit_category(db, plan):
    gen(db, MON)
    wandertag = db.scalars(select(Category).where(Category.name == "Wandertag")).one()
    confirm(db, MON, [{"slot_id": slot(plan, 1, 4).id, "type": "exkursion", "category_id": wandertag.id}])
    entry = day_entries(db, MON, "active")[-1]
    assert (entry.category_id, entry.subcategory_id) == (cat(db, "SV").id, wandertag.id)


def test_vertretung_in_free_slot_creates_an_entry(db, plan):
    gen(db, MON)
    free = slot(plan, 1, 3)
    result = confirm(db, MON, [{"slot_id": free.id, "type": "vertretung", "klasse": "3a"}])
    created = next(e for e in result.active if e.timetable_slot_id == free.id)
    assert created.flags == ["vertretung_angeordnet"] and created.klasse == "3a"
    assert (hm(created.start_at), hm(created.end_at)) == ("09:50", "10:35")
    # re-confirm without the deviation: the extra entry is discarded again
    confirm(db, MON, now=local_to_utc(MON, time(17)))
    db.expire_all()
    assert db.get(TimeEntry, created.id).status == "verworfen"


def test_vertretung_in_free_slot_needs_klasse(db, plan):
    with pytest.raises(tts.TimetableError, match="Klasse"):
        confirm(db, MON, [{"slot_id": slot(plan, 1, 3).id, "type": "vertretung"}])
    with pytest.raises(tts.TimetableError, match="freien Stunde"):
        confirm(db, MON, [{"slot_id": slot(plan, 1, 3).id, "type": "exkursion"}])


def test_extra_vertretung_outside_the_plan(db, plan):
    gen(db, MON)
    extra = [{"start": "11:35", "end": "12:20", "klasse": "2b", "fach": "Sport"}]
    result = confirm(db, MON, extra=extra)
    (entry,) = result.extra
    assert entry.source == "manuell" and entry.flags == ["vertretung_angeordnet"] and entry.klasse == "2b"
    assert entry.category_id == cat(db, "U").id
    again = confirm(db, MON, extra=extra, now=local_to_utc(MON, time(17)))
    assert [e.id for e in again.extra] == [entry.id]  # idempotent
    stored = db.get(DayConfirmation, MON).deviations_json["extra"]
    assert [x["entry_id"] for x in stored] == [entry.id]
    # a re-confirmation without extra keeps it (extras are additive)
    confirm(db, MON, now=local_to_utc(MON, time(18)))
    assert db.get(TimeEntry, entry.id).status == "active"
    assert len(db.get(DayConfirmation, MON).deviations_json["extra"]) == 1


def test_extra_vertretung_overlapping_a_lesson_is_a_conflict(db, plan):
    with pytest.raises(tts.TimetableConflict, match="Überschneidung"):
        confirm(db, MON, extra=[{"start": "08:30", "end": "09:00", "klasse": "2b"}])
    assert db.get(DayConfirmation, MON) is None


def test_deviation_by_entry_id_and_mapping_form(db, plan):
    gen(db, MON)
    first = day_entries(db, MON)[0]
    confirm(db, MON, {first.id: {"type": "entfallen"}})
    assert db.get(TimeEntry, first.id).status == "verworfen"
    confirm(db, MON, [{"entry_id": first.id, "type": "wie_geplant"}], now=local_to_utc(MON, time(17)))
    assert db.get(TimeEntry, first.id).status == "active"


# --------------------------------------------------------------------------- re-confirmation


def test_reconfirm_with_same_input_changes_nothing(db, plan):
    gen(db, MON)
    devs = [{"slot_id": slot(plan, 1, 2).id, "type": "vertretung", "klasse": "4b"},
            {"slot_id": slot(plan, 1, 4).id, "type": "entfallen"}]
    confirm(db, MON, devs)
    before = revision_count(db)
    result = confirm(db, MON, devs, now=local_to_utc(MON, time(17)))
    assert result.changed == [] and result.first is False
    assert revision_count(db) == before
    data = db.get(DayConfirmation, MON).deviations_json
    assert data["history"] == ["2026-10-26T15:00:00Z"]


def test_reconfirm_reverts_deviations_to_plan(db, plan):
    gen(db, MON)
    s2, s4 = slot(plan, 1, 2), slot(plan, 1, 4)
    confirm(db, MON, [{"slot_id": s2.id, "type": "vertretung", "klasse": "4b"},
                      {"slot_id": s4.id, "type": "zeit_angepasst", "start": "10:40", "end": "11:30"}])
    result = confirm(db, MON, [{"slot_id": s4.id, "type": "entfallen"}], now=local_to_utc(MON, time(17)))
    e2 = next(e for e in day_entries(db, MON) if e.timetable_slot_id == s2.id)
    e4 = next(e for e in day_entries(db, MON) if e.timetable_slot_id == s4.id)
    assert (e2.klasse, e2.fach, e2.flags, e2.subcategory_id) == ("3b", "Mathematik", [], None)
    assert e4.status == "verworfen" and (hm(e4.start_at), hm(e4.end_at)) == ("10:35", "11:20")
    assert {e.id for e in result.changed} == {e2.id, e4.id}
    assert len(entries_svc.revisions(db, e2.id)) == 2  # vorschlag->vertretung, vertretung->plan


def test_reconfirm_uses_stored_plan_even_if_slot_changed(db, plan):
    gen(db, MON)
    s1 = slot(plan, 1, 1)
    confirm(db, MON, [{"slot_id": s1.id, "type": "vertretung", "klasse": "4b"}])
    tts.update_slot(db, s1.id, {"klasse": "1a", "fach": "Kunst"})
    confirm(db, MON, now=local_to_utc(MON, time(17)))
    entry = next(e for e in day_entries(db, MON) if e.timetable_slot_id == s1.id)
    assert (entry.klasse, entry.fach) == ("3b", "Deutsch")


def test_reconfirm_picks_up_slot_added_later(db, plan):
    confirm(db, FRI, today=FRI)
    tts.add_slot(db, plan.id, {"weekday": 5, "slot_no": 2, "klasse": "1b"})
    result = confirm(db, FRI, today=FRI, now=local_to_utc(FRI, time(17)))
    assert len(result.active) == 2


# --------------------------------------------------------------------------- conflicts / atomicity


def _manual(db, d: date, start: time, end: time, code_: str = "KV") -> TimeEntry:
    (entry,) = entries_svc.create_entry(
        db, {"start_at": local_to_utc(d, start), "end_at": local_to_utc(d, end), "category_id": cat(db, code_).id},
        source="manuell", now=local_to_utc(d, time(12)),
    )
    return entry


def test_conflicting_suggestion_blocks_confirm_atomically(db, plan):
    gen(db, MON)
    _manual(db, MON, time(10, 0), time(11, 0))
    before = revision_count(db)
    with pytest.raises(tts.TimetableConflict, match="4. Stunde"):
        confirm(db, MON)
    assert db.get(DayConfirmation, MON) is None
    assert revision_count(db) == before
    assert all(e.status == "vorschlag" for e in day_entries(db, MON) if e.source == "stundenplan")


def test_conflict_resolved_with_entfallen_or_time(db, plan):
    gen(db, MON)
    _manual(db, MON, time(10, 0), time(11, 0))
    s4 = slot(plan, 1, 4)
    confirm(db, MON, [{"slot_id": s4.id, "type": "zeit_angepasst", "start": "11:00", "end": "11:45"}])
    entry = next(e for e in day_entries(db, MON) if e.timetable_slot_id == s4.id)
    assert entry.status == "active" and hm(entry.start_at) == "11:00"


def test_deviation_order_frees_time_before_activating(db, plan):
    # moving slot 1 into slot 2's time works when slot 2 is cancelled in the same confirmation
    gen(db, MON)
    confirm(db, MON, [
        {"slot_id": slot(plan, 1, 1).id, "type": "zeit_angepasst", "start": "08:30", "end": "09:15"},
        {"slot_id": slot(plan, 1, 2).id, "type": "entfallen"},
    ])
    assert [hm(e.start_at) for e in day_entries(db, MON, "active")] == ["08:30", "10:35"]


# --------------------------------------------------------------------------- validation / locks


@pytest.mark.parametrize(
    ("deviation", "message"),
    [
        ({"type": "verschoben"}, "Unbekannte Abweichung"),
        ({"type": "zeit_angepasst", "start": "08:00"}, "Beginn und Ende"),
        ({"type": "zeit_angepasst", "start": "09:00", "end": "08:00"}, "Ende muss nach"),
        ({"type": "vertretung", "klasse": "4b", "art": "freiwillig"}, "angeordnet"),
        ({"type": "vertretung", "klasse": "9z"}, "Unbekannte Klasse"),
        ({"type": "exkursion", "category_id": 99999}, "Unbekannte Kategorie"),
    ],
)
def test_invalid_deviations(db, plan, deviation, message):
    gen(db, MON)
    with pytest.raises(entries_svc.DomainError, match=message):
        confirm(db, MON, [{"slot_id": slot(plan, 1, 1).id, **deviation}])
    assert db.get(DayConfirmation, MON) is None


def test_unknown_slot_and_duplicate_deviation(db, plan):
    with pytest.raises(tts.TimetableError, match="unbekannte Stunde"):
        confirm(db, MON, [{"slot_id": "nope", "type": "entfallen"}])
    with pytest.raises(tts.TimetableError, match="unbekannte Stunde"):
        confirm(db, MON, [{"slot_id": slot(plan, 2, 1).id, "type": "entfallen"}])  # Tuesday slot on Monday
    s = slot(plan, 1, 1).id
    with pytest.raises(tts.TimetableError, match="mehr als eine"):
        confirm(db, MON, [{"slot_id": s, "type": "entfallen"}, {"slot_id": s, "type": "wie_geplant"}])


def test_cannot_confirm_future_or_non_school_days(db, plan):
    with pytest.raises(tts.TimetableError, match="Zukünftige"):
        tts.confirm_day(db, FRI, today=THU)
    with pytest.raises(tts.TimetableError, match="Ferientag"):
        confirm(db, date(2026, 11, 2), today=date(2026, 11, 3))
    cal.set_override(db, TUE, absence_type="krank")
    with pytest.raises(tts.TimetableError, match="krank"):
        confirm(db, TUE)


def test_closed_month_needs_reason(db, plan):
    gen(db, MON)
    db.add(MonthClose(year_month="2026-10", entries_hash_sha256=entries_svc.month_entries_hash(db, "2026-10"),
                      soll_seconds=0, ist_seconds=0, saldo_seconds=0))
    db.commit()
    with pytest.raises(entries_svc.EntryLocked):
        confirm(db, MON)
    result = confirm(db, MON, reason="Nachtrag Stundenplan")
    assert len(result.active) == 3
    month = db.get(MonthClose, "2026-10")
    assert month.status == "nachtraeglich_korrigiert" and month.hash_history
    assert all("korrigiert" in e.flags for e in result.active)


def test_pause_suggestion_can_be_cancelled_by_entry_id(db, plan):
    db.get(Settings, 1).grosse_pause_zaehlt = True
    db.commit()
    gen(db, WED)  # Mi 1, 2, 3 -> pause 09:30–09:50
    pause = next(e for e in day_entries(db, WED) if e.timetable_slot_id is None)
    result = confirm(db, WED, [{"entry_id": pause.id, "type": "entfallen"}])
    assert [e.id for e in result.verworfen] == [pause.id]
    assert db.get(DayConfirmation, WED).deviations_json["items"][f"entry:{pause.id}"]["kind"] == "pause"


def test_confirm_result_to_dict(db, plan):
    data = confirm(db, MON).to_dict()
    assert data["first_confirmation"] is True and data["confirmed_at"] == "2026-10-26T15:00:00Z"
    assert len(data["active"]) == 3 and data["active"][0]["status"] == "active"


# --------------------------------------------------------------------------- day view


def test_day_view_combines_ledger_slots_and_confirmation(db, plan):
    gen(db, MON)
    _manual(db, MON, time(8, 30), time(8, 50))
    view = tts.day_view(db, MON, today=TODAY)
    assert view["day_type"] == "schultag" and view["soll"] == 8 * 3600 and view["soll_hm"] == "8:00"
    assert view["ist_hm"] == "0:20" and view["delta_hm"] == "-7:40"
    assert len(view["vorschlaege"]) == 3 and len(view["entries"]) == 1
    assert [s["slot_no"] for s in view["slots"]] == [1, 2, 3, 4]
    assert [c["start"] for c in view["conflicts"]] == ["08:00", "08:45"]
    assert view["confirmed"] is False and view["can_confirm"] is True and view["timetable"]["id"] == plan.id
    holiday = tts.day_view(db, date(2026, 11, 2), today=TODAY)
    assert holiday["schultag"] is False and holiday["can_confirm"] is False
    assert "Ferientag" in holiday["suggestions_hinweis"]


# --------------------------------------------------------------------------- open days


def test_open_days_lists_unconfirmed_and_empty_school_days(db, plan):
    cal.set_override(db, TUE, absence_type="krank")
    cal.set_override(db, WED, absence_type="fortbildung")
    confirm(db, MON)
    result = tts.open_days(db, today=TODAY)
    by_date = {d.date: d for d in result.days}
    assert result.until == TODAY and result.period.label == "2026/27"
    assert MON not in by_date  # confirmed and has entries
    assert TUE not in by_date  # krank
    assert by_date[WED].ohne_eintraege and not by_date[WED].unbestaetigt
    assert by_date[WED].gruende == ("Fortbildung ohne Eintrag",)
    assert by_date[THU].unbestaetigt and by_date[THU].ohne_eintraege
    assert FRI not in by_date  # after today
    assert date(2026, 11, 2) not in by_date  # ferien
    first = result.days[0]
    assert first.date == date(2026, 9, 15) and first.unbestaetigt  # first school day of the year
    assert date(2026, 9, 14) not in by_date  # still Sommerferien


def test_open_days_with_entries_but_unconfirmed(db, plan):
    _manual(db, THU, time(14), time(15), "VN")
    gen(db, THU)
    day = next(d for d in tts.open_days(db, today=TODAY).days if d.date == THU)
    assert day.unbestaetigt and not day.ohne_eintraege and day.vorschlaege == 2
    assert day.gruende == ("Stundenplan nicht bestätigt",)


def test_open_days_without_timetable_only_lists_days_without_entries(db, year):
    _manual(db, THU, time(14), time(15), "VN")
    days = {d.date: d for d in tts.open_days(db, today=TODAY).days}
    assert THU not in days
    assert days[WED].gruende == ("Keine Einträge",) and not days[WED].unbestaetigt


def test_open_days_until_is_capped(db, plan):
    result = tts.open_days(db, until=date(2026, 9, 30), today=TODAY)
    assert result.until == date(2026, 9, 30) and result.days[-1].date == date(2026, 9, 30)
    future = tts.open_days(db, until=date(2027, 1, 1), today=TODAY)
    assert future.until == TODAY
    early = tts.open_days(db, until=date(2026, 7, 1), today=TODAY)
    assert early.days == []
    data = result.to_dict()
    assert data["count"] == len(result.days) and data["days"][0]["weekday_name"] == "Dienstag"


def test_open_days_without_period_raises(db):
    with pytest.raises(cal.NoPeriodError):
        tts.open_days(db, today=TODAY)
