"""Unit tests for core functions beyond the §13 acceptance table."""

from datetime import UTC, date, datetime, time, timedelta
from fractions import Fraction
from zoneinfo import ZoneInfo

import pytest

from app.core.account import (
    build_ledger,
    default_ferienarbeit,
    eu_ampel,
    eu_mittel,
    ferien_work_history,
    ferienarbeit,
    holiday_forecast,
    ist_pro_schultag,
    j_soll,
    month_summary,
    prognose_ende,
    saldo_at,
    saldo_series,
    saldo_tage,
    schulwochen_gesamt,
    schulwochen_ueber,
    w_max,
    week_summary,
)
from app.core.klassenfahrt import evaluate_ist
from app.core.leave import assign_leave, leave_candidates, plan_leave
from app.core.mehrarbeit import mehrarbeit_by_month, month_mehrarbeit, seconds_to_ustd
from app.core.parsing import parse_date, parse_duration, parse_time, parse_time_seconds
from app.core.calendar import holidays_between
from app.core.soll import (
    day_soll,
    day_solls,
    period_soll,
    round_half_up,
    tagessoll_exact,
    tagessoll_seconds,
    teilzeitfaktor,
)
from app.core.timesplit import local_day, split_at_midnight
from app.core.types import (
    FLAG_KLASSENFAHRT,
    FLAG_NACHTBEREITSCHAFT,
    FLAG_VERTRETUNG_ANGEORDNET,
    AbsenceType,
    Ampel,
    DayType,
    FerienKind,
    LeaveMode,
    Override,
    SollParams,
)
from core_builders import H, ferien, make_days, school_year_2026_ferien, weekdays

BERLIN = ZoneInfo("Europe/Berlin")
FULL = SollParams()

# ---------------------------------------------------------------- soll


def test_soll_params_and_factors():
    p = SollParams(upz_ist=23)
    assert teilzeitfaktor(p) == pytest.approx(23 / 28)
    assert tagessoll_seconds(p) == 23657  # 28800 * 23/28 = 23657.14 -> rounded per day
    assert tagessoll_seconds(SollParams(upz_ist=14)) == 4 * H
    assert tagessoll_seconds(SollParams(wochen_soll_voll_h=41.5)) == 29880
    with pytest.raises(ValueError):
        SollParams(upz_voll=0)


@pytest.mark.parametrize(
    "absence", [AbsenceType.DIENSTBEFREIUNG, AbsenceType.SONDERURLAUB, AbsenceType.ELTERNZEIT]
)
def test_soll_free_absences(absence):
    d = date(2026, 10, 14)
    days = make_days(d, d, overrides={d: Override(d, absence=absence)})
    assert build_ledger(days, FULL, {})[0].soll == 0


@pytest.mark.parametrize("absence", [AbsenceType.FORTBILDUNG, AbsenceType.KRANK])
def test_soll_kept_for_fortbildung_and_krank(absence):
    d = date(2026, 10, 14)
    days = make_days(d, d, overrides={d: Override(d, absence=absence)})
    assert build_ledger(days, FULL, {})[0].soll == 8 * H


# ---------------------------------------------------------------- krank


def test_krank_not_neutralised_when_disabled():
    d = date(2026, 10, 14)
    days = make_days(d, d, overrides={d: Override(d, absence=AbsenceType.KRANK)})
    row = build_ledger(days, FULL, {}, krank_neutralisiert=False)[0]
    assert row.delta == -8 * H


def test_partial_krank_credit_only_fills_gap_to_soll():
    d1, d2, d3 = date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)
    days = make_days(d1, d3)
    rows = build_ledger(
        days, FULL, {d1: 4 * H, d2: 2 * H, d3: 6 * H}, krank_partial={d1: 4 * H, d2: 9 * H, d3: 4 * H}
    )
    assert rows[0].ist == 8 * H and rows[0].krank_gutschrift == 4 * H and rows[0].ist_erfasst == 4 * H
    assert rows[1].krank_gutschrift == 6 * H and rows[1].delta == 0
    # 6 h recorded + 4 h Krank: the credit only neutralises the missing 2 h, no surplus.
    assert rows[2].krank_gutschrift == 2 * H and rows[2].ist == 8 * H and rows[2].delta == 0


