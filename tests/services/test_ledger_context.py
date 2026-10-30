"""services/ledger.py: DB → core, end to end through the database for the example school year."""

from __future__ import annotations

import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core import account as core
from app.core.calendar import build_days, holidays_between
from app.core.klassenfahrt import evaluate_ist
from app.core.leave import plan_leave
from app.core.types import AbsenceType, DayType, FerienKind, FerienRange, SollParams
from app.db.models import CalendarDay, Category, Ferien, TimeEntry
from app.services import calendar_service as cal
from app.services import ledger
from app.services import settings_service as settings_svc
from app.util import local_to_utc, now_utc

H = 3600
EXAMPLE_ICS = Path(__file__).resolve().parents[2] / "examples" / "ferien_bayern_BEISPIEL.ics"
TODAY = date(2026, 10, 29)


@pytest.fixture
def year(db):
    period = cal.create_period(db, "2026/27")
    cal.commit_ics(db, period, EXAMPLE_ICS.read_bytes())
    return period


def _cat(db, code: str) -> Category:
    return db.scalars(select(Category).where(Category.code == code)).one()


def add_entry(
    db,
    day: date,
    hours: float,
    code: str = "VN",
    *,
    flags: tuple[str, ...] = (),
    status: str = "active",
    deleted: bool = False,
    start: dtime = dtime(14, 0),
    subcode: str | None = None,
    running: bool = False,
) -> TimeEntry:
    """Test fixture rows (services/entries.py is owned elsewhere; the ledger only reads)."""
    start_at = local_to_utc(day, start)
    seconds = int(round(hours * H))
    entry = TimeEntry(
        start_at=start_at,
        end_at=None if running else start_at + timedelta(seconds=seconds),
        duration_seconds=0 if running else seconds,
        day=day,
        category_id=_cat(db, code).id,
        subcategory_id=_cat(db, subcode).id if subcode else None,
        flags=list(flags),
        status=status,
        deleted_at=now_utc() if deleted else None,
    )
    db.add(entry)
    db.commit()
    return entry


def fill_school_days(db, start: date, end: date, hours: float) -> dict[date, int]:
    """One VN entry of ``hours`` on every school day in [start, end]; returns ist per day."""
    ist: dict[date, int] = {}
    for row in db.scalars(
        select(CalendarDay).where(CalendarDay.date.between(start, end), CalendarDay.day_type == "schultag")
    ):
        add_entry(db, row.date, hours)
        ist[row.date] = int(hours * H)
    return ist


# =========================================================================== basics


def test_no_period_raises_hint(db):
    with pytest.raises(cal.NoPeriodError, match="Bitte zuerst ein Schuljahr anlegen"):
        ledger.load_context(db, today=TODAY)


def test_empty_year_figures(db, year):
    ctx = ledger.load_context(db, today=TODAY)
    assert ctx.period.id == year.id
    assert ctx.j_soll == 222 * 8 * H
    assert 1750 * H <= ctx.j_soll <= 1780 * H
    assert len(ctx.ledger) == 365
    assert ctx.ist_bisher == 0
    assert ctx.saldo_heute == -ctx.soll_bisher
    assert ctx.prognose_ende == -ctx.j_soll  # nothing recorded → no school-day average → neutral school days
    assert ctx.eu.hours == 0 and ctx.eu.ampel == "gruen"
    assert ctx.categories == [] and ctx.unterricht_share == 0
    assert len(ctx.plan.days) == 30 and ctx.leave_mode == "sommerferien_zuerst"
    # Materialised table and in-memory ledger agree.
    total = db.scalar(select(func.sum(CalendarDay.soll_seconds)).where(CalendarDay.period_id == year.id))
    assert total == ctx.j_soll
    assert {r.date: r.soll for r in ctx.ledger} == {
        r.date: r.soll_seconds for r in db.scalars(select(CalendarDay).where(CalendarDay.period_id == year.id))
    }


