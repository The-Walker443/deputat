"""Bavarian public holidays, school-free days, holiday classification and day materialisation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, timedelta

from .types import AbsenceType, Day, DayType, FerienKind, FerienRange, HolidayScope, Override

# (name, scope) per date; scope is a HolidayScope value.
HolidayMap = dict[date, tuple[str, str]]

REGIONAL_MARIAE_HIMMELFAHRT = "mariae_himmelfahrt"
REGIONAL_FRIEDENSFEST = "friedensfest"

_HOLIDAY_SCOPES = {HolidayScope.GESETZLICH, HolidayScope.REGIONAL}


def easter_sunday(year: int) -> date:
    """Easter Sunday (Gregorian calendar, anonymous Gregorian / Gauss algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 - name from the algorithm
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def buss_und_bettag(year: int) -> date:
    """Buß- und Bettag: the Wednesday strictly before 23 November."""
    ref = date(year, 11, 23)
    # Wednesday is weekday() == 2; go back at least one day.
    back = (ref.weekday() - 2) % 7 or 7
    return ref - timedelta(days=back)


def bavarian_holidays(
    year: int,
    regional: Iterable[str] = frozenset(),
    *,
    include_unterrichtsfrei: bool = True,
) -> HolidayMap:
    """Holidays of Bavaria for ``year``.

    ``regional`` may contain ``'mariae_himmelfahrt'`` and/or ``'friedensfest'``.
    With ``include_unterrichtsfrei`` the Buß- und Bettag is included with scope
    ``'unterrichtsfrei'`` (school-free, but *not* a holiday: soll stays).
    """
    regional = frozenset(regional)
    easter = easter_sunday(year)
    g = HolidayScope.GESETZLICH.value
    result: HolidayMap = {
        date(year, 1, 1): ("Neujahr", g),
        date(year, 1, 6): ("Heilige Drei Könige", g),
        easter - timedelta(days=2): ("Karfreitag", g),
        easter + timedelta(days=1): ("Ostermontag", g),
        date(year, 5, 1): ("Tag der Arbeit", g),
        easter + timedelta(days=39): ("Christi Himmelfahrt", g),
        easter + timedelta(days=50): ("Pfingstmontag", g),
        easter + timedelta(days=60): ("Fronleichnam", g),
        date(year, 10, 3): ("Tag der Deutschen Einheit", g),
        date(year, 11, 1): ("Allerheiligen", g),
        date(year, 12, 25): ("1. Weihnachtstag", g),
        date(year, 12, 26): ("2. Weihnachtstag", g),
    }
    r = HolidayScope.REGIONAL.value
    if REGIONAL_MARIAE_HIMMELFAHRT in regional:
        result[date(year, 8, 15)] = ("Mariä Himmelfahrt", r)
    if REGIONAL_FRIEDENSFEST in regional:
        result[date(year, 8, 8)] = ("Augsburger Hohes Friedensfest", r)
    if include_unterrichtsfrei:
        result.setdefault(
            buss_und_bettag(year), ("Buß- und Bettag", HolidayScope.UNTERRICHTSFREI.value)
        )
    return dict(sorted(result.items()))


def holidays_between(
    start: date,
    end: date,
    regional: Iterable[str] = frozenset(),
    *,
    include_unterrichtsfrei: bool = True,
) -> HolidayMap:
    """All Bavarian holidays within ``[start, end]`` (may span several years)."""
    regional = frozenset(regional)
    result: HolidayMap = {}
    for year in range(start.year, end.year + 1):
        for d, value in bavarian_holidays(
            year, regional, include_unterrichtsfrei=include_unterrichtsfrei
        ).items():
            if start <= d <= end:
                result[d] = value
    return result


_FERIEN_KEYWORDS: tuple[tuple[tuple[str, ...], FerienKind], ...] = (
    (("weihnacht",), FerienKind.WEIHNACHTEN),
    (("herbst",), FerienKind.HERBST),
    (("winter", "fasching", "frühjahr", "fruehjahr", "fastnacht", "karneval"), FerienKind.WINTER),
    (("oster",), FerienKind.OSTERN),
    (("pfingst",), FerienKind.PFINGSTEN),
    (("sommer",), FerienKind.SOMMER),
)


def classify_ferien(name: str) -> FerienKind:
    """Map a holiday name (e.g. from an ICS file) to a :class:`FerienKind` by keyword."""
    lowered = name.casefold()
    for keywords, kind in _FERIEN_KEYWORDS:
        if any(k in lowered for k in keywords):
            return kind
    return FerienKind.SONSTIGE


def daterange(start: date, end: date) -> Iterable[date]:
    """Yield every date in ``[start, end]``."""
    for offset in range((end - start).days + 1):
        yield start + timedelta(days=offset)


def build_days(
    start: date,
    end: date,
    ferien: Iterable[FerienRange],
    holidays: Mapping[date, tuple[str, str]],
    overrides: Mapping[date, Override] | None = None,
    leave_days: Iterable[date] = frozenset(),
) -> list[Day]:
    """Materialise every date in ``[start, end]``.

    ``day_type`` precedence: wochenende > feiertag > override day_type >
    ferientag (date inside a ferien range) > unterrichtsfrei (holiday map entry
    with scope ``'unterrichtsfrei'``) > schultag.

    ``ferien_id`` is set for every date inside a ferien range (weekends and
    holidays included) so that holiday work can be attributed, except when an
    override turns the date into a ``schultag`` (e.g. Nachprüfungen in the
    summer): that day is no ferien day. An override ``ferientag`` outside any
    range keeps ``ferien_id=None``; the account still treats it as a ferien
    day by its type.

    ``absence`` comes from the override; otherwise ``urlaub`` if the date is in
    ``leave_days``. Absences only apply to working days: on weekends and
    holidays (no target time) any absence is dropped, so e.g. a sick note
    entered as a range over Christmas or an urlaub override on a Saturday
    never marks such a day.
    """
    if end < start:
        raise ValueError("end must not be before start")
    overrides = overrides or {}
    leave = frozenset(leave_days)
    ferien_sorted = sorted(ferien, key=lambda f: (f.start, f.end, f.id))

    days: list[Day] = []
    for d in daterange(start, end):
        holiday = holidays.get(d)
        holiday_name = holiday[0] if holiday else None
        scope = holiday[1] if holiday else None
        is_holiday = scope in _HOLIDAY_SCOPES
        ferien_range = next((f for f in ferien_sorted if f.contains(d)), None)
        override = overrides.get(d)

        if d.isoweekday() >= 6:
            day_type = DayType.WOCHENENDE
        elif is_holiday:
            day_type = DayType.FEIERTAG
        elif override is not None and override.day_type is not None:
            day_type = DayType(override.day_type)
        elif ferien_range is not None:
            day_type = DayType.FERIENTAG
        elif scope == HolidayScope.UNTERRICHTSFREI:
            day_type = DayType.UNTERRICHTSFREI
        else:
            day_type = DayType.SCHULTAG

        absence: AbsenceType | None = None
        if day_type in (DayType.WOCHENENDE, DayType.FEIERTAG):
            pass
        elif override is not None and override.absence is not None:
            absence = AbsenceType(override.absence)
        elif d in leave:
            absence = AbsenceType.URLAUB

        in_ferien = ferien_range is not None and day_type != DayType.SCHULTAG
        days.append(
            Day(
                date=d,
                day_type=day_type,
                absence=absence,
                ferien_id=ferien_range.id if in_ferien else None,
                holiday_name=holiday_name,
                is_holiday=is_holiday,
                note=override.note if override else None,
            )
        )
    return days