# ---------------------------------------------------------------- leave


def _year():
    blocks = school_year_2026_ferien() + [ferien("he", "Herbstferien", date(2026, 11, 2), date(2026, 11, 6))]
    return blocks, make_days(date(2026, 8, 1), date(2027, 7, 31), blocks)


def test_leave_candidates_exclude_weekend_holiday_and_sick_days():
    blocks, days = _year()
    cands = set(leave_candidates(days))
    assert date(2026, 8, 15) not in cands  # Saturday + Mariä Himmelfahrt
    assert date(2026, 12, 25) not in cands  # holiday inside Weihnachtsferien
    assert date(2026, 11, 18) in cands  # Buß- und Bettag (unterrichtsfrei)
    assert date(2026, 10, 14) not in cands  # school day

    sick = date(2026, 8, 4)
    days2 = make_days(
        date(2026, 8, 1), date(2027, 7, 31), blocks, overrides={sick: Override(sick, absence=AbsenceType.KRANK)}
    )
    assert sick not in leave_candidates(days2)


def test_sommerferien_zuerst_falls_back_in_documented_order():
    blocks, days = _year()
    plan = plan_leave(days, blocks, LeaveMode.SOMMERFERIEN_ZUERST, n=33)
    extra = sorted(plan.days - set(weekdays(date(2026, 8, 3), date(2026, 9, 11))))
    # after summer: Weihnachten chronologically (21.–23.12.)
    assert extra == [date(2026, 12, 21), date(2026, 12, 22), date(2026, 12, 23)]


def test_shortfall_when_not_enough_candidates():
    herbst = ferien("he", "Herbstferien", date(2026, 11, 2), date(2026, 11, 6))
    days = make_days(date(2026, 10, 26), date(2026, 11, 8), [herbst])
    for mode in (LeaveMode.SOMMERFERIEN_ZUERST, LeaveMode.PROPORTIONAL):
        plan = plan_leave(days, [herbst], mode, n=30)
        assert len(plan.days) == 5 and plan.shortfall == 25 and plan.candidates == 5


def test_proportional_uses_non_ferien_candidates_only_as_fallback():
    blocks, days = _year()
    plan = plan_leave(days, blocks, LeaveMode.PROPORTIONAL)
    assert date(2026, 11, 18) not in plan.days
    assert len(plan.days) == 30


def test_manual_mode_validates():
    blocks, days = _year()
    manual = {date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 8), date(2026, 10, 14), date(2026, 12, 25)}
    plan = plan_leave(days, blocks, "manuell", manual=manual)
    assert plan.days == {date(2026, 8, 3), date(2026, 8, 4)}
    assert plan.invalid == {date(2026, 8, 8), date(2026, 10, 14), date(2026, 12, 25)}
    assert plan.shortfall == 28
    assert assign_leave(days, blocks, LeaveMode.MANUELL, manual=manual) == plan.days


# ---------------------------------------------------------------- account


def _school_term():
    """Mon 05.10.2026 – Sun 15.11.2026 incl. Herbstferien 02.–06.11."""
    herbst = ferien("he", "Herbstferien", date(2026, 11, 2), date(2026, 11, 6))
    days = make_days(date(2026, 10, 5), date(2026, 11, 15), [herbst])
    return herbst, days