def test_ledger_equals_direct_core_computation(db, year):
    ist = fill_school_days(db, date(2026, 9, 15), date(2026, 10, 28), 9.5)
    add_entry(db, date(2026, 11, 3), 4, "K")  # Ferienarbeit in Herbstferien
    ist[date(2026, 11, 3)] = 4 * H
    add_entry(db, date(2026, 10, 10), 2, "VN")  # Saturday
    ist[date(2026, 10, 10)] = 2 * H
    today = date(2026, 11, 10)

    ctx = ledger.load_context(db, today=today)

    # Independent pipeline straight from the core.
    ferien = [
        FerienRange(f.id, f.name, FerienKind(f.kind), f.start, f.end)
        for f in db.scalars(select(Ferien).order_by(Ferien.start))
    ]
    holidays = holidays_between(year.start, year.end, {"mariae_himmelfahrt"})
    base = build_days(year.start, year.end, ferien, holidays, {}, frozenset())
    plan = plan_leave(base, ferien, "sommerferien_zuerst")
    days = build_days(year.start, year.end, ferien, holidays, {}, plan.days)
    p = SollParams()
    rows = core.build_ledger(days, p, ist, True, {})
    forecast = core.holiday_forecast(rows, today, ferien)
    geplant = sum(f.erwartete_ferienarbeit for f in forecast)

    assert ctx.ledger == rows
    assert ctx.saldo_heute == core.saldo_at(rows, today)
    assert ctx.j_soll == core.j_soll(rows)
    assert ctx.ist_bisher == core.ist_bisher(rows, today)
    assert ctx.ferienarbeit_bisher == core.ferienarbeit(rows, until=today) == 4 * H
    assert ctx.prognose_ende == core.prognose_ende(rows, today, 20, geplant)
    assert ctx.w_max == core.w_max(rows, today, geplant)
    assert ctx.holiday_forecast == forecast
    assert ctx.series == core.saldo_series(rows)
    assert ctx.weeks == core.week_summary(rows)
    assert ctx.months == core.month_summary(rows)
    assert ctx.schulwochen_ueber_48 == core.schulwochen_ueber(rows, 48)
    assert ctx.saldo_tage == core.saldo_tage(ctx.saldo_heute, p)
    # EU window: the days before the first period (no data) are neutralised like absences.
    eu_start = today - timedelta(days=364)
    eu_abs = {d.date: d.absence for d in days if d.absence}
    d = eu_start
    while d < year.start:
        eu_abs[d] = "urlaub"
        d += timedelta(days=1)
    eu = core.eu_mittel(ist, eu_abs, today,
                        {d for d, (_n, s) in holidays_between(year.start, today,
                                                              {"mariae_himmelfahrt"}).items()
                         if s in ("gesetzlich", "regional")})
    assert ctx.eu == eu and ctx.eu_daten_ab == year.start
    # Plausibility: 9.5 h per school day → +1.5 h per school day, Herbstferien −40 h + 4 h.
    school_days = sum(
        1 for r in rows if date(2026, 9, 15) <= r.date <= date(2026, 10, 28) and r.day_type == DayType.SCHULTAG
    )
    soll_before = sum(r.soll for r in rows if r.date < date(2026, 9, 15))
    unworked_school_days = 4  # 29./30.10. and 9./10.11.
    herbst = 5 * 8 * H
    weekend_and_ferien_work = 2 * H + 4 * H
    assert ctx.saldo_heute == (
        -soll_before + school_days * int(1.5 * H) + weekend_and_ferien_work - herbst - unworked_school_days * 8 * H
    )


def test_entry_filters_and_category_distribution(db, year):
    d = date(2026, 10, 14)
    add_entry(db, d, 5, "U", subcode="U.1", start=dtime(8, 0))
    add_entry(db, d, 2, "K", start=dtime(14, 0))
    add_entry(db, d, 1, "KR", start=dtime(16, 0))  # pseudo: only neutralises soll
    add_entry(db, d, 3, "VN", status="vorschlag", start=dtime(17, 0))
    add_entry(db, d, 3, "VN", status="verworfen", start=dtime(18, 0))
    add_entry(db, d, 3, "VN", deleted=True, start=dtime(19, 0))

    ctx = ledger.load_context(db, today=TODAY)
    row = ctx.row(d)
    assert ctx.ist_by_day[d] == 7 * H
    assert ctx.krank_partial[d] == 1 * H
    assert (row.ist_erfasst, row.krank_gutschrift, row.ist) == (7 * H, 1 * H, 8 * H)

    by_code = {c.code: c for c in ctx.categories}
    assert set(by_code) == {"U", "K"}  # subcategory mapped to its parent, KR excluded
    assert by_code["U"].seconds == 5 * H and by_code["U"].share == pytest.approx(5 / 7)
    assert ctx.unterricht_seconds == 5 * H and ctx.unterricht_share == pytest.approx(5 / 7)


def test_krank_override_neutralises_day(db, year):
    d = date(2026, 10, 14)
    cal.set_override(db, d, absence_type="krank")
    ctx = ledger.load_context(db, today=d)
    assert ctx.row(d).soll == 8 * H and ctx.row(d).ist == 8 * H and ctx.row(d).delta == 0

    settings_svc.update_settings(db, krank_neutralisiert=False)
    assert ledger.load_context(db, today=d).row(d).ist == 0


