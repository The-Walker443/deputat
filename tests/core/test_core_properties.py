"""Property tests (hypothesis) from Konzept §13."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction

from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.account import build_ledger, j_soll, saldo_at, saldo_series
from app.core.calendar import build_days, holidays_between
from app.core.hashing import entries_hash
from app.core.leave import leave_candidates, plan_leave
from app.core.soll import day_soll, day_soll_exact, day_solls, period_soll, round_half_up
from app.core.timesplit import local_day, split_at_midnight
from app.core.types import AbsenceType, DayType, FerienKind, FerienRange, LeaveMode, Override, SollParams

KINDS = list(FerienKind)


@st.composite
def periods(draw):
    start = draw(st.dates(min_value=date(2020, 1, 1), max_value=date(2032, 12, 31)))
    length = draw(st.integers(min_value=1, max_value=400))
    end = start + timedelta(days=length - 1)
    ferien = []
    for i in range(draw(st.integers(min_value=0, max_value=6))):
        offset = draw(st.integers(min_value=0, max_value=length - 1))
        size = draw(st.integers(min_value=1, max_value=45))
        f_start = start + timedelta(days=offset)
        ferien.append(
            FerienRange(
                id=f"f{i}",
                name=f"Ferien {i}",
                kind=draw(st.sampled_from(KINDS)),
                start=f_start,
                end=min(end, f_start + timedelta(days=size - 1)),
            )
        )
    regional = draw(st.sets(st.sampled_from(["mariae_himmelfahrt", "friedensfest"])))
    overrides = {}
    for d in draw(st.lists(st.integers(min_value=0, max_value=length - 1), max_size=8)):
        day = start + timedelta(days=d)
        overrides[day] = Override(
            day,
            day_type=draw(st.sampled_from([None, DayType.SCHULTAG, DayType.UNTERRICHTSFREI, DayType.FERIENTAG])),
            absence=draw(st.none() | st.sampled_from(list(AbsenceType))),
        )
    params = SollParams(
        wochen_soll_voll_h=draw(st.sampled_from([40.0, 41.0, 42.5])),
        upz_voll=28,
        upz_ist=draw(st.integers(min_value=0, max_value=28)),
    )
    return start, end, ferien, holidays_between(start, end, regional), overrides, params


@settings(max_examples=150, deadline=None)
@given(periods(), st.sampled_from(list(LeaveMode)), st.integers(min_value=0, max_value=40), st.randoms())
def test_leave_never_on_weekend_or_holiday(period, mode, n, rnd):
    start, end, ferien, holidays, overrides, _ = period
    days = build_days(start, end, ferien, holidays, overrides, set())
    all_dates = [d.date for d in days]
    manual = set(rnd.sample(all_dates, min(len(all_dates), n)))
    plan = plan_leave(days, ferien, mode, n=n, manual=manual)

    by_date = {d.date: d for d in days}
    for d in plan.days:
        assert d.isoweekday() <= 5
        assert not by_date[d].is_holiday
        assert by_date[d].day_type in (DayType.FERIENTAG, DayType.UNTERRICHTSFREI)
    assert plan.days <= set(leave_candidates(days))
    if mode is not LeaveMode.MANUELL:
        assert len(plan.days) == min(n, plan.candidates)
        assert plan.shortfall == n - len(plan.days)

    # Materialised with the leave set, no absence (urlaub overrides included) lands on
    # weekends/holidays either.
    final = build_days(start, end, ferien, holidays, overrides, plan.days)
    for day in final:
        if day.absence is not None:
            assert day.date.isoweekday() <= 5 and not day.is_holiday
            assert day.day_type not in (DayType.WOCHENENDE, DayType.FEIERTAG)


@settings(max_examples=150, deadline=None)
@given(periods(), st.dictionaries(st.integers(0, 399), st.integers(0, 16 * 3600), max_size=60))
def test_sum_day_soll_equals_j_soll_and_soll_never_negative(period, ist_offsets):
    start, end, ferien, holidays, overrides, params = period
    days = build_days(start, end, ferien, holidays, overrides, set())
    solls = day_solls(days, params)
    assert all(s >= 0 for s in solls)
    assert all(s == 0 for d, s in zip(days, solls) if d.date.isoweekday() >= 6 or d.is_holiday)
    # No summation drift (§6.8): J_soll is the rounded exact sum, each day within 1 s of exact.
    exact = sum((day_soll_exact(d, params) for d in days), Fraction(0))
    assert sum(solls) == period_soll(days, params) == round_half_up(exact)
    assert all(abs(s - day_soll_exact(d, params)) < 1 for d, s in zip(days, solls))
    assert all(day_soll(d, params) == round_half_up(day_soll_exact(d, params)) for d in days)

    ist = {start + timedelta(days=o): s for o, s in ist_offsets.items()}
    ledger = build_ledger(days, params, ist)
    assert [r.soll for r in ledger] == solls
    assert j_soll(ledger) == period_soll(days, params)
    assert all(r.soll >= 0 for r in ledger)


@settings(max_examples=150, deadline=None)
@given(
    periods(),
    st.dictionaries(st.integers(0, 399), st.integers(0, 16 * 3600), max_size=60),
    st.integers(0, 399),
    st.integers(0, 399),
)
def test_saldo_is_additive_over_days(period, ist_offsets, a, b):
    start, end, ferien, holidays, overrides, params = period
    days = build_days(start, end, ferien, holidays, overrides, set())
    ist = {start + timedelta(days=o): s for o, s in ist_offsets.items()}
    ledger = build_ledger(days, params, ist)

    t1, t2 = sorted((start + timedelta(days=a), start + timedelta(days=b)))
    between = sum(r.ist - r.soll for r in ledger if t1 < r.date <= t2)
    assert saldo_at(ledger, t2) == saldo_at(ledger, t1) + between

    series = saldo_series(ledger)
    running = 0
    for row, (d, saldo) in zip(ledger, series):
        running += row.delta
        assert d == row.date and saldo == running


@settings(max_examples=100, deadline=None)
@given(periods())
def test_proportional_respects_quota_bounds(period):
    start, end, ferien, holidays, overrides, _ = period
    days = build_days(start, end, ferien, holidays, overrides, set())
    plan = plan_leave(days, ferien, LeaveMode.PROPORTIONAL, n=30)
    blocks: dict[str, int] = {}
    for d in days:
        if d.date in set(leave_candidates(days)) and d.ferien_id is not None:
            blocks[d.ferien_id] = blocks.get(d.ferien_id, 0) + 1
    total = sum(blocks.values())
    if total > 30:
        for fid, size in blocks.items():
            got = sum(1 for d in days if d.ferien_id == fid and d.date in plan.days)
            quota = Fraction(30 * size, total)
            assert int(quota) <= got <= int(quota) + 1


@settings(max_examples=200, deadline=None)
@given(
    st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2032, 12, 31)),
    st.integers(min_value=0, max_value=4 * 24 * 3600),
)
def test_split_at_midnight_preserves_duration_and_days(start, seconds):
    end = start + timedelta(seconds=seconds)
    parts = split_at_midnight(start, end)
    assert parts[0][0] == start and parts[-1][1] == end
    assert sum((b - a for a, b in parts), timedelta()) == end - start
    for (a, b), (c, _) in zip(parts, parts[1:]):
        assert b == c
    for a, b in parts:
        last = b - timedelta(microseconds=1) if b > a else b
        assert local_day(a) == local_day(last)


@settings(max_examples=50, deadline=None)
@given(st.lists(st.integers(min_value=0, max_value=10**6), min_size=1, max_size=20, unique=True), st.randoms())
def test_hash_is_order_independent(ids, rnd: random.Random):
    entries = [
        {
            "id": f"id-{i}",
            "start_at": datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=i),
            "duration_seconds": i,
            "flags": ["b", "a"],
        }
        for i in ids
    ]
    shuffled = entries[:]
    rnd.shuffle(shuffled)
    assert entries_hash(entries) == entries_hash(shuffled)