def test_saldo_series_week_month_aggregation():
    herbst, days = _school_term()
    ist = {d: 10 * H for d in weekdays(date(2026, 10, 5), date(2026, 10, 30))}
    ist[date(2026, 11, 4)] = 3 * H  # Ferienarbeit
    ledger = build_ledger(days, FULL, ist)

    series = saldo_series(ledger)
    assert series[4] == (date(2026, 10, 9), 10 * H)
    assert series[-1][1] == sum(r.delta for r in ledger)

    weeks = week_summary(ledger)
    assert weeks[0].monday == date(2026, 10, 5) and weeks[0].delta == 10 * H and weeks[0].is_schulwoche
    herbst_week = next(w for w in weeks if w.monday == date(2026, 11, 2))
    assert not herbst_week.is_schulwoche and herbst_week.soll == 40 * H and herbst_week.ist == 3 * H

    months = month_summary(ledger)
    assert [(m.year, m.month) for m in months] == [(2026, 10), (2026, 11)]
    assert months[-1].saldo_end == series[-1][1]
    assert months[1].ferienarbeit == 3 * H == ferienarbeit(ledger)
    assert saldo_tage(16 * H, FULL) == 2.0
    assert saldo_tage(16 * H, SollParams(upz_ist=0)) == 0.0


def test_schulwochen_ueber_48():
    _, days = _school_term()
    ist = {d: 10 * H for d in weekdays(date(2026, 10, 5), date(2026, 10, 9))}  # 50 h
    ist.update({d: 9 * H for d in weekdays(date(2026, 10, 12), date(2026, 10, 16))})  # 45 h
    ist.update({d: 10 * H for d in weekdays(date(2026, 11, 2), date(2026, 11, 6))})  # Ferien, not counted
    assert schulwochen_ueber(build_ledger(days, FULL, ist)) == 1


def test_prognose_ende_and_w_max():
    herbst, days = _school_term()
    today = date(2026, 10, 16)
    ist = {d: 9 * H for d in weekdays(date(2026, 10, 5), today)}  # +1 h per school day
    ledger = build_ledger(days, FULL, ist)
    assert ist_pro_schultag(ledger, today) == 9 * H

    # saldo today +10 h; school days 19.–30.10 (10) → +10 h; Herbstferien −40 h;
    # school days 09.–13.11 (5) → +5 h; planned ferienarbeit 4 h.
    assert prognose_ende(ledger, today, geplante_ferienarbeit=4 * H) == (10 + 10 - 40 + 5 + 4) * H

    assert schulwochen_gesamt(ledger) == 5  # 25 school days
    assert w_max(ledger, today) == 48 * H  # J_soll 240 h (25 school days + 5 ferien days) / 5 weeks
    assert w_max(ledger, today, ferienarbeit_geplant=10 * H) == 46 * H


def test_holiday_forecast_projects_gap_and_defaults():
    herbst, days = _school_term()
    today = date(2026, 10, 23)
    ist = {d: 9 * H for d in weekdays(date(2026, 10, 5), today)}
    ledger = build_ledger(days, FULL, ist)
    (fc,) = holiday_forecast(ledger, today, [herbst])
    assert fc.saldo_vor == 15 * H + 5 * H
    assert fc.prognose_bis == 5 * H
    assert fc.erwartete_ferienarbeit == 0  # no past Herbstferien in the ledger
    assert fc.saldo_nach == (20 - 40) * H


def test_holiday_forecast_running_ferien_counts_remaining_days_only():
    herbst, days = _school_term()
    ledger = build_ledger(days, FULL, {})
    (fc,) = holiday_forecast(ledger, date(2026, 11, 4), [herbst], planned_work={"he": H})
    assert fc.abbau_kapazitaet == 16 * H and fc.prognose_bis == 0
    assert holiday_forecast(ledger, date(2026, 11, 6), [herbst]) == []


@pytest.mark.parametrize(
    ("hours", "ampel"),
    [(43.99, Ampel.GRUEN), (44, Ampel.GELB), (48, Ampel.GELB), (48.01, Ampel.ROT)],
)
def test_eu_ampel_thresholds(hours, ampel):
    assert eu_ampel(hours) == ampel


# ---------------------------------------------------------------- klassenfahrt


