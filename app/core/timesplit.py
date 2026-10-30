"""Splitting entries at local midnight and the "nachträglich" rule.

Intervals are half-open ``[start, end)``. A split part therefore ends exactly
at the next local midnight (00:00 of the following day), which guarantees
that the durations of the parts add up to the original duration to the
second (Konzept T13 "Summe unverändert"). The UI shows such an end as
"23:59:59" / "24:00" of the entry's day; the day assignment derived from
``start_at`` stays unique.

Datetimes may be naive (interpreted as UTC, as stored in the database) or
timezone-aware; results use the same convention as the input.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
NACHTRAEGLICH_NACH = timedelta(hours=48)


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def local_day(dt_utc: datetime) -> date:
    """Local (Europe/Berlin) calendar day of a UTC instant."""
    return _as_utc(dt_utc).astimezone(BERLIN).date()


def local_midnight_utc(d: date) -> datetime:
    """UTC instant of 00:00 local time on ``d`` (aware)."""
    return datetime.combine(d, time(0), tzinfo=BERLIN).astimezone(UTC)


def split_at_midnight(start_utc: datetime, end_utc: datetime) -> list[tuple[datetime, datetime]]:
    """Split ``[start, end)`` at every local midnight in between.

    Handles DST: boundaries are computed in Europe/Berlin and converted back
    to UTC, so a part may be 23 or 25 hours long on transition days.
    """
    naive = start_utc.tzinfo is None
    start, end = _as_utc(start_utc), _as_utc(end_utc)
    if end < start:
        raise ValueError("end must not be before start")

    parts: list[tuple[datetime, datetime]] = []
    cursor = start
    while True:
        boundary = local_midnight_utc(local_day(cursor) + timedelta(days=1))
        if end <= boundary:
            parts.append((cursor, end))
            break
        parts.append((cursor, boundary))
        cursor = boundary

    if naive:
        return [(a.replace(tzinfo=None), b.replace(tzinfo=None)) for a, b in parts]
    return parts


def is_nachtraeglich(entry_day: date, created_at_utc: datetime) -> bool:
    """True if the entry was created more than 48 h after the end of its local day."""
    day_end = local_midnight_utc(entry_day + timedelta(days=1))
    return _as_utc(created_at_utc) > day_end + NACHTRAEGLICH_NACH
