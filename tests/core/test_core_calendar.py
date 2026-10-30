from datetime import date

import pytest

from app.core.calendar import (
    bavarian_holidays,
    build_days,
    buss_und_bettag,
    classify_ferien,
    easter_sunday,
    holidays_between,
)
from app.core.types import AbsenceType, DayType, FerienKind, Override
from core_builders import ferien, make_days


@pytest.mark.parametrize(
    ("year", "expected"),
    [
        (2024, date(2024, 3, 31)),
        (2025, date(2025, 4, 20)),
        (2026, date(2026, 4, 5)),
        (2027, date(2027, 3, 28)),
        (2000, date(2000, 4, 23)),
        (2038, date(2038, 4, 25)),
    ],
)
def test_easter_sunday(year, expected):
    assert easter_sunday(year) == expected


@pytest.mark.parametrize(
    ("year", "expected"),
    [(2024, date(2024, 11, 20)), (2025, date(2025, 11, 19)), (2026, date(2026, 11, 18)), (2027, date(2027, 11, 17))],
)
def test_buss_und_bettag(year, expected):
    assert buss_und_bettag(year) == expected
    assert expected.isoweekday() == 3


def test_buss_und_bettag_when_23rd_is_wednesday():
    # 23.11.2033 is a Wednesday -> the Wednesday *before* is 16.11.
    assert date(2033, 11, 23).isoweekday() == 3
    assert buss_und_bettag(2033) == date(2033, 11, 16)


def test_bavarian_holidays_2026():
    hol = bavarian_holidays(2026, include_unterrichtsfrei=False)
    names = {d: n for d, (n, _) in hol.items()}
    assert names == {
        date(2026, 1, 1): "Neujahr",
        date(2026, 1, 6): "Heilige Drei Könige",
        date(2026, 4, 3): "Karfreitag",
        date(2026, 4, 6): "Ostermontag",
        date(2026, 5, 1): "Tag der Arbeit",
        date(2026, 5, 14): "Christi Himmelfahrt",
        date(2026, 5, 25): "Pfingstmontag",
        date(2026, 6, 4): "Fronleichnam",
        date(2026, 10, 3): "Tag der Deutschen Einheit",
        date(2026, 11, 1): "Allerheiligen",
        date(2026, 12, 25): "1. Weihnachtstag",
        date(2026, 12, 26): "2. Weihnachtstag",
    }
    assert all(scope == "gesetzlich" for _, scope in hol.values())


def test_regional_holidays_and_buss_und_bettag_scope():
    hol = bavarian_holidays(2027, {"mariae_himmelfahrt", "friedensfest"})
    assert hol[date(2027, 8, 15)] == ("Mariä Himmelfahrt", "regional")
    assert hol[date(2027, 8, 8)][1] == "regional"
    assert hol[date(2027, 11, 17)] == ("Buß- und Bettag", "unterrichtsfrei")
    assert date(2027, 8, 15) not in bavarian_holidays(2027)


def test_holidays_between_spans_years():
    hol = holidays_between(date(2026, 8, 1), date(2027, 7, 31), {"mariae_himmelfahrt"})
    assert date(2026, 8, 15) in hol and date(2027, 1, 6) in hol
    assert date(2026, 1, 6) not in hol and date(2027, 8, 15) not in hol


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("Herbstferien", FerienKind.HERBST),
        ("Weihnachtsferien 2026/27", FerienKind.WEIHNACHTEN),
        ("Frühjahrsferien", FerienKind.WINTER),
        ("Faschingsferien", FerienKind.WINTER),
        ("Winterferien", FerienKind.WINTER),
        ("Osterferien", FerienKind.OSTERN),
        ("PFINGSTFERIEN", FerienKind.PFINGSTEN),
        ("Sommerferien Bayern", FerienKind.SOMMER),
        ("Beweglicher Ferientag", FerienKind.SONSTIGE),
    ],
)
def test_classify_ferien(name, kind):
    assert classify_ferien(name) == kind


def test_build_days_precedence():
    start, end = date(2026, 10, 30), date(2026, 11, 20)
    herbst = ferien("he", "Herbstferien", date(2026, 11, 2), date(2026, 11, 6))
    overrides = {
        date(2026, 11, 3): Override(date(2026, 11, 3), day_type=DayType.SCHULTAG),  # override beats ferien
        date(2026, 11, 13): Override(date(2026, 11, 13), day_type=DayType.UNTERRICHTSFREI, note="Päd. Tag"),
        date(2026, 11, 7): Override(date(2026, 11, 7), day_type=DayType.SCHULTAG),  # weekend wins
        date(2026, 11, 10): Override(date(2026, 11, 10), absence=AbsenceType.FORTBILDUNG),
    }
    days = {d.date: d for d in make_days(start, end, [herbst], overrides=overrides, leave={date(2026, 11, 4)})}

    assert days[date(2026, 10, 30)].day_type == DayType.SCHULTAG
    allerheiligen = days[date(2026, 11, 1)]  # Sunday
    assert allerheiligen.day_type == DayType.WOCHENENDE and allerheiligen.is_holiday
    assert days[date(2026, 11, 2)].day_type == DayType.FERIENTAG
    assert days[date(2026, 11, 2)].ferien_id == "he"
    assert days[date(2026, 11, 3)].day_type == DayType.SCHULTAG
    assert days[date(2026, 11, 4)].absence == AbsenceType.URLAUB
    assert days[date(2026, 11, 7)].day_type == DayType.WOCHENENDE
    assert days[date(2026, 11, 7)].ferien_id is None
    assert days[date(2026, 11, 10)].absence == AbsenceType.FORTBILDUNG
    assert days[date(2026, 11, 13)].day_type == DayType.UNTERRICHTSFREI
    assert days[date(2026, 11, 13)].note == "Päd. Tag"
    bbt = days[date(2026, 11, 18)]
    assert bbt.day_type == DayType.UNTERRICHTSFREI and not bbt.is_holiday
    assert bbt.holiday_name == "Buß- und Bettag"


def test_build_days_holiday_beats_override_and_leave_not_on_holiday():
    d = date(2026, 12, 25)
    days = build_days(
        d, d, [], bavarian_holidays(2026), {d: Override(d, day_type=DayType.SCHULTAG)}, {d}
    )
    assert days[0].day_type == DayType.FEIERTAG and days[0].absence is None


def test_build_days_rejects_reversed_range():
    with pytest.raises(ValueError):
        build_days(date(2026, 1, 2), date(2026, 1, 1), [], {}, {}, set())