def test_klassenfahrt_variants():
    d = date(2027, 6, 15)
    entries = [
        (d, 15 * H + 30 * 60, [FLAG_KLASSENFAHRT]),  # 07:00–22:30
        (d, 8 * H, [FLAG_KLASSENFAHRT, FLAG_NACHTBEREITSCHAFT]),
        (d, H, []),  # regular work the same day
        (date(2027, 6, 16), 6 * H, [FLAG_KLASSENFAHRT]),
    ]
    entries.append((date(2027, 6, 17), 11 * H, []))  # no Klassenfahrt: never capped
    a = evaluate_ist(entries, "A")
    b = evaluate_ist(entries)
    assert a[d] == 24 * H + 30 * 60
    # Variant B caps the whole Klassenfahrt day, unflagged entries included.
    assert b[d] == 10 * H
    assert b[date(2027, 6, 16)] == 6 * H
    assert b[date(2027, 6, 17)] == a[date(2027, 6, 17)] == 11 * H
    assert evaluate_ist(entries, "B", cap_h=12)[d] == 12 * H
    assert evaluate_ist(entries, "B", cap_h=20)[d] == 16 * H + 30 * 60


def test_klassenfahrt_b_counts_day_with_only_night_standby_as_zero():
    d = date(2027, 6, 15)
    assert evaluate_ist([(d, 8 * H, [FLAG_KLASSENFAHRT, FLAG_NACHTBEREITSCHAFT])]) == {d: 0}


# ---------------------------------------------------------------- mehrarbeit


def test_mehrarbeit_rounding_and_grouping():
    assert seconds_to_ustd(45 * 60) == 1.0
    assert seconds_to_ustd(55 * 60) == 1.0  # 1.22 -> 1.0
    assert seconds_to_ustd(57 * 60 + 30) == 1.5  # 1.2777 -> 1.5 (half up at 1.25)
    assert seconds_to_ustd(56 * 60 + 15) == 1.5  # exactly 1.25 -> 1.5
    entries = [
        (date(2026, 10, 5), 45 * 60, {FLAG_VERTRETUNG_ANGEORDNET}),
        (date(2026, 10, 6), 45 * 60, set()),  # not flagged: ignored
        (date(2026, 11, 5), 4 * 45 * 60, {FLAG_VERTRETUNG_ANGEORDNET}),
    ]
    by_month = mehrarbeit_by_month(entries)
    assert by_month[(2026, 10)].total == 1 and by_month[(2026, 10)].ausgleichsfaehig == 0
    assert by_month[(2026, 11)].ustd_angeordnet == 4 and by_month[(2026, 11)].ausgleichsfaehig == 4
    assert month_mehrarbeit([]).total == 0


# ---------------------------------------------------------------- timesplit


def _utc(*args):
    return datetime(*args, tzinfo=BERLIN).astimezone(UTC)


def test_split_not_needed_and_multi_day():
    start, end = _utc(2026, 11, 3, 8, 0), _utc(2026, 11, 3, 12, 0)
    assert split_at_midnight(start, end) == [(start, end)]
    start, end = _utc(2026, 11, 3, 20, 0), _utc(2026, 11, 5, 2, 0)
    parts = split_at_midnight(start, end)
    assert [local_day(a) for a, _ in parts] == [date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]
    assert parts[1][1] - parts[1][0] == timedelta(hours=24)
    with pytest.raises(ValueError):
        split_at_midnight(end, start)


@pytest.mark.parametrize(
    ("day", "length_h"), [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)], ids=["dst-start", "dst-end"]
)
def test_split_dst_days(day, length_h):
    start = _utc(day.year, day.month, day.day - 1, 23, 0)
    end = _utc(day.year, day.month, day.day + 1, 1, 0)
    parts = split_at_midnight(start, end)
    assert len(parts) == 3
    assert parts[1][1] - parts[1][0] == timedelta(hours=length_h)
    assert sum((b - a for a, b in parts), timedelta()) == end - start


