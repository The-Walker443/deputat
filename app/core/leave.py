"""Assignment of the 30 leave days to school-free working days, Konzept §6.2.

Candidates are Mon–Fri days of type ``ferientag`` or ``unterrichtsfrei`` that
are no holiday. Days already carrying a different absence (e.g. ``krank``)
are not candidates either: a sick day cannot be counted as leave.

Modes
-----
``sommerferien_zuerst``
    Summer holiday candidates chronologically, then Weihnachten, Pfingsten,
    Ostern, Winter (Fasching), Herbst, then other ferien blocks, then
    unterrichtsfrei days outside any ferien block (each group chronologically).
    "Summer" is the summer block of the period's first summer candidate (in a
    school year the one at its start, Konzept §1.3). A later summer block that
    already starts before the period ends (Bavaria: e.g. 29.07.2024) belongs
    to the next leave year and is used only after all other ferien blocks.
``proportional``
    ``n`` days distributed over the ferien blocks proportionally to their
    number of candidate days (largest remainder method, exact fractions).
    Remaining seats go to the largest remainder; ties are broken by the larger
    block, then by the earlier block start. Within a block the days are taken
    chronologically. With Konzept T9 (blocks 30/10/5/10/10 in chronological
    order Sommer, Weihnachten, Winter, Ostern, Pfingsten) the quotas are
    13.85/4.62/2.31/4.62/4.62; the three seats left after flooring go to
    Sommer (.85) and — tie .62 at equal size — to the two earlier 10-day
    blocks, giving 14/5/2/5/4. Candidates outside a ferien block (e.g. Buß-
    und Bettag) are used only if the ferien blocks do not offer ``n`` days.
``manuell``
    The user's selection; non-candidate dates are rejected (reported as
    ``invalid``) and never assigned. Valid dates beyond ``n`` are kept (the
    core does not pick which ones to drop) but reported as ``excess``; a plan
    with ``excess > 0`` must not be saved (:attr:`LeavePlan.is_complete`).

If there are fewer candidates than ``n`` all of them are assigned and the
shortfall is reported in :class:`LeavePlan`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from .types import AbsenceType, Day, DayType, FerienKind, FerienRange, LeaveMode

DEFAULT_URLAUBSTAGE = 30

SOMMERFERIEN_ZUERST_ORDER: tuple[FerienKind, ...] = (
    FerienKind.SOMMER,
    FerienKind.WEIHNACHTEN,
    FerienKind.PFINGSTEN,
    FerienKind.OSTERN,
    FerienKind.WINTER,
    FerienKind.HERBST,
    FerienKind.SONSTIGE,
)


@dataclass(frozen=True)
class LeavePlan:
    days: frozenset[date]
    requested: int
    candidates: int
    shortfall: int  # requested days that could not be placed
    invalid: frozenset[date] = frozenset()  # manual mode: rejected dates
    excess: int = 0  # manual mode: valid dates selected beyond ``requested``

    @property
    def noch_zuzuordnen(self) -> int:
        """UI figure "noch zuzuordnen: n"; negative if too many days are selected."""
        return self.requested - len(self.days)

    @property
    def is_complete(self) -> bool:
        """Exactly ``requested`` valid days assigned."""
        return self.shortfall == 0 and self.excess == 0 and not self.invalid


def is_leave_candidate(day: Day) -> bool:
    return (
        day.day_type in (DayType.FERIENTAG, DayType.UNTERRICHTSFREI)
        and not day.is_holiday
        and day.date.isoweekday() <= 5
        and day.absence in (None, AbsenceType.URLAUB)
    )


def leave_candidates(days: Iterable[Day]) -> list[date]:
    """Chronological list of dates that may carry a leave day."""
    return sorted(d.date for d in days if is_leave_candidate(d))


def _sommerferien_zuerst(cands: Sequence[Day], kind_of: dict[str, FerienKind], n: int) -> list[date]:
    rank = {kind: i for i, kind in enumerate(SOMMERFERIEN_ZUERST_ORDER)}
    later_summer = len(SOMMERFERIEN_ZUERST_ORDER)
    outside = later_summer + 1
    ordered = sorted(cands, key=lambda d: d.date)
    summer_ids = [d.ferien_id for d in ordered if d.ferien_id and kind_of.get(d.ferien_id) is FerienKind.SOMMER]
    primary_summer = summer_ids[0] if summer_ids else None

    def key(day: Day) -> tuple[int, date]:
        kind = kind_of.get(day.ferien_id) if day.ferien_id is not None else None
        if kind is None:
            return (outside, day.date)
        if kind is FerienKind.SOMMER and day.ferien_id != primary_summer:
            return (later_summer, day.date)
        return (rank[kind], day.date)

    return [d.date for d in sorted(ordered, key=key)[:n]]


def _proportional(cands: Sequence[Day], n: int) -> list[date]:
    blocks: dict[str, list[date]] = {}
    outside: list[date] = []
    for day in sorted(cands, key=lambda d: d.date):
        if day.ferien_id is None:
            outside.append(day.date)
        else:
            blocks.setdefault(day.ferien_id, []).append(day.date)

    total = sum(len(v) for v in blocks.values())
    if total == 0:
        return outside[:n]
    if total <= n:
        chosen = [d for v in blocks.values() for d in v]
        return chosen + outside[: n - total]

    quotas = {bid: Fraction(n * len(v), total) for bid, v in blocks.items()}
    seats = {bid: int(q) for bid, q in quotas.items()}
    remaining = n - sum(seats.values())
    order = sorted(
        blocks,
        key=lambda bid: (-(quotas[bid] - seats[bid]), -len(blocks[bid]), blocks[bid][0], bid),
    )
    for bid in order[:remaining]:
        seats[bid] += 1
    return [d for bid, v in blocks.items() for d in v[: seats[bid]]]


def plan_leave(
    days: Iterable[Day],
    ferien: Iterable[FerienRange],
    mode: LeaveMode | str,
    n: int = DEFAULT_URLAUBSTAGE,
    manual: Iterable[date] = frozenset(),
) -> LeavePlan:
    """Assign ``n`` leave days according to ``mode`` and report the outcome."""
    if n < 0:
        raise ValueError("n must not be negative")
    mode = LeaveMode(mode)
    cands = [d for d in days if is_leave_candidate(d)]
    cand_dates = {d.date for d in cands}
    kind_of = {f.id: FerienKind(f.kind) for f in ferien}

    invalid: frozenset[date] = frozenset()
    excess = 0
    if mode is LeaveMode.SOMMERFERIEN_ZUERST:
        chosen = _sommerferien_zuerst(cands, kind_of, n)
    elif mode is LeaveMode.PROPORTIONAL:
        chosen = _proportional(cands, n)
    else:
        manual_set = frozenset(manual)
        invalid = frozenset(d for d in manual_set if d not in cand_dates)
        chosen = sorted(manual_set - invalid)
        excess = max(0, len(chosen) - n)

    assigned = frozenset(chosen)
    return LeavePlan(
        days=assigned,
        requested=n,
        candidates=len(cand_dates),
        shortfall=max(0, n - len(assigned)),
        invalid=invalid,
        excess=excess,
    )


def assign_leave(
    days: Iterable[Day],
    ferien: Iterable[FerienRange],
    mode: LeaveMode | str,
    n: int = DEFAULT_URLAUBSTAGE,
    manual: Iterable[date] = frozenset(),
) -> frozenset[date]:
    """Convenience wrapper around :func:`plan_leave` returning only the dates."""
    return plan_leave(days, ferien, mode, n, manual).days
