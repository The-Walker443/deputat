"""Lenient parsers for importing existing records (Konzept §12).

All parsers raise :class:`ValueError` with a German message on invalid input
so the import wizard can show it per row.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from fractions import Fraction

EXCEL_EPOCH = date(1899, 12, 30)  # serial 0; accounts for Excel's 1900 leap-year bug for serials > 60

_DECIMAL_RE = re.compile(r"^(\d+(?:[.,]\d+)?|[.,]\d+)\s*(?:h|std\.?|stunden?)?$")
_HMS_RE = re.compile(r"^(\d+):([0-5]\d)(?::([0-5]\d))?\s*(?:h|std\.?)?$")
_MINUTES_RE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(?:m|min\.?|minuten?)$")
_H_MIN_RE = re.compile(r"^(\d+)\s*(?:h|std\.?)\s*(\d+)\s*(?:m|min\.?|minuten?)?$")


def _to_fraction(text: str) -> Fraction:
    text = text.replace(",", ".")
    return Fraction(("0" + text) if text.startswith(".") else text)


def _round_seconds(value: Fraction) -> int:
    return int((value + Fraction(1, 2)) // 1)


def parse_duration(value: str | int | float | timedelta | time) -> int:
    """Parse a duration into seconds.

    Accepted: decimal hours ``'2,5'`` / ``'2.5'`` / ``'2,5 h'``, ``'2:30'``
    (h:mm), ``'02:30:00'`` (h:mm:ss), minutes ``'150 min'``, ``'2h30'``,
    numbers (hours), ``timedelta`` and ``time`` (Excel duration cells).
    """
    if isinstance(value, timedelta):
        seconds = round(value.total_seconds())
    elif isinstance(value, time):
        seconds = value.hour * 3600 + value.minute * 60 + value.second
    elif isinstance(value, bool):
        raise ValueError(f"Ungültige Dauer: {value!r}")
    elif isinstance(value, (int, float)):
        seconds = _round_seconds(Fraction(str(value)) * 3600)
    else:
        text = str(value).strip().casefold()
        if not text:
            raise ValueError("Leere Dauer")
        if m := _HMS_RE.match(text):
            seconds = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3] or 0)
        elif m := _MINUTES_RE.match(text):
            seconds = _round_seconds(_to_fraction(m[1]) * 60)
        elif m := _H_MIN_RE.match(text):
            seconds = int(m[1]) * 3600 + int(m[2]) * 60
        elif m := _DECIMAL_RE.match(text):
            seconds = _round_seconds(_to_fraction(m[1]) * 3600)
        else:
            raise ValueError(f"Ungültige Dauer: {value!r}")
    if seconds < 0:
        raise ValueError(f"Negative Dauer: {value!r}")
    return seconds


# Plausible range for Excel date serials: 01.01.1970 .. 31.12.2099. Smaller
# numbers are almost always something else (a year "2026", a duration),
# larger ones overflow ``date``.
EXCEL_SERIAL_MIN = 25569
EXCEL_SERIAL_MAX = 73050


def excel_serial_to_date(serial: float) -> date:
    if not EXCEL_SERIAL_MIN <= serial < EXCEL_SERIAL_MAX + 1:  # also rejects NaN
        raise ValueError(f"Ungültige Excel-Seriennummer: {serial!r}")
    return EXCEL_EPOCH + timedelta(days=int(serial))


def _yyyymmdd(text: str) -> date:
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        raise ValueError(f"Ungültiges Datum: {text!r}") from None


_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"), "ymd"),
    (re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$"), "dmy"),
    (re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{2})$"), "dmy2"),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"), "dmy"),
)


def parse_date(value: str | int | float | date | datetime) -> date:
    """Parse ``dd.mm.yyyy``, ``yyyy-mm-dd``, ``dd.mm.yy`` (→ 20yy), ``dd/mm/yyyy``,
    ``yyyymmdd`` (8 digits, string or integer) or an Excel serial number
    (number or numeric string, 1970–2099). A trailing time part
    (``'31.10.2026 08:00'``, ISO ``T``) is ignored.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, bool):
        raise ValueError(f"Ungültiges Datum: {value!r}")
    if isinstance(value, int) and 10_000_000 <= value <= 99_999_999:
        return _yyyymmdd(str(value))
    if isinstance(value, (int, float)):
        return excel_serial_to_date(value)

    text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):
        return _yyyymmdd(text)
    if re.fullmatch(r"\d+(?:[.,]\d+)?", text) and not re.fullmatch(r"\d{1,2}\.\d{1,2}", text):
        return excel_serial_to_date(float(text.replace(",", ".")))
    text = re.split(r"[ T]", text, maxsplit=1)[0]
    for pattern, kind in _DATE_PATTERNS:
        if m := pattern.match(text):
            a, b, c = (int(x) for x in m.groups())
            try:
                if kind == "ymd":
                    return date(a, b, c)
                if kind == "dmy2":
                    return date(2000 + c, b, a)
                return date(c, b, a)
            except ValueError:
                break
    raise ValueError(f"Ungültiges Datum: {value!r}")


_TIME_RE = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?(?::(\d{2}))?\s*(?:uhr)?$")

DAY_SECONDS = 86400


def parse_time_seconds(value: str | int | float | time | datetime, *, end_of_day: bool = False) -> int:
    """Parse a clock time into seconds since local midnight.

    Accepts the same inputs as :func:`parse_time`. With ``end_of_day`` (for
    "Bis" columns) the end of the day is accepted as well and returned as
    86400: ``'24:00'``, ``'24:00:00'``, the Excel value ``1.0`` and Excel
    fractions that round up to a full day. Without it those raise
    :class:`ValueError` instead of silently wrapping to 00:00.
    """
    if isinstance(value, datetime):
        value = value.time()
    if isinstance(value, time):
        return value.hour * 3600 + value.minute * 60 + value.second
    if isinstance(value, bool):
        raise ValueError(f"Ungültige Uhrzeit: {value!r}")
    if isinstance(value, (int, float)):
        if not 0 <= value <= 1:  # also rejects NaN
            raise ValueError(f"Ungültige Uhrzeit: {value!r}")
        seconds = _round_seconds(Fraction(str(value)) * DAY_SECONDS)
    else:
        text = str(value).strip().casefold()
        m = _TIME_RE.match(text)
        if not m:
            raise ValueError(f"Ungültige Uhrzeit: {value!r}")
        h, mi, s = int(m[1]), int(m[2] or 0), int(m[3] or 0)
        if mi >= 60 or s >= 60 or h > 24:
            raise ValueError(f"Ungültige Uhrzeit: {value!r}")
        seconds = h * 3600 + mi * 60 + s
    if seconds > DAY_SECONDS or (seconds == DAY_SECONDS and not end_of_day):
        raise ValueError(f"Ungültige Uhrzeit: {value!r}")
    return seconds


def parse_time(value: str | int | float | time | datetime) -> time:
    """Parse ``'8:05'``, ``'08:05:30'``, ``'8.05'``, ``'8 Uhr'``, Excel time
    fractions (``0.5`` → 12:00) and ``time``/``datetime`` values.

    ``'24:00'`` / ``1.0`` cannot be a :class:`time` and raise; use
    :func:`parse_time_seconds` with ``end_of_day=True`` for end times.
    """
    seconds = parse_time_seconds(value)
    return time(seconds // 3600, seconds % 3600 // 60, seconds % 60)