# ---------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("2,5", 9000),
        ("2.5", 9000),
        ("2:30", 9000),
        ("02:30:00", 9000),
        ("150 min", 9000),
        ("150min", 9000),
        ("2,5 h", 9000),
        ("2h30", 9000),
        ("2 h 30 min", 9000),
        (",75", 2700),
        ("0:45", 2700),
        ("10", 36000),
        (2.5, 9000),
        (3, 10800),
        (timedelta(hours=1, minutes=5), 3900),
        (time(1, 30), 5400),
    ],
)
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("bad", ["", "abc", "2:75", "-1", "1,2,3", True])
def test_parse_duration_invalid(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("31.10.2026", date(2026, 10, 31)),
        ("1.2.2027", date(2027, 2, 1)),
        ("2026-10-31", date(2026, 10, 31)),
        ("31.10.26", date(2026, 10, 31)),
        ("31/10/2026", date(2026, 10, 31)),
        ("31.10.2026 08:15", date(2026, 10, 31)),
        ("2026-10-31T08:15:00", date(2026, 10, 31)),
        (46326, date(2026, 10, 31)),
        ("46326", date(2026, 10, 31)),
        (46326.75, date(2026, 10, 31)),
        (datetime(2026, 10, 31, 8), date(2026, 10, 31)),
        (date(2026, 10, 31), date(2026, 10, 31)),
    ],
)
def test_parse_date(value, expected):
    assert parse_date(value) == expected


@pytest.mark.parametrize("bad", ["31.02.2026", "2026-13-01", "gestern", "31.10", 0])
def test_parse_date_invalid(bad):
    with pytest.raises(ValueError):
        parse_date(bad)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("8:05", time(8, 5)),
        ("08:05:30", time(8, 5, 30)),
        ("8.05", time(8, 5)),
        ("8 Uhr", time(8, 0)),
        ("13", time(13, 0)),
        (0.5, time(12, 0)),
        (0.3125, time(7, 30)),
        (time(7, 45, 0, 500), time(7, 45)),
        (datetime(2026, 1, 1, 7, 45), time(7, 45)),
    ],
)
def test_parse_time(value, expected):
    assert parse_time(value) == expected


@pytest.mark.parametrize("bad", ["24:00", "7:60", "x", 1.5, 1.0, 0.99999999, "25:00", float("nan")])
def test_parse_time_invalid(bad):
    with pytest.raises(ValueError):
        parse_time(bad)


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("24:00", 86400), ("24:00:00", 86400), (1.0, 86400), (0.99999999, 86400), ("22:00", 79200), (0.5, 43200)],
)
def test_parse_time_seconds_accepts_end_of_day(value, seconds):
    assert parse_time_seconds(value, end_of_day=True) == seconds


@pytest.mark.parametrize("bad", ["24:01", "24:00:01", 1.01, -0.1])
def test_parse_time_seconds_rejects_beyond_end_of_day(bad):
    with pytest.raises(ValueError):
        parse_time_seconds(bad, end_of_day=True)
    with pytest.raises(ValueError):
        parse_time_seconds("24:00")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("20261031", date(2026, 10, 31)), (20261031, date(2026, 10, 31)), ("25569", date(1970, 1, 1))],
)
def test_parse_date_yyyymmdd_and_serial_window(value, expected):
    assert parse_date(value) == expected


@pytest.mark.parametrize("bad", ["20261341", 20261341, 1e9, "2026", 2026, 99999999.0, float("nan"), float("inf")])
def test_parse_date_rejects_implausible_numbers_with_value_error(bad):
    with pytest.raises(ValueError):
        parse_date(bad)


def test_day_types_in_school_term():
    _, days = _school_term()
    assert sum(1 for d in days if d.day_type == DayType.SCHULTAG) == 25


# ---------------------------------------------------------------- soll: no summation drift (§6.8)


@pytest.mark.parametrize("upz_ist", [23, 25, 26, 27])
def test_part_time_j_soll_is_exact_sum(upz_ist):
    p = SollParams(upz_ist=upz_ist)
    days = make_days(date(2026, 8, 1), date(2027, 7, 31), school_year_2026_ferien())
    n_soll_days = sum(1 for d in days if day_soll(d, FULL) > 0)
    expected = round_half_up(Fraction(n_soll_days * 8 * H * upz_ist, 28))
    ledger = build_ledger(days, p, {})
    assert period_soll(days, p) == j_soll(ledger) == sum(day_solls(days, p)) == expected
    assert all(abs(r.soll - tagessoll_exact(p)) < 1 for r in ledger if r.soll)
    # Every prefix of the ledger is the rounded exact prefix sum.
    running = 0
    for k, r in enumerate((r for r in ledger if r.soll), start=1):
        running += r.soll
        assert running == round_half_up(tagessoll_exact(p) * k)


