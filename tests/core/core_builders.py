"""Small builders shared by the core tests."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta

from app.core.calendar import build_days, classify_ferien, holidays_between
from app.core.types import Day, FerienRange, Override

H = 3600


def ferien(fid: str, name: str, start: date, end: date) -> FerienRange:
    return FerienRange(id=fid, name=name, kind=classify_ferien(name), start=start, end=end)


def make_days(
    start: date,
    end: date,
    ferien_ranges: Iterable[FerienRange] = (),
    holidays: Mapping[date, tuple[str, str]] | None = None,
    overrides: Mapping[date, Override] | None = None,
    leave: Iterable[date] = (),
    regional: frozenset[str] = frozenset({"mariae_himmelfahrt"}),
) -> list[Day]:
    if holidays is None:
        holidays = holidays_between(start, end, regional)
    return build_days(start, end, list(ferien_ranges), holidays, overrides or {}, set(leave))


def weekdays(start: date, end: date) -> list[date]:
    out = []
    d = start
    while d <= end:
        if d.isoweekday() <= 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def school_year_2026_ferien() -> list[FerienRange]:
    """Synthetic ferien blocks for 2026/27 (example data, not the official calendar).

    Weekday counts: Sommer 30, Weihnachten 10, Winter 5, Ostern 10, Pfingsten 10.
    """
    return [
        ferien("so", "Sommerferien 2026", date(2026, 8, 3), date(2026, 9, 11)),
        ferien("we", "Weihnachtsferien", date(2026, 12, 21), date(2027, 1, 1)),
        ferien("wi", "Frühjahrsferien (Fasching)", date(2027, 2, 8), date(2027, 2, 12)),
        ferien("os", "Osterferien", date(2027, 3, 22), date(2027, 4, 2)),
        ferien("pf", "Pfingstferien", date(2027, 5, 17), date(2027, 5, 28)),
    ]
