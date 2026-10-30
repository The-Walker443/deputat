"""Review fixes in services/ledger.py and settings_service: running timer over midnight, EU window
before the first data, category statistics on the Ist basis, Altersermäßigung independent of the Stichtag."""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import Category, Settings, TimeEntry
from app.services import calendar_service as cal
from app.services import ledger
from app.services import settings_service as settings_svc
from app.util import local_to_utc

H = 3600
EXAMPLE_ICS = Path(__file__).resolve().parents[2] / "examples" / "ferien_bayern_BEISPIEL.ics"


@pytest.fixture
def year(db):
    period = cal.create_period(db, "2026/27")
    cal.commit_ics(db, period, EXAMPLE_ICS.read_bytes())
    return period


def _cat(db, code: str) -> Category:
    return db.scalars(select(Category).where(Category.code == code)).one()


def _entry(db, day: date, hours: float, code: str = "VN", *, start: dtime = dtime(14, 0),
           running: bool = False, flags: tuple[str, ...] = ()) -> TimeEntry:
    start_at = local_to_utc(day, start)
    seconds = int(round(hours * H))
    e = TimeEntry(
        start_at=start_at,
        end_at=None if running else start_at + timedelta(seconds=seconds),
        duration_seconds=0 if running else seconds,
        day=day,
        category_id=_cat(db, code).id,
        flags=list(flags),
        status="active",
    )
    db.add(e)
    db.commit()
    return e


# ------------------------------------------------------------------ running timer across midnight


def test_running_parts_split_at_local_midnight():
    start = local_to_utc(date(2026, 11, 9), dtime(22, 0))
    now = local_to_utc(date(2026, 11, 10), dtime(1, 0))
    assert ledger.running_parts(start, now) == [(date(2026, 11, 9), 2 * H), (date(2026, 11, 10), 1 * H)]
    assert ledger.running_parts(now, start) == [(date(2026, 11, 10), 0)]


def test_running_timer_over_midnight_is_booked_per_day(db, year, monkeypatch):
    _entry(db, date(2026, 11, 9), 0, start=dtime(22, 0), running=True)
    now = local_to_utc(date(2026, 11, 10), dtime(1, 0))
    monkeypatch.setattr(ledger, "now_utc", lambda: now)
    ctx = ledger.load_context(db, today=date(2026, 11, 10))
    assert ctx.ist_by_day[date(2026, 11, 9)] == 2 * H
    assert ctx.ist_by_day[date(2026, 11, 10)] == 1 * H
    # The day view of the new day counts the part after midnight.
    assert ledger.day_detail(db, date(2026, 11, 10)).ist_erfasst == 1 * H
    assert ledger.day_detail(db, date(2026, 11, 9)).ist_erfasst == 2 * H


# ------------------------------------------------------------------ EU window


def test_eu_window_before_first_data_is_neutralised(db, year):
    today = date(2026, 11, 10)
    for row_day in (date(2026, 11, 2) + timedelta(days=i) for i in range(5)):  # one school week, 45 h
        _entry(db, row_day, 9, start=dtime(8, 0))
    ctx = ledger.load_context(db, today=today)
    assert ctx.eu_daten_ab == year.start == date(2026, 8, 1)
    # Covered: 01.08.–10.11.2026 = 102 days ≈ 14.6 weeks (minus the 30 leave days), not 52 weeks.
    assert ctx.eu.divisor_weeks == pytest.approx(52 - ctx.eu.absence_days / 5)
    assert 8 < ctx.eu.divisor_weeks < 15
    assert ctx.eu.hours == pytest.approx(45 / ctx.eu.divisor_weeks)
    assert ctx.eu.hours > 2.9  # 45 h / 52 weeks would be 0.87 h


def test_eu_window_fully_covered_is_unchanged(db, year):
    _entry(db, date(2025, 9, 1), 1)  # imported data from more than a year ago covers the window
    ctx = ledger.load_context(db, today=date(2026, 11, 10))
    assert ctx.eu_daten_ab is None
    assert ctx.eu.divisor_weeks == 52 - 30 / 5  # only the 30 leave days of the period are neutral


# ------------------------------------------------------------------ statistics on the Ist basis


def test_evaluated_rows_scale_capped_days_exactly():
    cats = {1: Category(id=1, code="SV", name="x", color="#000", is_pseudo=False, is_unterricht=False),
            2: Category(id=2, code="U", name="u", color="#000", is_pseudo=False, is_unterricht=True)}
    d = date(2026, 10, 14)
    rows = [ledger.EntryRow(d, 7 * H + 1, ("klassenfahrt",), 1), ledger.EntryRow(d, 7 * H, (), 2),
            ledger.EntryRow(d, 2 * H, ("nachtbereitschaft",), 1)]
    out = ledger.evaluated_rows(rows, cats, {d: 10 * H}, "B")
    assert sum(r.seconds for r in out) == 10 * H
    assert len(out) == 2  # night standby dropped under B
    out_a = ledger.evaluated_rows(rows, cats, {d: 16 * H + 1}, "A")
    assert sum(r.seconds for r in out_a) == 16 * H + 1


def test_month_categories_match_ist_with_klassenfahrt(db, year):
    from app.services import months

    for d in (date(2026, 10, 12), date(2026, 10, 13)):
        _entry(db, d, 15.5, "SV", start=dtime(7, 0), flags=("klassenfahrt",))
        _entry(db, d, 1.5, "SV", start=dtime(22, 30), flags=("nachtbereitschaft",))
    ov = months.month_overview(db, "2026-10", today=date(2026, 11, 10), sync=False)
    assert ov.ist_erfasst == 20 * H
    assert sum(c.seconds for c in ov.categories) == ov.ist_erfasst


# ------------------------------------------------------------------ Altersermäßigung vs. Stichtag


def test_j_soll_independent_of_stichtag_and_settings_not_rewritten(db, year):
    from app.util import today_local

    settings_svc.update_settings(db, geburtsdatum=date(1966, 9, 1), upz_ist=26)
    expected_ae = settings_svc.get_settings(db).altersermaessigung
    a = ledger.load_context(db, year, today=date(2026, 11, 10))
    b = ledger.load_context(db, year, today=date(2027, 7, 31))
    assert a.j_soll == b.j_soll and a.params == b.params
    db.expire_all()
    assert db.get(Settings, 1).altersermaessigung == expected_ae
    assert expected_ae == settings_svc.compute_altersermaessigung(
        date(1966, 9, 1), settings_svc.school_year_start_year(today_local()))


def test_future_month_cumulative_saldo_is_as_of_stichtag(db, year):
    from app.services import months

    today = date(2026, 11, 10)
    rows = {r.year_month: r for r in months.list_months(db, year, today=today)}
    saldo_today = ledger.load_context(db, year, today=today).saldo_heute
    assert rows["2026-12"].zukunft and rows["2026-12"].saldo_kumuliert == saldo_today
    assert rows["2027-03"].saldo_kumuliert == saldo_today
    ov = months.month_overview(db, "2027-01", today=today, sync=False)
    assert ov.eu.window_end == today