def test_part_time_25_of_28_reaches_1800h_for_252_soll_days():
    p = SollParams(upz_ist=25)
    days = make_days(date(2026, 8, 1), date(2027, 7, 31), school_year_2026_ferien())
    assert sum(1 for d in days if day_soll(d, FULL) > 0) == 252
    assert j_soll(build_ledger(days, p, {})) == 1800 * H  # per-day rounding gave 1799:58


# ---------------------------------------------------------------- leave: order, next summer, manual excess

OUTSIDE = "ausserhalb"


def test_sommerferien_zuerst_full_fallback_order():
    blocks, days = _year()
    kind_of = {b.id: b.kind for b in blocks}
    by_date = {d.date: d for d in days}
    sequence = []
    previous = plan_leave(days, blocks, LeaveMode.SOMMERFERIEN_ZUERST, n=30).days
    for n in range(31, len(leave_candidates(days)) + 1):
        current = plan_leave(days, blocks, LeaveMode.SOMMERFERIEN_ZUERST, n=n).days
        (added,) = current - previous
        sequence.append(kind_of.get(by_date[added].ferien_id, OUTSIDE))
        previous = current
    collapsed = [k for i, k in enumerate(sequence) if i == 0 or sequence[i - 1] != k]
    assert collapsed == [
        FerienKind.WEIHNACHTEN,
        FerienKind.PFINGSTEN,
        FerienKind.OSTERN,
        FerienKind.WINTER,
        FerienKind.HERBST,
        OUTSIDE,  # Buß- und Bettag
    ]


def test_sommerferien_zuerst_uses_weihnachten_before_next_summer_inside_period():
    blocks = [
        ferien("s23", "Sommerferien 2023", date(2023, 7, 31), date(2023, 9, 11)),
        ferien("w23", "Weihnachtsferien", date(2023, 12, 23), date(2024, 1, 5)),
        ferien("s24", "Sommerferien 2024", date(2024, 7, 29), date(2024, 9, 9)),
    ]
    days = make_days(date(2023, 8, 1), date(2024, 7, 31), blocks)
    summer_23 = [d for d in leave_candidates(days) if blocks[0].contains(d)]
    christmas = [d for d in leave_candidates(days) if blocks[1].contains(d)]
    assert len(summer_23) == 29  # 15.08. Mariä Himmelfahrt
    plan = plan_leave(days, blocks, LeaveMode.SOMMERFERIEN_ZUERST)
    assert plan.days == frozenset(summer_23) | {date(2023, 12, 27)}
    # The next summer is used only after all other ferien blocks.
    plan = plan_leave(days, blocks, LeaveMode.SOMMERFERIEN_ZUERST, n=len(summer_23) + len(christmas) + 1)
    assert date(2024, 7, 29) in plan.days and date(2024, 7, 30) not in plan.days


def test_manual_mode_reports_excess_selection():
    blocks, days = _year()
    summer = weekdays(date(2026, 8, 3), date(2026, 9, 11))
    christmas = [date(2026, 12, d) for d in (21, 22, 23, 24, 28)]
    plan = plan_leave(days, blocks, LeaveMode.MANUELL, manual=set(summer) | set(christmas))
    assert len(plan.days) == 35 and plan.excess == 5 and plan.shortfall == 0
    assert plan.noch_zuzuordnen == -5 and not plan.is_complete
    exact = plan_leave(days, blocks, LeaveMode.MANUELL, manual=set(summer))
    assert exact.excess == 0 and exact.noch_zuzuordnen == 0 and exact.is_complete
    assert plan_leave(days, blocks, LeaveMode.SOMMERFERIEN_ZUERST).excess == 0


# ---------------------------------------------------------------- ferien attribution follows day_type