def test_klassenfahrt_variants(db, year):
    d = date(2026, 10, 14)
    add_entry(db, d, 15.5, "SV", flags=("klassenfahrt",), start=dtime(7, 0))
    add_entry(db, d, 1.5, "SV", flags=("nachtbereitschaft",), start=dtime(22, 30))
    b = ledger.load_context(db, today=TODAY)
    a = ledger.load_context(db, today=TODAY, klassenfahrt_variante="A")
    assert b.klassenfahrt_variante == "B" and b.ist_by_day[d] == 10 * H
    assert a.ist_by_day[d] == 17 * H
    assert a.saldo_heute - b.saldo_heute == 7 * H
    # Categories use the same basis as the Ist (variant B: night standby dropped, day capped).
    assert b.categories[0].seconds == 10 * H == sum(c.seconds for c in b.categories)
    assert a.categories[0].seconds == 17 * H

    settings_svc.update_settings(db, klassenfahrt_cap_h=12)
    assert ledger.load_context(db, today=TODAY).ist_by_day[d] == 12 * H
    # Direct core check
    assert evaluate_ist([(d, int(15.5 * H), ("klassenfahrt",)), (d, int(1.5 * H), ("nachtbereitschaft",))], "B", 12)[d] == 12 * H


def test_mehrarbeit_per_month(db, year):
    for i, day in enumerate((date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8))):
        flag = "vertretung_angeordnet" if i % 2 == 0 else "vertretung_genehmigt"
        add_entry(db, day, 0.75, "U", flags=(flag,), start=dtime(8, 0))
    add_entry(db, date(2026, 10, 9), 5, "U", start=dtime(8, 0))  # regular teaching, no flag
    add_entry(db, date(2026, 11, 10), 2.25, "U", flags=("vertretung_angeordnet",), start=dtime(8, 0))
    add_entry(db, date(2026, 11, 11), 0.75, "VN", flags=("vertretung_angeordnet",))  # not teaching
    ctx = ledger.load_context(db, today=date(2026, 12, 1))
    october = ctx.mehrarbeit[(2026, 10)]
    assert (october.ustd_angeordnet, october.ustd_genehmigt, october.total, october.ausgleichsfaehig) == (2, 2, 4, 4)
    november = ctx.mehrarbeit[(2026, 11)]
    assert (november.total, november.ausgleichsfaehig) == (3, 0)  # Bagatellgrenze


def test_running_timer_counts_elapsed(db, year):
    today = now_utc().date()
    if not (year.start <= today <= year.end):
        pytest.skip("only meaningful during the example school year")
    entry = add_entry(db, today, 0, "VN", running=True, start=dtime(0, 0))
    entry.start_at = now_utc() - timedelta(minutes=30)
    db.commit()
    ctx = ledger.load_context(db, today=today)
    assert 29 * 60 <= ctx.ist_by_day.get(entry.day, 0) <= 31 * 60


def test_leave_mode_variants_and_comparison(db, year):
    fill_school_days(db, date(2026, 9, 15), date(2026, 10, 28), 9)
    sommer = ledger.load_context(db, today=TODAY)
    prop = ledger.load_context(db, today=TODAY, leave_mode="proportional")
    assert sommer.leave_mode == "sommerferien_zuerst" and prop.leave_mode == "proportional"
    assert sommer.j_soll == prop.j_soll  # 30 days in both
    # Proportional: fewer leave days in August/September → lower saldo today.
    assert prop.saldo_heute < sommer.saldo_heute
    # calendar_day stays on the configured mode.
    assert db.get(CalendarDay, date(2026, 11, 2)).absence_type is None

    variants = ledger.compare_leave_modes(db, year, today=TODAY)
    assert [v["mode"] for v in variants] == ["sommerferien_zuerst", "proportional"]
    assert variants[0]["massgeblich"] and not variants[1]["massgeblich"]
    assert variants[0]["saldo_heute"] == sommer.saldo_heute
    assert variants[1]["saldo_heute"] == prop.saldo_heute
    assert variants[0]["prognose_ende"] == sommer.prognose_ende
    assert variants[0]["saldo_periodenende"] == variants[1]["saldo_periodenende"]
    assert variants[1]["urlaub"].assigned == 30
    assert cal.compare_leave_modes(db, year, today=TODAY) == variants


