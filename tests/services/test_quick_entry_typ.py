"""services/quick_entry.py: day types of the "Erfassen" screen (Unterricht, Krank, Fortbildung, Klassenfahrt,
Schulveranstaltung) and the explicit Tagessumme marker (migration 0006)."""

from __future__ import annotations

from datetime import date, time as dtime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import CalendarDay, Category, DayOverride, MonthClose, Settings, TimeEntry, TimeEntryRevision
from app.services import calendar_service as cal
from app.services import entries as entries_svc
from app.services import quick_entry as svc
from app.util import local_to_utc

H = 3600
MON = date(2026, 11, 9)  # KW 46: five school days (no ferien catalogue in these tests)
TUE, WED, THU, FRI, SAT = (MON + timedelta(days=i) for i in range(1, 6))
NEXT_MON = MON + timedelta(days=7)
TODAY = MON + timedelta(days=4)  # Friday
NOW = local_to_utc(TODAY, dtime(20, 0))  # nothing in this week is "nachträglich"


@pytest.fixture
def period(db, no_ferien_catalog):
    return cal.create_period(db, "2026/27")


def cat_id(db, code: str) -> int:
    return db.scalar(select(Category.id).where(Category.code == code, Category.parent_id.is_(None)))


def code_of(db, entry: TimeEntry) -> str:
    return db.get(Category, entry.category_id).code


def active(db, day: date) -> list[TimeEntry]:
    return list(db.scalars(select(TimeEntry).where(TimeEntry.day == day, TimeEntry.deleted_at.is_(None))
                           .order_by(TimeEntry.created_at)))


def day_of(result_or_week: dict, d: date) -> dict:
    return next(x for x in result_or_week["days"] if x["date"] == d.isoformat())


def week(db) -> dict:
    return svc.week_view(db, MON, today=TODAY, now=NOW)


def bounded(db, day: date, start: dtime, hours: float, code: str = "VN") -> TimeEntry:
    a = local_to_utc(day, start)
    (entry,) = entries_svc.create_entry(
        db, {"start_at": a, "end_at": a + timedelta(seconds=int(hours * H)), "category_id": cat_id(db, code)},
        source="manuell", now=NOW,
    )
    return entry


def revisions(db, entry_id: str) -> list[TimeEntryRevision]:
    return list(db.scalars(select(TimeEntryRevision).where(TimeEntryRevision.entry_id == entry_id)
                           .order_by(TimeEntryRevision.revision_no)))