def test_school_day_override_inside_ferien_is_not_ferien():
    summer = school_year_2026_ferien()[0]
    exam, extra = date(2026, 9, 10), date(2026, 10, 14)
    overrides = {
        exam: Override(exam, day_type=DayType.SCHULTAG),  # Nachprüfung
        extra: Override(extra, day_type=DayType.FERIENTAG),  # ferientag outside any block
    }
    days = make_days(date(2026, 8, 1), date(2026, 10, 31), [summer], overrides=overrides)
    by_date = {d.date: d for d in days}
    assert by_date[exam].day_type == DayType.SCHULTAG and by_date[exam].ferien_id is None
    assert by_date[extra].day_type == DayType.FERIENTAG and by_date[extra].ferien_id is None

    ledger = build_ledger(days, FULL, {exam: 8 * H, extra: 3 * H, date(2026, 8, 4): 2 * H})
    assert ferienarbeit(ledger) == 5 * H  # 2 h in summer + 3 h on the extra ferientag
    assert sum(m.ferienarbeit for m in month_summary(ledger)) == 5 * H
    (fc,) = holiday_forecast(ledger, date(2026, 8, 20), [summer])
    assert fc.abbau_kapazitaet == 15 * 8 * H  # 16 remaining weekdays minus the exam day


def test_absence_on_weekend_or_holiday_is_dropped():
    sat, xmas, wed = date(2026, 11, 7), date(2026, 12, 25), date(2026, 12, 23)
    overrides = {
        sat: Override(sat, absence=AbsenceType.URLAUB),
        xmas: Override(xmas, absence=AbsenceType.KRANK),
        wed: Override(wed, absence=AbsenceType.KRANK),
    }
    days = {d.date: d for d in make_days(sat, xmas, overrides=overrides)}
    assert days[sat].absence is None and days[xmas].absence is None
    assert days[wed].absence == AbsenceType.KRANK


# ---------------------------------------------------------------- holiday forecast: history, running block, chaining


def _herbst_history():
    """Previous school year with Herbstferien 2025 (12 h recorded) and the current one."""
    he25 = ferien("he25", "Herbstferien 2025", date(2025, 10, 27), date(2025, 10, 31))
    he26 = ferien("he26", "Herbstferien 2026", date(2026, 11, 2), date(2026, 11, 6))
    prev_days = make_days(date(2025, 8, 1), date(2026, 7, 31), [he25])
    prev = build_ledger(prev_days, FULL, {date(2025, 10, 27): 4 * H, date(2025, 10, 28): 8 * H})
    cur_days = make_days(date(2026, 8, 1), date(2027, 7, 31), [he26])
    return he25, he26, prev, cur_days


def test_holiday_forecast_default_from_previous_period_history():
    he25, he26, prev, cur_days = _herbst_history()
    ledger = build_ledger(cur_days, FULL, {})
    today = date(2026, 10, 20)
    he24 = ferien("he24", "Herbstferien 2024", date(2024, 10, 28), date(2024, 10, 31))  # no data
    history = ferien_work_history([*prev, *ledger], [he24, he25, he26], today)
    assert history == {FerienKind.HERBST: [12 * H]}  # he24 is not covered and does not count as 0

    (fc,) = holiday_forecast(ledger, today, [he26], history=history)
    assert fc.erwartete_ferienarbeit == 12 * H
    # The saldo comes from the current period only (no carry-over, Konzept §1.3).
    assert fc.saldo_vor == saldo_at(ledger, today) + fc.prognose_bis
    assert fc.saldo_nach == fc.saldo_vor - 40 * H + 12 * H
    assert default_ferienarbeit({FerienKind.HERBST: [12 * H, 6 * H]}, FerienKind.HERBST) == 9 * H

    # Without history the ledger alone has no past Herbstferien; a listed block without rows is ignored.
    (fc,) = holiday_forecast(ledger, today, [he25, he26])
    assert fc.erwartete_ferienarbeit == 0


