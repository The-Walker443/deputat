"""Reference target time (Referenz-Soll), Konzept §6.1.

Rounding (Konzept §6.8, "keine Rundung bei der Summenbildung"): the exact
day target is ``tagessoll_voll * f`` as a :class:`~fractions.Fraction`. Sums
(week, period) are formed from the exact values and rounded half up once.
For per-day integer values that must add up to such a sum (ledger rows,
materialised ``calendar_day.soll_seconds``) use :func:`day_solls`, which
distributes the rounding so that every prefix sum equals the rounded exact
prefix sum. For the usual factors (1.0, 0.75, 0.5) every value is exact anyway.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from fractions import Fraction

from .types import AbsenceType, Day, DayType, SollParams

SOLL_FREE_ABSENCES = frozenset(
    {
        AbsenceType.URLAUB,
        AbsenceType.DIENSTBEFREIUNG,
        AbsenceType.SONDERURLAUB,
        AbsenceType.ELTERNZEIT,
    }
)


def _frac(value: float | int) -> Fraction:
    # str() avoids binary float artefacts (40.0 -> 40, 21.5 -> 43/2).
    return Fraction(str(value)) if isinstance(value, float) else Fraction(value)


def round_half_up(value: Fraction) -> int:
    """Round to the nearest integer, exact halves towards +infinity."""
    return int((value + Fraction(1, 2)) // 1)


def teilzeitfaktor(p: SollParams) -> Fraction:
    """f = upz_ist / upz_voll (exact)."""
    return _frac(p.upz_ist) / _frac(p.upz_voll)


def tagessoll_voll_seconds(p: SollParams) -> int:
    """Full-time day target: wochen_soll_voll_h / 5 * 3600 (40 h -> 28,800 s)."""
    return round_half_up(_frac(p.wochen_soll_voll_h) * 3600 / 5)


def tagessoll_exact(p: SollParams) -> Fraction:
    """Exact day target of a regular working day: tagessoll_voll * f (seconds)."""
    return _frac(p.wochen_soll_voll_h) * 3600 / 5 * teilzeitfaktor(p)


def tagessoll_seconds(p: SollParams) -> int:
    """Day target rounded to whole seconds (for display of a single day)."""
    return round_half_up(tagessoll_exact(p))


def is_soll_free(day: Day) -> bool:
    """True for days whose target is 0 (weekend, holiday, leave-like absences)."""
    return (
        day.day_type in (DayType.WOCHENENDE, DayType.FEIERTAG)
        or day.date.isoweekday() >= 6
        or day.is_holiday
        or day.absence in SOLL_FREE_ABSENCES
    )


def day_soll_exact(day: Day, p: SollParams) -> Fraction:
    return Fraction(0) if is_soll_free(day) else tagessoll_exact(p)


def day_soll(day: Day, p: SollParams) -> int:
    """Target seconds for a single ``day`` (rounded; see module docstring for sums).

    Ferientage, unterrichtsfreie Tage, Fortbildung and Krank keep the full
    day target (sick days are neutralised on the *ist* side, see account.py).
    """
    return round_half_up(day_soll_exact(day, p))


def day_solls(days: Sequence[Day], p: SollParams) -> list[int]:
    """Integer day targets for ``days`` (in the given order) without summation drift.

    Value k is ``round(E_k) − round(E_{k−1})`` with ``E_k`` the exact sum of
    the first k targets, so every prefix sum is the rounded exact prefix sum,
    soll-free days stay 0 and each value differs from the exact one by < 1 s.
    """
    result: list[int] = []
    exact = Fraction(0)
    previous = 0
    for day in days:
        exact += day_soll_exact(day, p)
        rounded = round_half_up(exact)
        result.append(rounded - previous)
        previous = rounded
    return result


def week_soll(days: Iterable[Day], p: SollParams) -> int:
    """Sum of day targets of the given days (typically one ISO week), rounded once."""
    return round_half_up(sum((day_soll_exact(d, p) for d in days), Fraction(0)))


def period_soll(days: Iterable[Day], p: SollParams) -> int:
    """J_soll: sum of exact day targets over the period, rounded once."""
    return round_half_up(sum((day_soll_exact(d, p) for d in days), Fraction(0)))