def set_typ(db, d: date, typ: str, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("today", TODAY)
    return svc.set_day_typ(db, d, typ, **kw)


def set_total(db, d: date, seconds: int, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("today", TODAY)
    return svc.set_day_total(db, d, seconds, **kw)


# --------------------------------------------------------------------------- view


def test_default_type_and_chip_labels(db, period):
    buss = date(2026, 11, 18)  # Buß- und Bettag: unterrichtsfrei, soll stays
    w = week(db)
    mon, sat = day_of(w, MON), day_of(w, SAT)
    assert mon["typ"] == "unterricht" and mon["typ_label"] == "Unterricht" and mon["typ_set"] is False
    assert mon["typ_changeable"] is True and mon["typ_fixed"] is None
    assert mon["typ_options"] == list(svc.DAY_TYPES)
    assert sat["typ_label"] == "Wochenende" and "krank" not in sat["typ_options"]  # no soll -> no Krank
    assert sat["typ_options"] == ["unterricht", "fortbildung", "klassenfahrt", "schulveranstaltung"]
    nxt = day_of(svc.week_view(db, NEXT_MON, today=TODAY, now=NOW), buss)
    # without ferien the automatic leave assignment uses the unterrichtsfreie day: leave of the plan, not entered by
    # hand -> shown as such, but the chip stays a chip (Krank/Fortbildung move the leave, like the calendar does)
    assert (nxt["typ_fixed"], nxt["typ_label"], nxt["typ_style"], nxt["typ_changeable"]) == (
        None, "Urlaub (autom.)", "urlaub", True)
    assert nxt["typ_hint"] and "anderen Ferientag" in nxt["typ_hint"]
    db.get(Settings, 1).urlaubs_modus = "manuell"
    db.commit()
    cal.rematerialize_all(db)
    nxt = day_of(svc.week_view(db, NEXT_MON, today=TODAY, now=NOW), buss)
    assert nxt["typ_label"] == "Unterrichtsfrei" and nxt["typ_style"] == "frei" and nxt["typ_changeable"]
    xmas = day_of(svc.week_view(db, date(2026, 12, 21), today=TODAY, now=NOW), date(2026, 12, 25))
    assert xmas["typ_label"] == "Feiertag" and xmas["typ_style"] == "feiertag"
    assert w["kf_cap"] == 10 * H  # variant B by default


def test_ferien_label_and_options_outside_a_period(db, period):
    db.get(Settings, 1).urlaubs_modus = "manuell"  # no automatic leave on the two ferien days
    db.commit()
    cal.create_ferien(db, period, name="Testferien", start=MON, end=TUE)
    assert day_of(week(db), MON)["typ_label"] == "Ferien"
    outside = svc.week_view(db, date(2030, 1, 7), today=TODAY, now=NOW)["days"][0]
    assert outside["in_period"] is False
    assert outside["typ_options"] == ["unterricht", "klassenfahrt", "schulveranstaltung"]  # absences need a year


def test_fixed_calendar_absence_is_shown_not_switched(db, period):
    cal.set_override(db, MON, absence_type="dienstbefreiung")
    mon = day_of(week(db), MON)
    assert mon["typ_fixed"] == "dienstbefreiung" and mon["typ_label"] == "Dienstbefreiung"
    assert mon["typ_changeable"] is False and mon["calendar_url"] == f"/kalender/tag/{MON.isoformat()}"
    with pytest.raises(svc.DayTypLocked) as exc:
        set_typ(db, MON, "fortbildung")
    assert exc.value.status_code == 409 and "Kalender" in exc.value.message


# --------------------------------------------------------------------------- types and switching


def test_type_chosen_before_hours_persists(db, period):
    result = set_typ(db, MON, "klassenfahrt")
    assert result["action"] == "geaendert" and result["day"]["typ"] == "klassenfahrt"
    assert result["day"]["typ_label"] == "Klassenfahrt" and result["day"]["typ_style"] == "sv"
    row = db.get(DayOverride, MON)
    assert row.erfassung_typ == "klassenfahrt" and row.absence_type is None and row.erfassung_typ_at == NOW
    assert active(db, MON) == []
    assert day_of(week(db), MON)["typ"] == "klassenfahrt"  # persisted without any entry

    set_total(db, MON, 9 * H)
    (entry,) = active(db, MON)
    assert code_of(db, entry) == "SV" and "klassenfahrt" in entry.flags and entry.tagessumme is True
    assert entry.subcategory_id is None and entry.has_bounds is False
    # repeating is harmless
    assert set_typ(db, MON, "klassenfahrt")["action"] == "unveraendert"


def test_fortbildung_sets_the_calendar_absence_and_category_f(db, period):
    set_typ(db, MON, "fortbildung")
    assert db.get(DayOverride, MON).absence_type == "fortbildung"
    assert db.get(CalendarDay, MON).absence_type == "fortbildung"
    result = set_total(db, MON, int(7.5 * H))
    (entry,) = active(db, MON)
    assert code_of(db, entry) == "F" and entry.tagessumme and entry.duration_seconds == 7.5 * H
    mon = result["day"]
    assert mon["soll"] == 8 * H and mon["delta"] == -H // 2  # the soll stays
    assert mon["typ"] == "fortbildung" and mon["typ_style"] == "fortbildung"


def test_switching_only_changes_the_tagessumme(db, period):
    detail = bounded(db, MON, dtime(14), 2, code="K")
    set_total(db, MON, 9 * H)
    ts = next(e for e in active(db, MON) if e.tagessumme)
    assert ts.duration_seconds == 7 * H and code_of(db, ts) == "AZ"
    created, value_changed = ts.created_at, ts.updated_at

    steps = [("fortbildung", "F", False), ("klassenfahrt", "SV", True), ("schulveranstaltung", "SV", False),
             ("unterricht", "AZ", False)]
    for i, (typ, code, kf) in enumerate(steps, start=1):
        result = set_typ(db, MON, typ, now=NOW + timedelta(minutes=i))
        db.refresh(ts)
        assert (code_of(db, ts), "klassenfahrt" in ts.flags) == (code, kf), typ
        assert ts.duration_seconds == 7 * H and ts.created_at == created and ts.deleted_at is None
        assert ts.updated_at == value_changed  # a type switch is no change of the value
        assert len(revisions(db, ts.id)) == i
        assert result["day"]["ist"] == 9 * H
    db.refresh(detail)
    assert code_of(db, detail) == "K" and revisions(db, detail.id) == []  # "nur Pauschalanteil"
    row = db.get(DayOverride, MON)
    assert row.absence_type is None and row.erfassung_typ is None
    assert db.get(CalendarDay, MON).absence_type is None


def test_krank_removes_the_tagessumme_and_keeps_detail(db, period):
    detail = bounded(db, MON, dtime(15), 1.5)
    set_total(db, MON, 8 * H)
    ts = next(e for e in active(db, MON) if e.tagessumme)
    before = week(db)
    assert day_of(before, MON)["delta"] == 0

    result = set_typ(db, MON, "krank")
    assert result["removed_seconds"] == 6.5 * H
    db.refresh(ts)
    assert ts.deleted_at is not None
    (rev,) = revisions(db, ts.id)
    assert rev.change_reason == "Tag als krank markiert" and rev.snapshot_json["_action"] == "delete"
    assert [e.id for e in active(db, MON)] == [detail.id]
    assert db.get(DayOverride, MON).absence_type == "krank"
    mon = result["day"]
    assert mon["krank"] is True and mon["krank_credit"] is True and mon["typ_label"] == "Krank"
    assert mon["ist"] == 1.5 * H and mon["ist_konto"] == 9.5 * H  # soll credited, recorded work on top
    assert mon["delta"] == 1.5 * H

    # back to Unterricht: absence gone, the field is free again (the removed value does not come back)
    back = set_typ(db, MON, "unterricht", now=NOW + timedelta(minutes=1))
    assert back["day"]["krank"] is False and back["day"]["ist"] == 1.5 * H
    assert db.get(DayOverride, MON).absence_type is None


def test_krank_without_hours_leaves_week_saldo_untouched(db, period):
    set_total(db, MON, 8 * H)
    set_total(db, TUE, 8 * H)
    base = week(db)["delta"]
    assert day_of(week(db), WED)["delta"] == -8 * H  # nothing recorded yet
    result = set_typ(db, WED, "krank")
    assert result["removed_seconds"] == 0
    assert day_of(result, WED)["delta"] == 0
    assert result["delta"] == base + 8 * H  # the day no longer costs its soll
    assert result["krank_gutschrift"] == 8 * H


def test_krank_without_credit_setting(db, period):
    db.get(Settings, 1).krank_neutralisiert = False
    db.commit()
    mon = set_typ(db, MON, "krank")["day"]
    assert mon["krank"] is True and mon["krank_credit"] is False and mon["delta"] == -8 * H


def test_krank_needs_a_school_day_with_soll_and_a_period(db, period):
    with pytest.raises(entries_svc.EntryValidationError):
        set_typ(db, SAT, "krank")
    with pytest.raises(entries_svc.EntryValidationError):
        set_typ(db, date(2030, 1, 7), "krank", now=NOW, today=TODAY)
    with pytest.raises(entries_svc.EntryValidationError):
        set_typ(db, MON, "urlaub")
    # Klassenfahrt on a weekend or outside every school year is fine (no calendar absence)
    assert set_typ(db, SAT, "klassenfahrt")["day"]["typ"] == "klassenfahrt"
    assert set_typ(db, date(2030, 1, 7), "schulveranstaltung")["day"]["typ"] == "schulveranstaltung"


def test_klassenfahrt_cap_variant_b_and_a(db, period):
    set_typ(db, MON, "klassenfahrt")
    mon = set_total(db, MON, 12 * H)["day"]
    assert mon["ist"] == 12 * H and mon["ist_konto"] == 10 * H
    assert mon["kf_cap"] == 10 * H and mon["kf_capped"] is True
    settings = db.get(Settings, 1)
    settings.klassenfahrt_variante = "A"
    db.commit()
    mon = day_of(week(db), MON)
    assert mon["ist_konto"] == 12 * H and mon["kf_cap"] is None and mon["kf_capped"] is False
    assert week(db)["kf_cap"] is None


# --------------------------------------------------------------------------- identity marker / protection


def test_duration_only_f_entry_from_the_form_is_not_the_tagessumme(db, period):
    (form_entry,) = entries_svc.create_entry(
        db, {"has_bounds": False, "day": MON, "duration_seconds": 2 * H, "category_id": cat_id(db, "F")},
        source="manuell", now=NOW)
    assert form_entry.tagessumme is False
    set_typ(db, MON, "fortbildung")
    state = svc.day_state(db, MON)
    assert state.tagessumme is None and state.detail_seconds == 2 * H
    set_total(db, MON, 7 * H)
    state = svc.day_state(db, MON)
    assert state.tagessumme is not None and state.tagessumme.id != form_entry.id
    assert state.tagessumme.duration_seconds == 5 * H and code_of(db, state.tagessumme) == "F"
    db.refresh(form_entry)
    assert form_entry.duration_seconds == 2 * H and form_entry.tagessumme is False


def test_editing_a_tagessumme_in_einträge_makes_it_an_ordinary_entry(db, period):
    set_total(db, MON, 6 * H)
    (ts,) = active(db, MON)
    entries_svc.update_entry(db, ts.id, {"duration_seconds": 5 * H}, now=NOW)  # value only: still the day total
    assert db.get(TimeEntry, ts.id).tagessumme is True
    entries_svc.update_entry(db, ts.id, {"category_id": cat_id(db, "K")}, now=NOW)
    assert db.get(TimeEntry, ts.id).tagessumme is False
    assert svc.day_state(db, MON).tagessumme is None


def test_old_unmarked_az_entry_is_still_found_and_marked_when_written(db, period):
    (old,) = entries_svc.create_entry(
        db, {"has_bounds": False, "day": MON, "duration_seconds": 4 * H, "category_id": cat_id(db, "AZ")},
        source="manuell", now=NOW)
    assert old.tagessumme is False and svc.day_state(db, MON).tagessumme.id == old.id
    # a type change does not take over an unmarked entry made by hand (it may be an ordinary AZ entry) ...
    set_typ(db, MON, "fortbildung")
    db.refresh(old)
    assert old.tagessumme is False and code_of(db, old) == "AZ"
    # ... a value written through the field does: then it is the day's Tagessumme and follows the type
    set_total(db, MON, 5 * H)
    db.refresh(old)
    assert old.tagessumme is True and code_of(db, old) == "F" and old.duration_seconds == 5 * H
    assert svc.day_state(db, MON).tagessumme.id == old.id


def test_types_work_with_deactivated_or_note_required_categories(db, period):
    f = db.get(Category, cat_id(db, "F"))
    sv = db.get(Category, cat_id(db, "SV"))
    f.active = False
    sv.requires_note = True
    db.commit()
    set_typ(db, MON, "fortbildung")
    set_total(db, MON, 6 * H)
    (entry,) = active(db, MON)
    assert code_of(db, entry) == "F"
    set_typ(db, MON, "klassenfahrt")
    db.refresh(entry)
    assert code_of(db, entry) == "SV" and entry.note is None
    set_typ(db, TUE, "schulveranstaltung")
    set_total(db, TUE, 5 * H)
    assert code_of(db, active(db, TUE)[0]) == "SV"
    # the normal entry form still enforces both rules
    with pytest.raises(entries_svc.EntryValidationError):
        entries_svc.create_entry(db, {"has_bounds": False, "day": WED, "duration_seconds": H,
                                      "category_id": f.id}, source="manuell", now=NOW)
    with pytest.raises(entries_svc.EntryValidationError):
        entries_svc.create_entry(db, {"has_bounds": False, "day": WED, "duration_seconds": H,
                                      "category_id": sv.id}, source="manuell", now=NOW)


# --------------------------------------------------------------------------- calendar consistency


def test_calendar_changes_keep_the_tagessumme_category_in_line(db, period):
    set_total(db, MON, 6 * H)
    (ts,) = active(db, MON)
    cal.set_override(db, MON, absence_type="fortbildung", note="Schilf")
    db.refresh(ts)
    assert code_of(db, ts) == "F" and day_of(week(db), MON)["typ"] == "fortbildung"
    cal.delete_override(db, MON)
    db.refresh(ts)
    assert code_of(db, ts) == "AZ"

    set_typ(db, TUE, "klassenfahrt")
    cal.set_override(db, TUE, note="Fahrt nach X")  # calendar input on the same day keeps the type
    assert db.get(DayOverride, TUE).erfassung_typ == "klassenfahrt"
    cal.delete_override(db, TUE)  # "Eingaben entfernen" removes calendar input only
    row = db.get(DayOverride, TUE)
    assert row is not None and row.erfassung_typ == "klassenfahrt" and row.note is None
    assert not cal.has_calendar_input(row)


# --------------------------------------------------------------------------- locks / offline


def close_month(db, ym: str) -> None:
    db.add(MonthClose(year_month=ym, closed_at=NOW, entries_hash_sha256=entries_svc.month_entries_hash(db, ym),
                      soll_seconds=0, ist_seconds=0, saldo_seconds=0, hash_history=[]))
    db.commit()


def test_closed_month_needs_a_reason(db, period):
    oct_day = date(2026, 10, 14)
    set_total(db, oct_day, 6 * H, now=local_to_utc(oct_day, dtime(20)), today=oct_day)
    close_month(db, "2026-10")
    day = day_of(svc.week_view(db, svc.monday_of(oct_day), today=TODAY, now=NOW), oct_day)
    # the chip stays tappable: the sheet asks for the reason of the correction (typ_needs_reason)
    assert day["locked"] is True and day["typ_changeable"] is True and day["typ_needs_reason"] is True
    with pytest.raises(entries_svc.EntryLocked):
        set_typ(db, oct_day, "fortbildung")
    with pytest.raises(entries_svc.EntryLocked):
        set_typ(db, date(2026, 10, 15), "klassenfahrt")  # also without a Tagessumme
    assert db.get(DayOverride, oct_day) is None
    assert set_typ(db, oct_day, "unterricht")["action"] == "unveraendert"  # nothing to do, no lock error

    set_typ(db, oct_day, "fortbildung", reason="Fortbildung vergessen")
    (entry,) = active(db, oct_day)
    assert code_of(db, entry) == "F" and "korrigiert" in entry.flags
    mc = db.get(MonthClose, "2026-10")
    assert mc.status == "nachtraeglich_korrigiert"
    set_typ(db, oct_day, "krank", reason="war krank")
    assert active(db, oct_day) == []
    assert revisions(db, entry.id)[-1].change_reason == "Tag als krank markiert: war krank"


def test_offline_device_time_last_input_wins(db, period):
    evening = local_to_utc(MON, dtime(18))
    # laptop (online) at 18:00: Klassenfahrt
    set_typ(db, MON, "klassenfahrt", now=evening)
    # phone typed "Fortbildung" offline at 12:00, arrives at 19:00: older -> ignored
    stale = set_typ(db, MON, "fortbildung", at=local_to_utc(MON, dtime(12)), now=local_to_utc(MON, dtime(19)),
                    today=MON)
    assert stale["action"] == "veraltet" and stale["day"]["typ"] == "klassenfahrt"
    # a queued type typed later than the last change wins even when it arrives late
    late = set_typ(db, MON, "schulveranstaltung", at=local_to_utc(MON, dtime(18, 30)),
                   now=local_to_utc(MON, dtime(19)), today=MON)
    assert late["action"] == "geaendert" and db.get(DayOverride, MON).erfassung_typ_at == local_to_utc(MON, dtime(18, 30))


def test_hours_typed_before_the_day_was_marked_krank_are_ignored(db, period):
    set_typ(db, TUE, "krank", now=local_to_utc(TUE, dtime(9)))
    stale = set_total(db, TUE, 8 * H, at=local_to_utc(TUE, dtime(7)), now=local_to_utc(TUE, dtime(10)), today=TUE)
    assert stale["action"] == "veraltet" and active(db, TUE) == []
    with pytest.raises(entries_svc.EntryValidationError):
        set_typ(db, MON, "fortbildung", at=NOW + timedelta(hours=1))  # device clock in the future


def test_mark_legacy_tagessummen_picks_the_newest_per_day(db, period):
    az = cat_id(db, "AZ")
    ids = []
    for minute in (0, 1):
        (e,) = entries_svc.create_entry(db, {"has_bounds": False, "day": MON, "duration_seconds": H,
                                             "category_id": az}, source="manuell", now=NOW + timedelta(minutes=minute))
        ids.append(e.id)
    assert svc.mark_legacy_tagessummen(db) == 1
    db.expire_all()
    assert [db.get(TimeEntry, i).tagessumme for i in ids] == [False, True]


# --------------------------------------------------------------------------- review fixes (regressions)

BUSS = date(2026, 11, 18)  # Buß- und Bettag: unterrichtsfrei; the default leave plan puts Urlaub on it


def bounded_flags(db, day: date, start: dtime, hours: float, flags: list[str], code: str = "SV") -> TimeEntry:
    a = local_to_utc(day, start)
    (entry,) = entries_svc.create_entry(
        db, {"start_at": a, "end_at": a + timedelta(seconds=int(hours * H)), "category_id": cat_id(db, code),
             "flags": flags},
        source="manuell", now=NOW,
    )
    return entry


def test_calendar_krank_removes_the_tagessumme_like_the_chip(db, period):
    bounded(db, MON, dtime(8), 2, "K")
    set_typ(db, MON, "klassenfahrt")
    set_total(db, MON, 12 * H)  # Tagessumme 10 h (SV + klassenfahrt) next to 2 h K
    (ts,) = [e for e in active(db, MON) if e.tagessumme]
    cal.set_override(db, MON, absence_type="krank")
    mon = day_of(week(db), MON)
    assert mon["krank"] and mon["tagessumme_seconds"] == 0 and mon["ist"] == 2 * H  # only the K entry is left
    assert mon["ist_konto"] == mon["soll"] + 2 * H  # credited soll + individual entries, no double counting
    assert db.get(DayOverride, MON).erfassung_typ is None  # no Klassenfahrt left behind next to krank
    assert db.get(TimeEntry, ts.id).deleted_at is not None
    assert revisions(db, ts.id)[-1].change_reason == svc.KRANK_REASON
    # a sick note over a range does the same
    set_total(db, TUE, 8 * H)
    set_total(db, WED, 7 * H)
    cal.set_override_range(db, TUE, WED, absence_type="krank")
    assert active(db, TUE) == [] and active(db, WED) == []
    # back to no absence: the hours do not come back (same as the chip)
    cal.delete_override(db, TUE)
    assert day_of(week(db), TUE)["ist"] == 0


def test_calendar_krank_in_a_closed_month_needs_the_reason(db, period):
    oct_day = date(2026, 10, 14)
    set_total(db, oct_day, 6 * H, now=local_to_utc(oct_day, dtime(20)), today=oct_day)
    close_month(db, "2026-10")
    with pytest.raises(cal.CalendarLocked):
        cal.set_override(db, oct_day, absence_type="krank")
    assert len(active(db, oct_day)) == 1
    cal.set_override(db, oct_day, absence_type="krank", reason="Krankmeldung nachgereicht")
    (ts,) = db.scalars(select(TimeEntry).where(TimeEntry.day == oct_day)).all()
    assert ts.deleted_at is not None
    assert revisions(db, ts.id)[-1].change_reason == f"{svc.KRANK_REASON}: Krankmeldung nachgereicht"


def test_automatic_leave_day_takes_krank_and_fortbildung(db, period):
    nxt = date(2026, 11, 16)
    before = day_of(svc.week_view(db, nxt, today=TODAY, now=NOW), BUSS)
    assert before["absence"] == "urlaub" and before["typ_changeable"] and "fortbildung" in before["typ_options"]
    result = set_typ(db, BUSS, "fortbildung")
    assert result["action"] == "geaendert"
    assert day_of(svc.week_view(db, nxt, today=TODAY, now=NOW), BUSS)["absence"] == "fortbildung"  # leave moved on
    set_typ(db, BUSS, "krank")
    assert day_of(svc.week_view(db, nxt, today=TODAY, now=NOW), BUSS)["krank"] is True
    set_typ(db, BUSS, "unterricht")
    assert day_of(svc.week_view(db, nxt, today=TODAY, now=NOW), BUSS)["typ_label"] == svc.AUTO_LEAVE_LABEL


def test_manually_assigned_leave_stays_protected(db, period):
    db.get(Settings, 1).urlaubs_modus = "manuell"
    db.commit()
    cal.rematerialize_all(db)
    cal.set_override(db, BUSS, urlaub_manuell=True)
    day = day_of(svc.week_view(db, date(2026, 11, 16), today=TODAY, now=NOW), BUSS)
    assert (day["typ_fixed"], day["typ_label"], day["typ_changeable"]) == ("urlaub", "Urlaub", False)
    with pytest.raises(svc.DayTypLocked):
        set_typ(db, BUSS, "krank")


def test_klassenfahrt_type_caps_a_day_made_of_individual_entries(db, period):
    set_typ(db, MON, "klassenfahrt")
    bounded(db, MON, dtime(6), 6)
    bounded(db, MON, dtime(13), 6)  # 12 h detail, no Tagessumme (nothing carries the flag)
    mon = day_of(week(db), MON)
    assert mon["ist"] == 12 * H and mon["ist_konto"] == 10 * H  # the type marks the day as trip day
    assert mon["kf_capped"] is True and mon["kf_counted"] == 10 * H
    # total = detail sum: the Tagessumme goes, the cap stays (it does not hang on the Tagessumme's flag)
    set_total(db, MON, 14 * H)
    assert day_of(week(db), MON)["ist_konto"] == 10 * H
    set_total(db, MON, 12 * H)
    mon = day_of(week(db), MON)
    assert mon["tagessumme_seconds"] == 0 and mon["ist_konto"] == 10 * H and mon["kf_capped"] is True
    # another type removes the cap
    set_typ(db, MON, "schulveranstaltung")
    mon = day_of(week(db), MON)
    assert mon["ist_konto"] == 12 * H and mon["kf_capped"] is False


def test_klassenfahrt_hint_shows_what_counts_with_nachtbereitschaft(db, period):
    bounded_flags(db, MON, dtime(0), 6, ["nachtbereitschaft"])
    set_typ(db, MON, "klassenfahrt")
    set_total(db, MON, 14 * H)  # Tagessumme 8 h; variant B drops the 6 h standby -> 8 h, below the cap
    mon = day_of(week(db), MON)
    assert mon["ist_konto"] == 8 * H and mon["nb_seconds"] == 6 * H
    assert mon["kf_capped"] is True and mon["kf_counted"] == 8 * H  # "zählt 8:00", not "max. 10:00"
    db.get(Settings, 1).klassenfahrt_variante = "A"
    db.commit()
    mon = day_of(week(db), MON)
    assert mon["kf_capped"] is False and mon["nb_seconds"] == 0 and mon["ist_konto"] == 14 * H


def test_queued_input_older_than_a_calendar_change_is_stale(db, period):
    cal.set_override(db, MON, absence_type="krank")  # in the calendar, "now"
    earlier = NOW - timedelta(hours=3)
    assert set_typ(db, MON, "unterricht", at=earlier)["action"] == "veraltet"
    assert db.get(DayOverride, MON).absence_type == "krank"
    assert set_total(db, MON, 8 * H, at=earlier)["action"] == "veraltet"
    # removing the absence in the calendar is stamped too when the row stays (it holds a type of "Erfassen") ...
    set_typ(db, TUE, "klassenfahrt", now=earlier - timedelta(hours=1))
    cal.set_override(db, TUE, absence_type="fortbildung")
    cal.delete_override(db, TUE)
    row = db.get(DayOverride, TUE)
    assert row is not None and row.absence_type is None and row.erfassung_typ_at > earlier
    assert set_typ(db, TUE, "fortbildung", at=earlier)["action"] == "veraltet"
    # ... but no row is kept just for the stamp
    cal.set_override(db, WED, absence_type="fortbildung")
    cal.set_override(db, WED)
    assert db.get(DayOverride, WED) is None


def test_hand_made_az_entry_is_not_retyped_by_a_calendar_edit(db, period):
    set_typ(db, MON, "fortbildung")
    (e,) = entries_svc.create_entry(db, {"has_bounds": False, "day": MON, "duration_seconds": 2 * H,
                                         "category_id": cat_id(db, "AZ"), "note": "von Hand"},
                                    source="manuell", now=NOW)
    cal.set_override(db, MON, absence_type="fortbildung", note="Notiz")  # any calendar edit runs the sync
    set_typ(db, MON, "fortbildung")  # a repeated tap runs the repair
    db.expire_all()
    e = db.get(TimeEntry, e.id)
    assert code_of(db, e) == "AZ" and e.tagessumme is False and len(revisions(db, e.id)) <= 1


def test_adopting_the_leave_plan_keeps_the_erfassen_type(db, period):
    db.get(Settings, 1).urlaubs_modus = "manuell"
    db.commit()
    cal.rematerialize_all(db)
    set_typ(db, BUSS, "klassenfahrt")
    set_total(db, BUSS, 9 * H)
    cal.adopt_leave_plan(db, period, "proportional")
    cal.adopt_leave_plan(db, period, "proportional")
    row = db.get(DayOverride, BUSS)
    assert row is not None and row.erfassung_typ == "klassenfahrt"
    (ts,) = active(db, BUSS)
    assert code_of(db, ts) == "SV" and "klassenfahrt" in ts.flags


def test_calendar_writes_take_the_erfassen_write_lock():
    import threading

    seen: list[bool] = []

    @cal._serialised
    def probe() -> None:
        t = threading.Thread(target=lambda: seen.append(svc._WRITE_LOCK.acquire(blocking=False)))
        t.start()
        t.join()

    probe()
    assert seen == [False]  # held while a calendar change runs
    for fn in (cal.set_override, cal.set_override_range, cal.delete_override, cal.adopt_leave_plan):
        assert hasattr(fn, "__wrapped__")
    with svc._WRITE_LOCK, svc._WRITE_LOCK:  # re-entrant: a type change runs set_override inside the lock
        pass


def test_input_time_uses_the_waiting_time_of_the_queue():
    now = NOW
    slow = now - timedelta(minutes=3)  # device clock 3 min slow, request sent right away
    assert svc.input_time(slow.isoformat(), now, sent_at=slow.isoformat()) == (now, False)
    assert svc.input_time(slow.isoformat(), now) == (slow, True)  # without the header: counted as delayed
    queued = now - timedelta(hours=2)  # typed offline, sent 1 h later by a clock that is 1 h slow
    when, delayed = svc.input_time(queued.isoformat(), now, sent_at=(queued + timedelta(hours=1)).isoformat())
    assert when == now - timedelta(hours=1) and delayed is True
    assert svc.input_time(slow.isoformat(), now, sent_at="kaputt") == (slow, True)  # bad header: ignored


def test_typ_options_outside_a_school_year_list_the_disabled_types(db, period):
    outside = svc.week_view(db, date(2030, 1, 7), today=TODAY, now=NOW)["days"]
    assert outside[0]["typ_disabled"] == ["krank", "fortbildung"]
    assert outside[5]["typ_disabled"] == ["fortbildung"]  # Saturday: no Krank anyway
    assert day_of(week(db), MON)["typ_disabled"] == []