def test_holiday_forecast_running_block_subtracts_work_already_done():
    he25, he26, prev, cur_days = _herbst_history()
    today = date(2026, 11, 3)  # Tuesday in the Herbstferien
    ledger = build_ledger(cur_days, FULL, {date(2026, 11, 2): 4 * H, today: 4 * H})
    history = ferien_work_history([*prev, *ledger], [he25, he26], today)
    assert history == {FerienKind.HERBST: [12 * H]}  # the running block is not history

    (fc,) = holiday_forecast(ledger, today, [he26], history=history)
    assert fc.abbau_kapazitaet == 24 * H and fc.prognose_bis == 0
    assert fc.ferienarbeit_bisher == 8 * H
    assert fc.erwartete_ferienarbeit == 4 * H  # 12 h expected in total, 8 h already in saldo_heute
    assert fc.saldo_nach == saldo_at(ledger, today) - 24 * H + 4 * H

    (fc,) = holiday_forecast(ledger, today, [he26], planned_work={"he26": 6 * H})
    assert fc.erwartete_ferienarbeit == 0  # more done than planned: nothing further expected


def test_holiday_forecast_chains_consecutive_blocks():
    blocks = school_year_2026_ferien() + [ferien("he", "Herbstferien", date(2026, 11, 2), date(2026, 11, 6))]
    days = make_days(date(2026, 8, 1), date(2027, 7, 31), blocks)
    today = date(2026, 10, 23)  # Friday
    ist = {d.date: 9 * H for d in days if d.day_type == DayType.SCHULTAG and d.date <= today}
    ledger = build_ledger(days, FULL, ist)
    by_id = {fc.ferien_id: fc for fc in holiday_forecast(ledger, today, blocks, planned_work={"we": 5 * H})}
    he, we = by_id["he"], by_id["we"]

    assert he.prognose_bis == 5 * H  # 26.–30.10.: 5 school days at +1 h
    assert he.saldo_vor == saldo_at(ledger, today) + 5 * H
    assert he.erwartete_ferienarbeit == 0  # no earlier Herbstferien
    assert he.saldo_nach == he.saldo_vor - 40 * H

    # 09.11.–18.12.: 29 school days at +1 h and Buß- und Bettag (−8 h); Herbst days not counted again.
    assert we.prognose_bis == 29 * H - 8 * H
    assert we.saldo_vor == he.saldo_nach + we.prognose_bis
    assert we.abbau_kapazitaet == 8 * 8 * H  # 10 weekdays minus 25.12. and 01.01.
    assert we.saldo_nach == we.saldo_vor - 64 * H + 5 * H
    assert list(by_id) == ["he", "we", "wi", "os", "pf"]


# ---------------------------------------------------------------- EU mean: krank, weekends, holidays


def _weekdays_back(today, count):
    out, d = [], today
    while len(out) < count:
        if d.isoweekday() <= 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def test_eu_mean_krank_days_are_neutral_like_urlaub():
    today = date(2026, 10, 29)
    absent = _weekdays_back(today, 30)
    absences = {d: AbsenceType.URLAUB for d in absent[:25]} | {d: AbsenceType.KRANK for d in absent[25:]}
    result = eu_mittel({today - timedelta(days=100): 1840 * H}, absences, today)
    assert result.absence_days == 30 and result.divisor_weeks == 46
    assert result.hours == pytest.approx(40.0)


def test_eu_mean_ignores_absences_on_weekends_and_holidays():
    today = date(2026, 10, 29)
    absences = {
        date(2025, 12, 24): AbsenceType.KRANK,  # Wednesday: counts
        date(2025, 12, 25): AbsenceType.KRANK,  # holiday
        date(2025, 12, 27): AbsenceType.KRANK,  # Saturday
        date(2026, 10, 25): AbsenceType.URLAUB,  # Sunday
        date(2026, 10, 26): AbsenceType.FORTBILDUNG,  # not neutral
    }
    holidays = holidays_between(today - timedelta(days=364), today)
    result = eu_mittel({}, absences, today, holidays=holidays)
    assert result.absence_days == 1 and result.divisor_weeks == pytest.approx(51.8)
    assert eu_mittel({}, absences, today).absence_days == 2  # without holiday info only weekends drop
