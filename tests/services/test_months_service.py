"""services/months.py + services/reports.py: month overview (T11/T12), close (T20, locks, open items),
Mehrarbeit status, month and year report contents."""

from __future__ import annotations

import re
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import (
    CalendarDay,
    Category,
    DayConfirmation,
    ImportBatch,
    MehrarbeitMonth,
    MonthClose,
    TimeEntry,
    TimeEntryRevision,
)
from app.services import calendar_service as cal
from app.services import entries as entries_svc
from app.services import months
from app.services import reports
from app.services import settings_service
from app.util import local_to_utc

H = 3600
EXAMPLE_ICS = Path(__file__).resolve().parents[2] / "examples" / "ferien_bayern_BEISPIEL.ics"
TODAY = date(2026, 10, 29)
SEPT = "2026-09"


@pytest.fixture
def year(db):
    period = cal.create_period(db, "2026/27")
    cal.commit_ics(db, period, EXAMPLE_ICS.read_bytes())
    return period


def cat_id(db, code: str) -> int:
    return db.scalar(select(Category.id).where(Category.code == code))


def add(db, day: date, start: dtime, minutes: int, code: str = "VN", *, flags=(), source="manuell", **extra):
    """Create an entry through services/entries.py, 'recorded' the same evening."""
    start_at = local_to_utc(day, start)
    data = {
        "start_at": start_at,
        "end_at": start_at + timedelta(minutes=minutes),
        "category_id": cat_id(db, code),
        "flags": list(flags),
        **extra,
    }
    return entries_svc.create_entry(db, data, source=source, now=local_to_utc(day, dtime(21, 0)))[0]


def school_days(db, start: date, end: date) -> list[date]:
    return list(db.scalars(
        select(CalendarDay.date).where(CalendarDay.date.between(start, end), CalendarDay.day_type == "schultag")
        .order_by(CalendarDay.date)
    ))


def fill_month(db, start: date, end: date, minutes: int = 8 * 60, code: str = "VN") -> list[date]:
    days = school_days(db, start, end)
    for d in days:
        add(db, d, dtime(7, 30), minutes, code)
    return days


@pytest.fixture
def september(db, year):
    """Every school day of September 2026 (15.–30.09.) with 8 h recorded."""
    return fill_month(db, date(2026, 9, 1), date(2026, 9, 30))


# =========================================================================== helpers