def test_holiday_forecast_uses_history_and_planned_work(db, year):
    prev = cal.create_period(db, "2025/26")
    herbst_2025 = cal.create_ferien(db, prev, name="Herbstferien 2025", start=date(2025, 11, 3), end=date(2025, 11, 7))
    add_entry(db, date(2025, 11, 4), 12, "K")
    add_entry(db, date(2025, 11, 5), 0, "K")

    ctx = ledger.load_context(db, year, today=TODAY)
    herbst = next(f for f in ctx.holiday_forecast if f.kind == FerienKind.HERBST)
    assert herbst.erwartete_ferienarbeit == 12 * H  # mean of past Herbstferien
    assert herbst.abbau_kapazitaet == 5 * 8 * H
    assert herbst.saldo_nach == herbst.saldo_vor - 40 * H + 12 * H
    weihnachten = next(f for f in ctx.holiday_forecast if f.kind == FerienKind.WEIHNACHTEN)
    assert weihnachten.erwartete_ferienarbeit == 0  # no history
    assert ctx.ferienarbeit_geplant == 12 * H

    settings_svc.set_ferienarbeit_schaetzung(db, {herbst.ferien_id: 3, weihnachten.ferien_id: 10})
    ctx = ledger.load_context(db, year, today=TODAY)
    by_id = {f.ferien_id: f for f in ctx.holiday_forecast}
    assert by_id[herbst.ferien_id].erwartete_ferienarbeit == 3 * H
    assert by_id[weihnachten.ferien_id].erwartete_ferienarbeit == 10 * H
    assert ctx.planned_work == {herbst.ferien_id: 3 * H, weihnachten.ferien_id: 10 * H}
    assert ctx.prognose_ende == core.prognose_ende(ctx.ledger, TODAY, 20, 13 * H)
    assert herbst_2025.id not in by_id


def test_eu_window_reaches_into_previous_period(db, year):
    prev = cal.create_period(db, "2025/26")
    cal.set_override(db, date(2026, 3, 2), absence_type="krank")
    add_entry(db, date(2026, 3, 3), 40, "VN", start=dtime(0, 0))
    ctx = ledger.load_context(db, year, today=TODAY)
    assert ctx.eu.ist_seconds == 40 * H
    assert ctx.eu.absence_days == 30 + 1 + 1  # summer leave 2026 + sick day + 2025/26 leave (Buß- und Bettag only)
    assert prev.id != year.id


def test_day_detail(db, year):
    d = date(2026, 10, 14)
    add_entry(db, d, 2, "K", start=dtime(14, 0))
    add_entry(db, d, 1, "KR", start=dtime(16, 0))
    add_entry(db, d, 0.75, "U", status="vorschlag", start=dtime(8, 0))
    detail = ledger.day_detail(db, d)
    assert (detail.day_type, detail.soll, detail.ist_erfasst, detail.krank_gutschrift) == ("schultag", 8 * H, 2 * H, H)
    assert detail.ist == 3 * H and detail.delta == -5 * H
    assert [e["category"]["code"] for e in detail.entries] == ["K", "KR"]
    assert len(detail.vorschlaege) == 1 and detail.vorschlaege[0]["status"] == "vorschlag"
    assert detail.period_id == year.id

    bb = ledger.day_detail(db, date(2026, 11, 18))
    assert (bb.day_type, bb.holiday_name, bb.soll) == ("unterrichtsfrei", "Buß- und Bettag", 8 * H)
    herbst = ledger.day_detail(db, date(2026, 11, 4))
    assert herbst.ferien_name == "BEISPIEL – Herbstferien 2026"
    leave = ledger.day_detail(db, date(2026, 8, 3))
    assert (leave.absence, leave.soll) == ("urlaub", 0)

    outside = ledger.day_detail(db, date(2030, 1, 1))  # no period
    assert (outside.day_type, outside.soll, outside.period_id) == ("feiertag", 0, None)
    assert ledger.day_detail(db, date(2030, 1, 2)).soll == 8 * H


def test_full_year_performance(db, year):
    for row in db.scalars(select(CalendarDay).where(CalendarDay.period_id == year.id)):
        if row.day_type == "schultag":
            for start, hours, code in ((dtime(7, 30), 5, "U"), (dtime(14, 0), 2, "VN"), (dtime(19, 0), 1.5, "K")):
                db.add(
                    TimeEntry(
                        start_at=local_to_utc(row.date, start),
                        end_at=local_to_utc(row.date, start) + timedelta(hours=hours),
                        duration_seconds=int(hours * H),
                        day=row.date,
                        category_id=_cat(db, code).id,
                        flags=[],
                    )
                )
    db.commit()
    ledger.load_context(db, today=date(2027, 7, 31))  # warm-up
    t0 = time.perf_counter()
    ctx = ledger.load_context(db, today=date(2027, 7, 31))
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.2, f"load_context took {elapsed:.3f}s"
    assert ctx.ist_bisher == sum(r.ist for r in ctx.ledger)
    assert ctx.schulwochen_ueber_48 == 0  # 8.5 h × 5 = 42.5 h per full school week
    assert max(w.ist_erfasst for w in ctx.weeks) == 5 * int(8.5 * H)
    assert ctx.saldo_heute == ctx.series[-1][1]
    assert ctx.unterricht_share == pytest.approx(5 / 8.5)


def test_absence_types_exposed(db, year):
    cal.set_override(db, date(2026, 10, 14), absence_type="fortbildung")
    ctx = ledger.load_context(db, today=TODAY)
    assert ctx.row(date(2026, 10, 14)).absence == AbsenceType.FORTBILDUNG
    assert ctx.row(date(2026, 10, 14)).soll == 8 * H
