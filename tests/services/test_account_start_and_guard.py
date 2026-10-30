"""Audit fixes for the account figures.

* Settings changes that alter the Soll/Ist of closed months need a reason (Konzept L4, §4).
* ``settings.erfassung_ab``: starting mid-year does not count earlier school days as "0 h worked".
* Übertrag (Konzept §1.3, §18): the previous period's closing saldo is shown, and added only when configured.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import func, select

from app.db.models import CalendarDay, Category, MonthClose, TimeEntry
from app.services import calendar_service as cal
from app.services import ledger
from app.services import months as months_svc
from app.services import settings_service as settings_svc
from app.services import timetable as tts
from app.util import local_to_utc

TODAY = date(2026, 10, 30)


def cat(db, code: str) -> int:
    return db.scalar(select(Category.id).where(Category.code == code))


def add(db, day: date, hours: float = 8.0, code: str = "U") -> TimeEntry:
    start = local_to_utc(day, time(8))
    entry = TimeEntry(start_at=start, end_at=start + timedelta(hours=hours), duration_seconds=int(hours * 3600),
                      has_bounds=True, day=day, category_id=cat(db, code), status="active", source="manuell",
                      flags=[], created_at=datetime(2026, 10, 1))
    db.add(entry)
    db.commit()
    return entry


def september_soll(db) -> int:
    return db.scalar(select(func.sum(CalendarDay.soll_seconds)).where(
        CalendarDay.date.between(date(2026, 9, 1), date(2026, 9, 30))))


@pytest.fixture
def year(db):
    period = cal.create_period(db, "2026/27")
    db.commit()
    return period


@pytest.fixture
def closed_september(db, year):
    add(db, date(2026, 9, 15))
    months_svc.close_month(db, "2026-09", force=True, note="Test", today=TODAY)
    return db.get(MonthClose, "2026-09")


# --------------------------------------------------------------------------- settings guard


def test_settings_change_touching_a_closed_month_needs_a_reason(db, closed_september):
    before = september_soll(db)
    with pytest.raises(cal.CalendarLocked) as exc:
        settings_svc.update_settings(db, upz_ist=21)
    assert "2026-09" in str(exc.value)
    db.expire_all()
    assert settings_svc.get_settings(db).upz_ist == 28
    assert september_soll(db) == before
    assert db.get(MonthClose, "2026-09").status == "abgeschlossen"


def test_settings_change_with_reason_marks_the_month(db, closed_september):
    before = september_soll(db)
    settings_svc.update_settings(db, upz_ist=21, reason="Teilzeit rückwirkend genehmigt")
    db.expire_all()
    assert september_soll(db) < before
    mc = db.get(MonthClose, "2026-09")
    assert mc.status == "nachtraeglich_korrigiert"
    assert mc.hash_history and "Teilzeit rückwirkend" in mc.hash_history[-1]["reason"]
    assert mc.hash_history[-1]["kind"] == "einstellungen"


def test_rule_change_affecting_ist_of_a_closed_month_is_guarded(db, year):
    cal.set_override(db, date(2026, 9, 16), absence_type="krank")
    add(db, date(2026, 9, 15))
    months_svc.close_month(db, "2026-09", force=True, note="Test", today=TODAY)
    with pytest.raises(cal.CalendarLocked):
        settings_svc.update_settings(db, krank_neutralisiert=False)


def test_unrelated_settings_change_needs_no_reason(db, closed_september):
    settings_svc.update_settings(db, name="Frau Muster", klassen=["3b"])
    settings_svc.update_settings(db, prognose_schultage=15)
    assert db.get(MonthClose, "2026-09").status == "abgeschlossen"


def test_leave_mode_shortcut_refuses_without_reason(client, db, year):
    # Sommerferien reach into September: "Sommerferien zuerst" puts leave days there, "proportional" moves some.
    cal.create_ferien(db, year, name="Sommerferien", start=date(2026, 8, 3), end=date(2026, 9, 14))
    cal.create_ferien(db, year, name="Weihnachtsferien", start=date(2026, 12, 24), end=date(2027, 1, 8))
    cal.create_ferien(db, year, name="Osterferien", start=date(2027, 3, 29), end=date(2027, 4, 9))
    add(db, date(2026, 9, 15))
    months_svc.close_month(db, "2026-09", force=True, note="Test", today=TODAY)
    resp = client.post("/kalender/urlaubsmodus", data={"urlaubs_modus": "proportional", "next": "/kalender"},
                       follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    assert settings_svc.get_settings(db).urlaubs_modus == "sommerferien_zuerst"


def test_settings_page_asks_for_a_reason(client, db, closed_september):
    page = client.get("/einstellungen").text
    assert 'name="grund"' in page
    resp = client.post("/einstellungen/upz", data={
        "upz_voll": "28", "upz_ist": "21", "anrechnungsstunden": "0", "anrechnungs_grund": "",
        "geburtsdatum": "",
    })
    assert resp.status_code == 423
    assert "Begründung" in resp.text
    resp = client.post("/einstellungen/upz", data={
        "upz_voll": "28", "upz_ist": "21", "anrechnungsstunden": "0", "anrechnungs_grund": "",
        "geburtsdatum": "", "grund": "Teilzeit ab September",
    }, follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(MonthClose, "2026-09").status == "nachtraeglich_korrigiert"


def test_api_patch_needs_reason(client, db, closed_september):
    assert client.patch("/api/settings", json={"upz_ist": 21}).status_code == 423
    assert client.patch("/api/settings", json={"upz_ist": 21, "reason": "Teilzeit"}).status_code == 200


# --------------------------------------------------------------------------- Erfassung ab


def test_mid_year_start_without_erfassung_ab_is_a_big_deficit(db, year):
    ctx = ledger.load_context(db, year, today=TODAY)
    assert ctx.saldo_heute < -200 * 3600


def test_erfassung_ab_neutralises_earlier_days(db, year):
    settings_svc.update_settings(db, erfassung_ab=date(2026, 10, 26))
    for d in (date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29)):
        add(db, d, 9.0)
    ctx = ledger.load_context(db, year, today=date(2026, 10, 29))
    # 26.–29.10. are Herbstferien in Bavaria only with ferien data; without ferien they are school days:
    # four days à +1 h, everything before is neutral.
    assert ctx.saldo_heute == 4 * 3600
    assert ctx.erfassung_ab == date(2026, 10, 26)
    early = ctx.row(date(2026, 9, 15))
    assert early.vor_erfassung and early.delta == 0
    # the Prognose only samples recorded days (9 h a day -> clearly positive)
    assert ctx.prognose_ende > 0
    # the EU mean starts at the recording start
    assert ctx.eu_daten_ab == date(2026, 10, 26)


def test_erfassung_ab_hides_older_days_from_the_todo_lists(db, year):
    tts.create_timetable(db, valid_from=date(2026, 9, 14), slots=[
        {"weekday": wd, "slot_no": 1, "klasse": "3b"} for wd in range(1, 6)
    ])
    assert tts.open_days(db, until=TODAY, today=TODAY).days
    settings_svc.update_settings(db, erfassung_ab=TODAY)
    assert tts.open_days(db, until=TODAY - timedelta(days=1), today=TODAY).days == []
    ov = months_svc.month_overview(db, "2026-09", today=TODAY)
    assert not [i for i in ov.open_items if i.kind == months_svc.ITEM_OHNE_EINTRAG]


def test_wizard_offers_erfassung_ab(client, db, year):
    page = client.get(f"/kalender/einrichtung?periode={year.id}&schritt=4").text
    assert "Ab wann erfasst du?" in page and 'name="erfassung_ab"' in page
    resp = client.post("/kalender/erfassung-ab", data={
        "erfassung_ab": "2026-10-19", "next": f"/kalender/einrichtung?periode={year.id}&schritt=4"},
        follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    assert settings_svc.get_settings(db).erfassung_ab == date(2026, 10, 19)
    assert "Erfassung ab 19.10.2026" in client.get("/konto").text


# --------------------------------------------------------------------------- Übertrag


@pytest.fixture
def two_years(db):
    prev = cal.create_period(db, "2025/26")
    cur = cal.create_period(db, "2026/27")
    db.commit()
    return prev, cur


def test_uebertrag_is_shown_but_not_added_by_default(db, two_years):
    prev, cur = two_years
    settings_svc.update_settings(db, erfassung_ab=date(2026, 7, 27))
    for d in range(27, 32):  # Mon–Fri, last week of 2025/26 (school days without ferien): +2 h each
        add(db, date(2026, 7, d), 10.0)
    ctx = ledger.load_context(db, cur, today=date(2026, 8, 3))
    assert ctx.uebertrag == 10 * 3600
    assert ctx.uebertrag_label == "2025/26"
    assert not ctx.uebertrag_verrechnet
    plain = ctx.saldo_heute
    settings_svc.update_settings(db, uebertrag_verrechnen=True)
    ctx2 = ledger.load_context(db, cur, today=date(2026, 8, 3))
    assert ctx2.uebertrag_verrechnet
    assert ctx2.saldo_heute == plain + 10 * 3600
    assert ctx2.prognose_ende == ctx.prognose_ende + 10 * 3600
    assert ctx2.series[-1][1] == ctx.series[-1][1] + 10 * 3600
    assert ctx2.saldo_at(cur.end) == ctx.saldo_at(cur.end) + 10 * 3600


def test_no_uebertrag_without_data_in_the_previous_period(db, two_years):
    _prev, cur = two_years
    ctx = ledger.load_context(db, cur, today=TODAY)
    assert ctx.uebertrag is None


def test_konto_shows_uebertrag(client, db, two_years):
    settings_svc.update_settings(db, erfassung_ab=date(2026, 7, 27))
    add(db, date(2026, 7, 27), 10.0)
    assert "Übertrag aus 2025/26" in client.get("/konto").text