def test_parse_ym_and_labels():
    assert months.parse_ym("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert months.parse_ym("2026-12") == (date(2026, 12, 1), date(2026, 12, 31))
    assert months.month_label("2027-03") == "März 2027"
    for bad in ("2026-13", "2026-1", "26-01", "abc", ""):
        with pytest.raises(months.MonthValidationError):
            months.parse_ym(bad)


def test_freizeitausgleich_frist_is_end_of_third_following_month():
    assert months.freizeitausgleich_frist("2026-10") == date(2027, 1, 31)
    assert months.freizeitausgleich_frist("2026-11") == date(2027, 2, 28)
    assert months.freizeitausgleich_frist("2026-12") == date(2027, 3, 31)


def test_month_without_period_is_not_found(db):
    with pytest.raises(months.MonthNotFound, match="kein Schuljahr"):
        months.month_overview(db, SEPT, today=TODAY)


# =========================================================================== overview


def test_overview_figures_match_ledger(db, september):
    ov = months.month_overview(db, SEPT, today=TODAY)
    assert ov.label == "September 2026"
    assert ov.status == "offen" and not ov.is_closed
    assert len(september) == 12
    # soll: 12 school days + Mon 14.09. (last ferien day, no leave) = 13 working days × 8 h
    assert ov.soll == sum(r.soll for r in ov.ledger)
    assert ov.ist == 12 * 8 * H
    assert ov.saldo == ov.ist - ov.soll
    assert ov.entries_count == 12
    assert ov.stichtag == date(2026, 9, 30)
    assert not ov.laufend and not ov.zukunft
    # cumulative saldo = saldo of the ledger up to 30.09.
    from app.services import ledger as ledger_svc

    ctx = ledger_svc.load_context(db, today=date(2026, 9, 30))
    assert ov.saldo_kumuliert == ctx.saldo_heute
    assert sum(w.ist for w in ov.weeks) == ov.ist
    assert [c.code for c in ov.categories] == ["VN"] and ov.categories[0].share == pytest.approx(1.0)
    assert ov.open_items == [] and ov.can_close


def test_overview_ferienarbeit_and_weekend_work(db, year):
    add(db, date(2026, 8, 10), dtime(9), 120)  # Sommerferien weekday (urlaub) -> Ferienarbeit
    add(db, date(2026, 8, 15), dtime(9), 60)  # Saturday + Mariä Himmelfahrt inside the ferien
    ov = months.month_overview(db, "2026-08", today=TODAY)
    assert ov.ferienarbeit == 3 * H
    assert ov.wochenend_feiertag_arbeit == H


def test_running_month_covers_days_until_today(db, year):
    add(db, date(2026, 10, 28), dtime(8), 300)
    ov = months.month_overview(db, "2026-10", today=TODAY)
    assert ov.laufend and ov.stichtag == TODAY
    assert ov.soll < ov.soll_monat_gesamt
    assert ov.ist == 5 * H
    kinds = {i.kind for i in ov.open_items}
    assert months.ITEM_MONAT_LAEUFT in kinds
    assert not ov.can_close and not ov.can_force


def test_future_month_has_no_figures(db, year):
    ov = months.month_overview(db, "2027-02", today=TODAY)
    assert ov.zukunft and ov.soll == 0 and ov.ist == 0 and ov.entries_count == 0


def test_t11_three_ustd_vertretung_not_compensable(db, year):
    d = date(2026, 10, 5)
    add(db, d, dtime(8), 135, "U", flags=["vertretung_angeordnet"])  # 3 UStd
    ov = months.month_overview(db, "2026-10", today=TODAY)
    assert ov.mehrarbeit.ustd_total == 3.0
    assert ov.mehrarbeit.ustd_ausgleichsfaehig == 0.0
    assert ov.mehrarbeit.frist is None
    assert "Bagatellgrenze" in ov.mehrarbeit.hinweis


def test_t12_four_ustd_vertretung_compensable_with_frist(db, year):
    d = date(2026, 10, 5)
    add(db, d, dtime(8), 90, "U", flags=["vertretung_angeordnet"])  # 2 UStd
    add(db, d + timedelta(days=1), dtime(8), 90, "U", flags=["vertretung_genehmigt"])  # 2 UStd
    ov = months.month_overview(db, "2026-10", today=TODAY)
    ma = ov.mehrarbeit
    assert (ma.ustd_angeordnet, ma.ustd_genehmigt, ma.ustd_total, ma.ustd_ausgleichsfaehig) == (2.0, 2.0, 4.0, 4.0)
    assert ma.frist == date(2027, 1, 31) and not ma.frist_abgelaufen
    assert "bis 31.01.2027" in ma.hinweis
    row = db.get(MehrarbeitMonth, "2026-10")  # maintained row
    assert row is not None and row.ustd_ausgleichsfaehig == 4.0 and row.status == "offen"


def test_mehrarbeit_frist_expired_and_status_update(db, year):
    for i in range(4):
        add(db, date(2026, 10, 5) + timedelta(days=i), dtime(8), 45, "U", flags=["vertretung_angeordnet"])
    late = date(2027, 2, 2)
    ov = months.month_overview(db, "2026-10", today=late)
    assert ov.mehrarbeit.frist_abgelaufen and "abgelaufen" in ov.mehrarbeit.hinweis

    info = months.update_mehrarbeit(db, "2026-10", status="beantragt", note="Antrag 01.02.", today=late)
    assert info.status == "beantragt" and info.note == "Antrag 01.02." and "beantragt" in info.hinweis
    info = months.update_mehrarbeit(db, "2026-10", status="ausgeglichen", ustd_vertretung_erhalten=1.0, today=late)
    assert info.status == "ausgeglichen" and info.ustd_vertretung_erhalten == 1.0
    assert info.note == "Antrag 01.02."  # untouched when not passed
    # recomputation keeps the status
    ov = months.month_overview(db, "2026-10", today=late)
    assert ov.mehrarbeit.status == "ausgeglichen" and ov.mehrarbeit.hinweis == "Mehrarbeit ausgeglichen."
    with pytest.raises(months.MonthValidationError):
        months.update_mehrarbeit(db, "2026-10", status="erledigt")


def test_mehrarbeit_row_updated_to_zero_when_entries_go_away(db, year):
    entries = [add(db, date(2026, 10, 5) + timedelta(days=i), dtime(8), 45, "U", flags=["vertretung_angeordnet"])
               for i in range(4)]
    months.month_overview(db, "2026-10", today=TODAY)
    for e in entries:
        entries_svc.delete_entry(db, e.id)
    months.month_overview(db, "2026-10", today=TODAY)
    row = db.get(MehrarbeitMonth, "2026-10")
    assert row.ustd_ausgleichsfaehig == 0.0 and row.ustd_angeordnet == 0.0


# =========================================================================== open items


def test_open_items_school_day_without_entries(db, year):
    days = school_days(db, date(2026, 9, 1), date(2026, 9, 30))
    for d in days[1:]:
        add(db, d, dtime(8), 60)
    ov = months.month_overview(db, SEPT, today=TODAY)
    missing = [i for i in ov.open_items if i.kind == months.ITEM_OHNE_EINTRAG]
    assert days[0] == date(2026, 9, 15)  # 14.09. is still a ferien day
    assert [i.date for i in missing] == [days[0]]
    assert "Di 15.09.2026" in missing[0].message
    assert not ov.can_close and ov.can_force


def test_open_items_ignore_absences_and_confirmed_days(db, year):
    days = school_days(db, date(2026, 9, 15), date(2026, 9, 30))
    for d in days[2:]:
        add(db, d, dtime(8), 60)
    cal.set_override(db, days[0], absence_type="krank")
    db.add(DayConfirmation(date=days[1], deviations_json={"1": "ausfall"}))
    db.commit()
    ov = months.month_overview(db, SEPT, today=TODAY)
    assert all(i.date not in (days[0], days[1]) for i in ov.open_items)


def test_open_items_unconfirmed_suggestions(db, september):
    d = september[0]
    start_at = local_to_utc(d, dtime(14, 0))
    entries_svc.create_entry(
        db, {"start_at": start_at, "end_at": start_at + timedelta(minutes=45), "category_id": cat_id(db, "U"),
             "status": "vorschlag"},
        source="stundenplan", now=start_at,
    )
    ov = months.month_overview(db, SEPT, today=TODAY)
    assert [i.kind for i in ov.open_items] == [months.ITEM_VORSCHLAG]
    assert ov.open_items[0].date == d
    db.add(DayConfirmation(date=d))
    db.commit()
    assert months.month_overview(db, SEPT, today=TODAY).open_items == []


def test_running_timer_is_blocking(db, september):
    from app.services import timer

    timer.start(db, cat_id(db, "K"), started_at=local_to_utc(date(2026, 9, 30), dtime(20)),
                now=local_to_utc(date(2026, 9, 30), dtime(20, 1)))
    ov = months.month_overview(db, SEPT, today=TODAY)
    item = next(i for i in ov.open_items if i.kind == months.ITEM_TIMER_LAEUFT)
    assert item.blocking and not ov.can_force
    with pytest.raises(months.MonthOpenItems, match="Timer läuft"):
        months.close_month(db, SEPT, force=True, note="egal", today=TODAY)


# =========================================================================== close


def test_close_month_hash_lock_and_report_file(db, september, settings_env):
    now = datetime(2026, 10, 29, 18, 0)
    result = months.close_month(db, SEPT, today=TODAY, now=now)
    mc = db.get(MonthClose, SEPT)
    assert mc is result.month_close
    assert mc.entries_hash_sha256 == entries_svc.month_entries_hash(db, SEPT)
    assert re.fullmatch(r"[0-9a-f]{64}", mc.entries_hash_sha256)
    assert mc.closed_at == now and mc.status == "abgeschlossen" and mc.hash_history == []
    ov = result.overview
    assert (mc.soll_seconds, mc.ist_seconds, mc.saldo_seconds) == (ov.soll, ov.ist, ov.saldo)
    assert mc.ist_seconds == 12 * 8 * H
    # entries locked, updated_at untouched by the lock
    entries = db.scalars(select(TimeEntry).where(TimeEntry.day.between(date(2026, 9, 1), date(2026, 9, 30)))).all()
    assert entries and all(e.locked for e in entries)
    assert all(e.updated_at == e.created_at for e in entries)
    # report file
    assert mc.report_file == "reports/2026-09.html"
    path = settings_env.data_dir / "reports" / "2026-09.html"
    assert result.report_path == path and path.is_file()
    html = path.read_text(encoding="utf-8")
    assert mc.entries_hash_sha256 in html
    assert "vorläufig" not in html.split("<body", 1)[1].split("Kennzahlen")[0]
    assert "abgeschlossen" in html
    assert "<script" not in html and "<link" not in html and "@page" in html  # self-contained, print CSS
    assert "Bericht jetzt exportieren" in result.hinweis
    ov2 = months.month_overview(db, SEPT, today=TODAY)
    assert ov2.is_closed and ov2.status_label == "abgeschlossen" and not ov2.can_close


def test_t20_same_data_same_hash_change_different_hash(db, september):
    before = entries_svc.month_entries_hash(db, SEPT)
    assert entries_svc.month_entries_hash(db, SEPT) == before
    months.close_month(db, SEPT, today=TODAY)
    mc = db.get(MonthClose, SEPT)
    assert mc.entries_hash_sha256 == before  # locking does not change the hash
    assert entries_svc.month_entries_hash(db, SEPT) == before

    entry = db.scalars(select(TimeEntry).where(TimeEntry.day == september[0])).one()
    with pytest.raises(entries_svc.EntryLocked):
        entries_svc.update_entry(db, entry.id, {"note": "x"})
    entries_svc.update_entry(db, entry.id, {"duration_seconds": 7 * H}, reason="Tippfehler korrigiert")
    db.refresh(mc)
    assert mc.entries_hash_sha256 != before
    assert mc.status == "nachtraeglich_korrigiert"
    assert mc.hash_history[0]["hash"] == before
    # the stored hash always equals the hash of the current data
    entries_svc.update_entry(db, entry.id, {"duration_seconds": 8 * H}, reason="zurück")
    db.refresh(mc)
    assert entries_svc.month_entries_hash(db, SEPT) == mc.entries_hash_sha256


def test_t20_deletion_changes_hash(db, september):
    months.close_month(db, SEPT, today=TODAY)
    mc = db.get(MonthClose, SEPT)
    old = mc.entries_hash_sha256
    entry = db.scalars(select(TimeEntry).where(TimeEntry.day == september[3])).one()
    entries_svc.delete_entry(db, entry.id, reason="doppelt erfasst")
    db.refresh(mc)
    assert mc.entries_hash_sha256 != old


def test_close_refuses_with_open_items_unless_forced_with_note(db, year, settings_env):
    days = school_days(db, date(2026, 9, 15), date(2026, 9, 30))
    for d in days[1:]:
        add(db, d, dtime(8), 60)
    with pytest.raises(months.MonthOpenItems) as exc:
        months.close_month(db, SEPT, today=TODAY)
    assert exc.value.status_code == 409 and exc.value.items
    assert db.get(MonthClose, SEPT) is None
    assert not any(e.locked for e in db.scalars(select(TimeEntry)))
    with pytest.raises(months.MonthValidationError, match="begründen"):
        months.close_month(db, SEPT, force=True, today=TODAY)
    with pytest.raises(months.MonthValidationError):
        months.close_month(db, SEPT, force=True, note="   ", today=TODAY)

    result = months.close_month(db, SEPT, force=True, note="Tag war Wandertag, Eintrag fehlt", today=TODAY)
    assert result.forced_items
    html = (settings_env.data_dir / result.month_close.report_file).read_text(encoding="utf-8")
    assert "Abschluss trotz offener Punkte" in html
    assert "Wandertag" in html and "Schultag ohne Einträge" in html
    # Stored in the database as well (migration 0002), shown by the overview.
    mc = db.get(MonthClose, SEPT)
    assert mc.note == "Tag war Wandertag, Eintrag fehlt"
    assert mc.forced_items and mc.forced_items[0]["kind"] == result.forced_items[0].kind
    assert mc.forced_items[0]["date"] == days[0].isoformat()
    abschluss = months.month_overview(db, SEPT, today=TODAY, sync=False).abschluss
    assert abschluss.note == mc.note and abschluss.forced_items == mc.forced_items


def test_close_twice_and_future_and_running_month_refused(db, september):
    months.close_month(db, SEPT, today=TODAY)
    with pytest.raises(months.MonthConflict, match="bereits abgeschlossen"):
        months.close_month(db, SEPT, today=TODAY)
    with pytest.raises(months.MonthConflict, match="Zukunft"):
        months.close_month(db, "2026-12", today=TODAY)
    with pytest.raises(months.MonthOpenItems, match="noch nicht zu Ende"):
        months.close_month(db, "2026-10", force=True, note="x", today=TODAY)


def test_close_on_last_day_of_month_allowed(db, september):
    result = months.close_month(db, SEPT, today=date(2026, 9, 30))
    assert result.month_close.status == "abgeschlossen"


def test_close_keeps_report_of_an_older_database(db, september, settings_env):
    path = settings_env.data_dir / "reports" / "2026-09.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("alt", encoding="utf-8")
    months.close_month(db, SEPT, today=TODAY, now=datetime(2026, 10, 29, 10, 0))
    olds = list(path.parent.glob("2026-09_alt_*.html"))
    assert len(olds) == 1 and olds[0].read_text(encoding="utf-8") == "alt"
    assert path.read_text(encoding="utf-8") != "alt"


def test_entries_created_after_close_need_reason_and_are_locked(db, september):
    months.close_month(db, SEPT, today=TODAY)
    d = september[0]
    with pytest.raises(entries_svc.EntryLocked):
        add(db, d, dtime(18), 30)
    start_at = local_to_utc(d, dtime(18))
    (entry,) = entries_svc.create_entry(
        db, {"start_at": start_at, "end_at": start_at + timedelta(minutes=30), "category_id": cat_id(db, "K")},
        source="manuell", reason="nachgetragen", now=local_to_utc(TODAY, dtime(9)),
    )
    assert entry.locked


def test_list_months(db, september):
    months.close_month(db, SEPT, today=TODAY)
    rows = months.list_months(db, today=TODAY)
    assert [r.year_month for r in rows][:3] == ["2026-08", "2026-09", "2026-10"]
    assert len(rows) == 12
    sept = next(r for r in rows if r.year_month == SEPT)
    mc = db.get(MonthClose, SEPT)
    assert sept.status == "abgeschlossen" and sept.hash == mc.entries_hash_sha256
    assert sept.ist == mc.ist_seconds and sept.soll == mc.soll_seconds
    octo = next(r for r in rows if r.year_month == "2026-10")
    assert octo.laufend and octo.status == "offen"
    assert next(r for r in rows if r.year_month == "2027-01").zukunft
    # cumulative saldo is consistent
    assert sept.saldo_kumuliert == rows[0].saldo + sept.saldo


def test_overview_to_dict_is_json_ready(db, september):
    import json

    from fastapi.encoders import jsonable_encoder

    data = months.overview_to_dict(months.month_overview(db, SEPT, today=TODAY))
    text = json.dumps(jsonable_encoder(data))
    assert '"saldo_hm"' in text and data["eu_mittel"]["ampel"] in ("gruen", "gelb", "rot")


# =========================================================================== reports


def test_month_report_contains_header_entries_revisions_and_hash(db, september):
    settings_service.update_settings(db, name="Erika Muster", schule="Grundschule Beispielstadt")
    entry = db.scalars(select(TimeEntry).where(TimeEntry.day == september[0])).one()
    entries_svc.update_entry(db, entry.id, {"note": "Elternbrief", "duration_seconds": 7 * H},
                             reason="Ende korrigiert")
    gone = db.scalars(select(TimeEntry).where(TimeEntry.day == september[1])).one()
    entries_svc.delete_entry(db, gone.id, reason="falscher Tag")
    batch = ImportBatch(filename="alt_2025.xlsx", created_at=datetime(2026, 10, 20, 10))
    db.add(batch)
    db.commit()
    start_at = local_to_utc(september[2], dtime(15))
    entries_svc.create_entry(
        db, {"day": september[2], "has_bounds": False, "duration_seconds": "2,5", "category_id": cat_id(db, "K"),
             "import_batch_id": batch.id},
        source="import", now=start_at,
    )
    months.close_month(db, SEPT, force=True, note="Tag frei", today=TODAY, now=datetime(2026, 10, 29, 17, 0))
    html = reports.render_month_report(db, SEPT, today=TODAY)
    mc = db.get(MonthClose, SEPT)

    assert "Monatsbericht September 2026" in html
    assert "Erika Muster" in html and "Grundschule Beispielstadt" in html
    assert "2026/27" in html and "Teilzeitfaktor" in html
    assert html.count(mc.entries_hash_sha256) >= 2  # header + footer
    assert "0.1.0" in html and "Rechenregeln-Version 1" in html
    # all entries (incl. the deleted one, struck through) and their sources
    for d in september:
        assert d.strftime("%d.%m.%Y") in html
    assert html.count('<tr class="deleted">') == 1
    assert "Import vom 20.10.2026 aus Datei alt_2025.xlsx" in html
    assert "2:30" in html and "ohne Zeitgrenzen" in html
    # change log
    assert "Ende korrigiert" in html and "falscher Tag" in html
    assert "Dauer: 8:00 → 7:00" in html
    assert "Notiz: – → Elternbrief" in html
    assert "Gelöscht" in html
    # key figure sections
    for heading in ("Kennzahlen", "Kategorien", "Wochenübersicht", "Mehrarbeit", "Vollständige Eintragsliste",
                    "Änderungsprotokoll"):
        assert heading in html


def test_month_report_preliminary_for_open_month(db, september):
    html = reports.render_month_report(db, SEPT, today=TODAY)
    assert "vorläufig" in html
    assert entries_svc.month_entries_hash(db, SEPT) in html


def test_month_report_escapes_user_text(db, september):
    entry = db.scalars(select(TimeEntry).where(TimeEntry.day == september[0])).one()
    entries_svc.update_entry(db, entry.id, {"note": "<script>alert(1)</script>"})
    html = reports.render_month_report(db, SEPT, today=TODAY)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_month_report_shows_hash_history_after_correction(db, september):
    months.close_month(db, SEPT, today=TODAY)
    old = db.get(MonthClose, SEPT).entries_hash_sha256
    entry = db.scalars(select(TimeEntry).where(TimeEntry.day == september[0])).one()
    entries_svc.update_entry(db, entry.id, {"duration_seconds": 6 * H}, reason="Korrektur nach Abschluss")
    html = reports.render_month_report(db, SEPT, today=TODAY)
    assert "nachträglich korrigiert" in html.lower()
    assert old in html and db.get(MonthClose, SEPT).entries_hash_sha256 in html


def test_revision_rows_include_split_note(db, year):
    start = local_to_utc(date(2026, 9, 30), dtime(22, 30))
    entries_svc.create_entry(db, {"start_at": start, "end_at": start + timedelta(hours=2, minutes=45),
                                  "category_id": cat_id(db, "K")}, source="manuell", now=start + timedelta(hours=3))
    rows = reports.revision_rows(db, date(2026, 9, 1), date(2026, 9, 30))
    assert any("An Mitternacht geteilt" in r["detail"] for r in rows)
    rows_oct = reports.revision_rows(db, date(2026, 10, 1), date(2026, 10, 31))
    assert any("Teil 2 von 2" in r["detail"] for r in rows_oct)


def test_revision_rows_follow_entries_moved_out_of_month(db, september):
    entry = db.scalars(select(TimeEntry).where(TimeEntry.day == september[-1])).one()
    new_start = local_to_utc(date(2026, 10, 1), dtime(7, 30))
    entries_svc.update_entry(db, entry.id, {"start_at": new_start, "end_at": new_start + timedelta(hours=8)},
                             reason="falscher Tag")
    rows = reports.revision_rows(db, date(2026, 9, 1), date(2026, 9, 30))
    assert any(r["reason"] == "falscher Tag" for r in rows)
    assert db.scalar(select(TimeEntryRevision).where(TimeEntryRevision.entry_id == entry.id)) is not None


def test_year_report_contents(db, september):
    add(db, date(2026, 8, 10), dtime(9), 120)
    add(db, date(2026, 10, 7), dtime(8), 180, "SV", flags=["klassenfahrt"])
    add(db, date(2026, 10, 7), dtime(22), 480, "SV", flags=["klassenfahrt", "nachtbereitschaft"])
    for i in range(4):
        add(db, date(2026, 10, 12) + timedelta(days=i), dtime(8), 45, "U", flags=["vertretung_angeordnet"])
    months.close_month(db, SEPT, today=TODAY)
    html = reports.render_year_report(db, today=TODAY)
    assert "Jahresbericht 2026/27" in html
    assert "<svg" in html and "<polyline" in html and "stroke-dasharray" in html  # chart incl. forecast
    assert "<script" not in html
    assert "Urlaub = Sommerferien" in html and "Urlaub proportional verteilt" in html
    assert "(maßgeblich)" in html
    assert "Sommerferien 2026" in html and "Herbstferien 2026" in html  # Ferienarbeit je Abschnitt
    assert "A – alle Stunden inkl. Nachtbereitschaft" in html and "B – gekappt" in html
    assert "Urlaubszuordnung" in html and "30 von 30 Tagen" in html
    assert "Oktober 2026" in html and "abgeschlossen" in html
    assert db.get(MonthClose, SEPT).entries_hash_sha256[:16] in html
    assert reports.range_hash(reports.hashed_entries(db, date(2026, 8, 1), date(2027, 7, 31))) in html
    assert "Vollständige Eintragsliste" in html
    without = reports.render_year_report(db, today=TODAY, include_entries=False)
    assert "Vollständige Eintragsliste" not in without and len(without) < len(html)


def test_year_report_klassenfahrt_variants_differ(db, year):
    add(db, date(2026, 10, 7), dtime(7), 15 * 60, "SV", flags=["klassenfahrt"])
    data = reports.year_report_data(db, today=TODAY, include_entries=False)
    a, b = data["klassenfahrt"]["variants"]
    assert a["variante"] == "A" and b["variante"] == "B"
    assert a["ist_bisher"] - b["ist_bisher"] == 5 * H  # B caps the day at 10 h
    assert b["massgeblich"] and not a["massgeblich"]
    assert data["klassenfahrt"]["tage"] == 1


def test_saldo_chart_svg_is_well_formed(db, september):
    import xml.etree.ElementTree as ET

    from app.services import ledger as ledger_svc

    svg = reports.saldo_chart_svg(ledger_svc.load_context(db, today=TODAY))
    root = ET.fromstring(str(svg))
    assert root.tag.endswith("svg")
    assert root.findall(".//{http://www.w3.org/2000/svg}polyline")


def test_num_de():
    assert reports.num_de(28.0) == "28"
    assert reports.num_de(27.5) == "27,5"
    assert reports.num_de(20.05) == "20,05"
