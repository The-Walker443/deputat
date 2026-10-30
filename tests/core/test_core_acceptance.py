"""Acceptance criteria T1–T20 of Konzept §13 that belong to the calculation core."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.account import build_ledger, eu_mittel, holiday_forecast, saldo_at, j_soll
from app.core.calendar import bavarian_holidays, build_days
from app.core.hashing import HASH_FIELDS, canonical_entries, entries_hash
from app.core.leave import DEFAULT_URLAUBSTAGE, plan_leave
from app.core.mehrarbeit import month_mehrarbeit
from app.core.parsing import parse_duration
from app.core.soll import day_soll, period_soll, week_soll
from app.core.timesplit import is_nachtraeglich, split_at_midnight
from app.core.types import (
    FLAG_VERTRETUNG_ANGEORDNET,
    FLAG_VERTRETUNG_GENEHMIGT,
    AbsenceType,
    Ampel,
    DayType,
    LeaveMode,
    Override,
    SollParams,
)
from core_builders import H, ferien, make_days, school_year_2026_ferien, weekdays

BERLIN = ZoneInfo("Europe/Berlin")
FULL = SollParams()


def test_t01_school_week_full_time_soll_40h():
    days = make_days(date(2026, 10, 12), date(2026, 10, 18))
    assert all(d.day_type in (DayType.SCHULTAG, DayType.WOCHENENDE) for d in days)
    assert week_soll(days, FULL) == 40 * H


def test_t02_week_with_holiday_on_thursday_soll_32h():
    days = make_days(date(2026, 5, 11), date(2026, 5, 17))  # Christi Himmelfahrt = Thu 14.05.2026
    thursday = days[3]
    assert thursday.day_type == DayType.FEIERTAG and thursday.holiday_name == "Christi Himmelfahrt"
    assert week_soll(days, FULL) == 32 * H


def test_t03_holiday_week_without_leave_and_no_work_decreases_saldo_by_40h():
    herbst = ferien("he", "Herbstferien", date(2026, 11, 2), date(2026, 11, 6))
    days = make_days(date(2026, 11, 2), date(2026, 11, 8), [herbst])
    assert [d.day_type for d in days[:5]] == [DayType.FERIENTAG] * 5
    ledger = build_ledger(days, FULL, {})
    assert saldo_at(ledger, date(2026, 11, 8)) == -40 * H


def test_t04_assigned_leave_day_has_soll_0_and_keeps_saldo():
    herbst = ferien("he", "Herbstferien", date(2026, 11, 2), date(2026, 11, 6))
    day = date(2026, 11, 3)
    days = make_days(date(2026, 11, 2), date(2026, 11, 6), [herbst], leave={day})
    leave_day = next(d for d in days if d.date == day)
    assert leave_day.absence == AbsenceType.URLAUB
    assert day_soll(leave_day, FULL) == 0
    ledger = build_ledger(days, FULL, {})
    assert saldo_at(ledger, day) - saldo_at(ledger, day - timedelta(days=1)) == 0


def test_t05_school_week_with_50h_ist_gives_plus_10h():
    days = make_days(date(2026, 10, 12), date(2026, 10, 18))
    ist = {d: 10 * H for d in weekdays(date(2026, 10, 12), date(2026, 10, 16))}
    ledger = build_ledger(days, FULL, ist)
    assert saldo_at(ledger, date(2026, 10, 18)) == 10 * H


def test_t06_part_time_21_of_28():
    p = SollParams(upz_voll=28, upz_ist=21)
    days = make_days(date(2026, 10, 12), date(2026, 10, 18), leave=set())
    assert week_soll(days, p) == 30 * H

    herbst = ferien("he", "Herbstferien", date(2026, 11, 2), date(2026, 11, 6))
    days = make_days(date(2026, 11, 2), date(2026, 11, 6), [herbst], leave={date(2026, 11, 4)})
    assert day_soll(days[2], p) == 0
    assert day_soll(days[1], p) == 6 * H

    # Leave entitlement stays 30 days (5-day week) for part-time teachers: the leave
    # plan does not depend on f (plan_leave takes no SollParams; choosing n from the
    # teacher settings is service-level). End to end, the part-time account has the
    # same 30 soll-free leave days and J_soll scales exactly with f.
    blocks = school_year_2026_ferien()
    base = make_days(date(2026, 8, 1), date(2027, 7, 31), blocks)
    plan = plan_leave(base, blocks, LeaveMode.SOMMERFERIEN_ZUERST)
    assert plan.requested == DEFAULT_URLAUBSTAGE == 30 and len(plan.days) == 30
    year = make_days(date(2026, 8, 1), date(2027, 7, 31), blocks, leave=plan.days)
    part, full = build_ledger(year, p, {}), build_ledger(year, FULL, {})
    leave_rows = [r for r in part if r.absence == AbsenceType.URLAUB]
    assert len(leave_rows) == 30 and all(r.soll == 0 for r in leave_rows)
    assert j_soll(part) * 4 == j_soll(full) * 3


def test_t07_period_with_261_weekdays_11_holidays_30_leave_days_j_soll_1760h():
    start, end = date(2026, 1, 1), date(2026, 12, 31)
    holidays = bavarian_holidays(2026, {"mariae_himmelfahrt"}, include_unterrichtsfrei=False)
    # Synthetic additions so that exactly 11 holidays fall on weekdays.
    holidays[date(2026, 7, 15)] = ("Synthetischer Feiertag 1", "gesetzlich")
    holidays[date(2026, 9, 16)] = ("Synthetischer Feiertag 2", "gesetzlich")
    leave = set(weekdays(date(2026, 8, 3), date(2026, 9, 11)))

    days = build_days(start, end, [], holidays, {}, leave)
    assert sum(1 for d in days if d.date.isoweekday() <= 5) == 261
    assert sum(1 for d in days if d.is_holiday and d.date.isoweekday() <= 5) == 11
    assert sum(1 for d in days if d.absence == AbsenceType.URLAUB) == 30

    assert period_soll(days, FULL) == 1760 * H
    assert j_soll(build_ledger(days, FULL, {})) == 1760 * H


def test_t08_sommerferien_zuerst_puts_all_30_days_into_summer():
    blocks = school_year_2026_ferien() + [
        ferien("he", "Herbstferien", date(2026, 11, 2), date(2026, 11, 6))
    ]
    days = make_days(date(2026, 8, 1), date(2027, 7, 31), blocks)
    plan = plan_leave(days, blocks, LeaveMode.SOMMERFERIEN_ZUERST)
    summer = blocks[0]
    assert len(plan.days) == 30 and plan.shortfall == 0
    assert all(summer.contains(d) for d in plan.days)
    assert sorted(plan.days) == weekdays(summer.start, summer.end)


def test_t09_proportional_largest_remainder_30_10_5_10_10_gives_14_5_2_5_4():
    blocks = school_year_2026_ferien()
    days = make_days(date(2026, 8, 1), date(2027, 7, 31), blocks, holidays={})
    sizes = Counter(d.ferien_id for d in days if d.day_type == DayType.FERIENTAG)
    assert [sizes[b.id] for b in blocks] == [30, 10, 5, 10, 10]

    plan = plan_leave(days, blocks, LeaveMode.PROPORTIONAL)
    by_block = {b.id: sorted(d for d in plan.days if b.contains(d)) for b in blocks}
    assert [len(by_block[b.id]) for b in blocks] == [14, 5, 2, 5, 4]
    assert len(plan.days) == 30
    # chronological within each block
    for b in blocks:
        assert by_block[b.id] == weekdays(b.start, b.end)[: len(by_block[b.id])]


def test_t10_sick_school_day_neutralised():
    day = date(2026, 10, 14)
    days = make_days(
        date(2026, 10, 12), date(2026, 10, 16), overrides={day: Override(day, absence=AbsenceType.KRANK)}
    )
    ledger = build_ledger(days, FULL, {}, krank_neutralisiert=True)
    row = next(r for r in ledger if r.date == day)
    assert row.soll == 8 * H and row.ist == row.soll
    assert row.delta == 0


@pytest.mark.parametrize(("ustd", "expected"), [(3, 0.0), (4, 4.0)], ids=["t11", "t12"])
def test_t11_t12_mehrarbeit_bagatellgrenze(ustd, expected):
    entries = [(date(2026, 10, 5 + i), 45 * 60, {FLAG_VERTRETUNG_ANGEORDNET}) for i in range(ustd - 1)]
    entries.append((date(2026, 10, 20), 45 * 60, [FLAG_VERTRETUNG_GENEHMIGT]))
    result = month_mehrarbeit(entries)
    assert result.total == ustd
    assert result.ausgleichsfaehig == expected


def test_t13_timer_over_midnight_split_with_unchanged_sum():
    start = datetime(2026, 11, 3, 22, 30, tzinfo=BERLIN).astimezone(UTC).replace(tzinfo=None)
    end = datetime(2026, 11, 4, 1, 15, tzinfo=BERLIN).astimezone(UTC).replace(tzinfo=None)
    parts = split_at_midnight(start, end)
    assert len(parts) == 2

    local = [
        tuple(x.replace(tzinfo=UTC).astimezone(BERLIN) for x in part) for part in parts
    ]
    (a_start, a_end), (b_start, b_end) = local
    assert (a_start.date(), a_start.hour, a_start.minute) == (date(2026, 11, 3), 22, 30)
    # First part ends at the exclusive midnight boundary, i.e. inclusive 23:59:59 of 03.11.
    assert (a_end - timedelta(seconds=1)).strftime("%d.%m. %H:%M:%S") == "03.11. 23:59:59"
    assert (b_start.date(), b_start.hour, b_start.minute) == (date(2026, 11, 4), 0, 0)
    assert (b_end.hour, b_end.minute) == (1, 15)
    assert sum((b - a).total_seconds() for a, b in parts) == (end - start).total_seconds() == 2 * H + 45 * 60


COVERED_IN = "service-level behaviour, covered in"


@pytest.mark.skip(reason=f"T14 (revision with full snapshot, revision_no +1): {COVERED_IN} tests/services/test_entries_service.py"
    "::test_t14_update_creates_revision_with_full_previous_state")
def test_t14_revision_on_edit():
    pass


@pytest.mark.skip(reason=f"T15 (locked month: reason required, 'nachträglich korrigiert', old hash kept): {COVERED_IN} "
    "tests/services/test_entries_service.py::test_t15_locked_month_requires_reason_and_keeps_old_hash")
def test_t15_locked_month_requires_reason():
    pass


def test_t16_entry_created_three_days_later_is_nachtraeglich():
    entry_day = date(2026, 10, 5)
    created = datetime(2026, 10, 8, 12, 0, tzinfo=BERLIN)
    assert is_nachtraeglich(entry_day, created.astimezone(UTC).replace(tzinfo=None))
    # Boundary: 48 h after the end of the day (08.10. 00:00 local) is not yet "more than 48 h".
    assert not is_nachtraeglich(entry_day, datetime(2026, 10, 8, 0, 0, tzinfo=BERLIN))
    assert is_nachtraeglich(entry_day, datetime(2026, 10, 8, 0, 0, 1, tzinfo=BERLIN))
    assert not is_nachtraeglich(entry_day, datetime(2026, 10, 6, 9, 0, tzinfo=BERLIN))


@pytest.mark.skip(reason=f"T16 (flag 'nachtraeglich' set on create and not removable on edit): {COVERED_IN} "
    "tests/services/test_entries_service.py::test_t16_entry_created_three_days_later_is_nachtraeglich_and_sticky")
def test_t16_flag_set_and_not_removable():
    pass


def test_t17_import_day_total_decimal_comma():
    # Core half of T17: the duration parser.
    assert parse_duration("2,5") == 2 * H + 30 * 60


@pytest.mark.skip(reason=f"T17 (import creates entry with has_bounds=false and flag ohne_zeitgrenzen): {COVERED_IN} "
    "tests/services/test_import_service.py::test_t17_duration_without_times_becomes_unbounded_entry")
def test_t17_import_entry_without_bounds():
    pass


def test_t18_eu_mean_2020h_with_6_leave_weeks():
    today = date(2026, 10, 29)
    window = [today - timedelta(days=i) for i in range(365)]
    work_days = [d for d in window if d.isoweekday() <= 5][:202]
    ist = {d: 10 * H for d in work_days}  # 202 × 10 h = 2,020 h
    leave_days = [d for d in window if d.isoweekday() <= 5 and d not in ist][:30]
    absences = {d: AbsenceType.URLAUB for d in leave_days}
    absences[today - timedelta(days=400)] = AbsenceType.URLAUB  # outside the window

    result = eu_mittel(ist, absences, today)
    assert result.ist_seconds == 2020 * H
    assert result.divisor_weeks == 46
    assert result.hours == pytest.approx(43.913, abs=1e-3)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "OPEN SPEC DECISION: Konzept §13 T18 expects '≈ 43,9 h → gelb', but §6.5 defines "
        "gruen < 44 ≤ gelb ≤ 48 < rot, so 43.91 h is 'gruen'. The §6.5 thresholds are "
        "implemented as written until the product owner decides."
    ),
)
def test_t18_ampel_expectation_gelb():
    today = date(2026, 10, 29)
    window = [today - timedelta(days=i) for i in range(365)]
    work_days = [d for d in window if d.isoweekday() <= 5][:202]
    leave_days = [d for d in window if d.isoweekday() <= 5][202:232]
    result = eu_mittel({d: 10 * H for d in work_days}, {d: AbsenceType.URLAUB for d in leave_days}, today)
    assert result.ampel == Ampel.GELB


def test_t19_holiday_credit_calculator_plus_83_minus_40_plus_12():
    herbst = ferien("he", "Herbstferien", date(2026, 11, 2), date(2026, 11, 6))
    days = make_days(date(2026, 10, 26), date(2026, 11, 8), [herbst])
    week = weekdays(date(2026, 10, 26), date(2026, 10, 30))
    ist = {d: (8 * H) + (83 * H) // 5 for d in week}  # 40 h soll + 83 h surplus
    ledger = build_ledger(days, FULL, ist)
    today = date(2026, 10, 30)
    assert saldo_at(ledger, today) == 83 * H

    (fc,) = holiday_forecast(ledger, today, [herbst], planned_work={"he": 12 * H})
    assert fc.prognose_bis == 0
    assert fc.saldo_vor == 83 * H
    assert fc.abbau_kapazitaet == 40 * H
    assert fc.saldo_nach == 55 * H


def _entry(i: int, **overrides):
    base = {
        "id": f"0190-{i:04d}",
        "day": date(2026, 10, i),
        "start_at": datetime(2026, 10, i, 6, 0),
        "end_at": datetime(2026, 10, i, 8, 30),
        "duration_seconds": 9000,
        "has_bounds": True,
        "category_id": 3,
        "subcategory_id": None,
        "klasse": "3b",
        "fach": "Mathematik",
        "location": "schule",
        "note": "Korrektur Probe",
        "flags": ["nachtraeglich", "korrigiert"],
        "source": "timer",
        "status": "active",
        "import_batch_id": None,
        "created_at": datetime(2026, 10, i, 8, 31),
        "updated_at": datetime(2026, 10, i, 8, 31),
        "locked": False,
    }
    base.update(overrides)
    return base


def test_t20_month_hash_deterministic_and_change_sensitive():
    entries = [_entry(i) for i in range(1, 6)]
    h1 = entries_hash(entries)
    # Same data in another order, flags reordered, aware datetimes: same hash.
    shuffled = [dict(e) for e in reversed(entries)]
    shuffled[0]["flags"] = list(reversed(shuffled[0]["flags"]))
    shuffled[1]["start_at"] = shuffled[1]["start_at"].replace(tzinfo=UTC).astimezone(BERLIN)
    assert entries_hash(shuffled) == h1
    assert len(h1) == 64
    # Closing the month (locked/updated_at) does not change the hash.
    assert entries_hash([{**e, "locked": True, "updated_at": datetime(2026, 11, 1)} for e in entries]) == h1

    seen = {h1}
    for field, value in T20_CHANGES.items():
        modified = [dict(e) for e in entries]
        modified[2][field] = value
        h = entries_hash(modified)
        assert h not in seen, field
        seen.add(h)
    assert entries_hash(entries[:-1]) not in seen
    assert canonical_entries(entries).startswith(b'{"v":1,"entries":[{"id":"0190-0001"')


# One change per field of the closed-month evidence (Konzept §4, §10.1). The list is spelled
# out here on purpose: dropping a field from HASH_FIELDS must make T20 fail.
T20_CHANGES = {
    "id": "0190-9999",
    "day": date(2026, 10, 4),
    "start_at": datetime(2026, 10, 3, 6, 1),
    "end_at": datetime(2026, 10, 3, 8, 31),
    "duration_seconds": 9001,
    "has_bounds": False,
    "category_id": 4,
    "subcategory_id": 41,
    "klasse": "3a",
    "fach": "Deutsch",
    "location": "zuhause",
    "note": "Korrektur Probe.",
    "flags": ["nachtraeglich"],
    "source": "manuell",
    "status": "verworfen",
    "import_batch_id": "0190-batch",
    "created_at": datetime(2026, 10, 3, 8, 32),
    "deleted_at": datetime(2026, 10, 20, 9, 0),  # soft delete
}


@pytest.mark.parametrize("field", list(T20_CHANGES))
def test_t20_every_field_change_alters_hash(field):
    entries = [_entry(i) for i in range(1, 6)]
    modified = [dict(e) for e in entries]
    modified[2][field] = T20_CHANGES[field]
    assert entries_hash(modified) != entries_hash(entries)


def test_t20_change_list_covers_all_hash_fields():
    assert set(T20_CHANGES) == set(HASH_FIELDS)
