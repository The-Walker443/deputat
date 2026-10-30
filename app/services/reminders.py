"""In-app reminders (Konzept §3.2 Nr. 15): compact hints shown on every page (``GET /hinweise``).

:func:`collect` returns a list of dicts ``{key, level, text, link, link_label}``:

- ``key``    stable id of the *situation* (changes when the situation changes, e.g. another month);
             the browser hides dismissed keys for the rest of the session (sessionStorage)
- ``level``  ``info`` | ``warn`` | ``danger`` (maps to the ``banner-*`` classes of app.css)
- ``text``   German UI text, ``link`` relative URL (or None), ``link_label`` button text

Rules (each one is cheap: no full ledger computation, so the slot can load on every page):

1. no account period -> hint to create one (the other period-based rules are skipped)
2. timer running for more than 6 h
3. last school day before today (within the last 4 days) without any entry
4. school days with timetable that are not confirmed yet (``timetable.open_days``)
5. month closing due: the previous month is not closed after the 5th, older months with entries
   are not closed
6. the last backup failed, or no successful backup in the last 2 days

No web push (Konzept: optional, not implemented). Never raises: a failing rule is logged and skipped.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.types import AbsenceType, DayType
from app.db.models import BackupLog, CalendarDay, Category, EntryStatus, MonthClose, Period, Settings, TimeEntry
from app.util import local_date, now_utc, to_local

log = logging.getLogger("app.services.reminders")

LONG_TIMER_SECONDS = 6 * 3600
MISSING_DAY_LOOKBACK = 4  # days: Monday still reminds of Friday
MONTH_CLOSE_DAY = 5  # reminder from the 6th of the following month
BACKUP_MAX_AGE = timedelta(days=2)

LEVELS = ("danger", "warn", "info")
MONTH_NAMES = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)


def _hint(key: str, level: str, text: str, link: str | None = None, link_label: str | None = None) -> dict[str, Any]:
    return {"key": key, "level": level, "text": text, "link": link, "link_label": link_label}


def _date_de(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def _month_label(ym: str) -> str:
    return f"{MONTH_NAMES[int(ym[5:7]) - 1]} {ym[:4]}"


def _hours_text(seconds: int) -> str:
    minutes = seconds // 60
    return f"{minutes // 60}:{minutes % 60:02d}"


def _current_period(db: Session, today: date) -> Period | None:
    return db.scalars(select(Period).where(Period.start <= today, Period.end >= today)).first()


# --------------------------------------------------------------------------- rules


def _rule_timer(db: Session, now: datetime, today: date) -> list[dict[str, Any]]:
    entry = db.scalars(
        select(TimeEntry)
        .where(TimeEntry.end_at.is_(None), TimeEntry.deleted_at.is_(None), TimeEntry.status == EntryStatus.ACTIVE.value)
        .order_by(TimeEntry.start_at.desc())
    ).first()
    if entry is None:
        return []
    elapsed = int((now - entry.start_at).total_seconds())
    if elapsed <= LONG_TIMER_SECONDS:
        return []
    cat = db.get(Category, entry.category_id)
    name = f" ({cat.name})" if cat is not None else ""
    return [_hint(
        f"timer-lang:{entry.id}",
        "warn",
        f"Der Timer{name} läuft seit {_hours_text(elapsed)} h (Start {to_local(entry.start_at):%d.%m. %H:%M}). "
        "Noch aktiv? Sonst bitte beenden und die Endzeit korrigieren.",
        "/heute",
        "Zum Timer",
    )]


def _rule_missing_day(db: Session, now: datetime, today: date) -> list[dict[str, Any]]:
    lo = today - timedelta(days=MISSING_DAY_LOOKBACK)
    settings = db.get(Settings, 1)
    if settings is not None and settings.erfassung_ab is not None:
        lo = max(lo, settings.erfassung_ab)  # days before the start of recording are neutral
    day = db.scalars(
        select(CalendarDay)
        .where(CalendarDay.date >= lo, CalendarDay.date < today, CalendarDay.day_type == DayType.SCHULTAG.value)
        .order_by(CalendarDay.date.desc())
    ).first()
    if day is None:
        return []
    if day.absence_type and day.absence_type != AbsenceType.FORTBILDUNG.value:
        return []
    count = db.scalar(
        select(func.count()).select_from(TimeEntry).where(
            TimeEntry.day == day.date,
            TimeEntry.status == EntryStatus.ACTIVE.value,
            TimeEntry.deleted_at.is_(None),
        )
    )
    if count:
        return []
    when = "gestern" if day.date == today - timedelta(days=1) else f"am {_date_de(day.date)}"
    return [_hint(
        f"tag-ohne-eintrag:{day.date.isoformat()}",
        "info",
        f"Für den Schultag {when} ist noch nichts erfasst.",
        f"/eintraege/neu?datum={day.date.isoformat()}",
        "Nachtragen",
    )]


def _rule_unconfirmed(db: Session, now: datetime, today: date) -> list[dict[str, Any]]:
    from app.services import timetable as timetable_service

    yesterday = today - timedelta(days=1)
    result = timetable_service.open_days(db, until=yesterday, today=today)
    days = [d.date for d in result.days if d.unbestaetigt]
    if not days:
        return []
    if len(days) == 1:
        text = f"Der Stundenplan für {_date_de(days[0])} ist noch nicht bestätigt."
    else:
        text = f"{len(days)} Schultage mit Stundenplan sind noch nicht bestätigt (zuletzt {_date_de(days[-1])})."
    return [_hint(
        f"unbestaetigt:{len(days)}:{days[-1].isoformat()}",
        "info",
        text,
        f"/tag/{days[0].isoformat()}" if len(days) == 1 else "/tage",
        "Tag bestätigen" if len(days) == 1 else "Offene Tage",
    )]


def _months_with_entries(db: Session, start: date, end: date) -> set[str]:
    rows = db.scalars(
        select(TimeEntry.day).where(
            TimeEntry.day.between(start, end),
            TimeEntry.status == EntryStatus.ACTIVE.value,
            TimeEntry.deleted_at.is_(None),
        ).distinct()
    )
    return {f"{d:%Y-%m}" for d in rows}


def _rule_month_close(db: Session, now: datetime, today: date) -> list[dict[str, Any]]:
    period = _current_period(db, today)
    first_of_month = today.replace(day=1)
    prev_ym = f"{first_of_month - timedelta(days=1):%Y-%m}"
    lo = period.start if period is not None else (first_of_month - timedelta(days=1)).replace(day=1)
    if lo >= first_of_month:
        return []
    with_entries = _months_with_entries(db, lo, first_of_month - timedelta(days=1))
    closed = set(db.scalars(select(MonthClose.year_month)))
    due = sorted(
        ym for ym in with_entries - closed
        if ym < prev_ym or (ym == prev_ym and today.day > MONTH_CLOSE_DAY)
    )
    if not due:
        return []
    latest = due[-1]
    if len(due) == 1:
        text = f"Monatsabschluss für {_month_label(latest)} ist fällig."
    else:
        others = ", ".join(_month_label(ym) for ym in due[:-1])
        text = f"Monatsabschluss für {_month_label(latest)} ist fällig (außerdem offen: {others})."
    return [_hint(f"monatsabschluss:{','.join(due)}", "warn", text, f"/monate/{due[0]}", "Abschließen")]


def _rule_backup(db: Session, now: datetime, today: date) -> list[dict[str, Any]]:
    last = db.scalars(select(BackupLog).order_by(BackupLog.created_at.desc(), BackupLog.id.desc())).first()
    last_ok = db.scalars(
        select(BackupLog).where(BackupLog.ok.is_(True)).order_by(BackupLog.created_at.desc(), BackupLog.id.desc())
    ).first()
    if last is not None and not last.ok:
        detail = f": {last.message}" if last.message else ""
        return [_hint(
            f"backup-fehler:{last.id}",
            "danger",
            f"Die letzte Datensicherung am {to_local(last.created_at):%d.%m.%Y %H:%M} ist fehlgeschlagen{detail[:200]}.",
            "/export",
            "Sicherung",
        )]
    if last_ok is not None:
        if now - last_ok.created_at <= BACKUP_MAX_AGE:
            return []
        return [_hint(
            f"backup-alt:{last_ok.id}:{today.isoformat()}",
            "warn",
            f"Die letzte erfolgreiche Datensicherung ist vom {to_local(last_ok.created_at):%d.%m.%Y} "
            "(älter als 2 Tage).",
            "/export",
            "Sicherung",
        )]
    # Never backed up: only remind once data older than 2 days exists (not on a fresh install).
    oldest = db.scalar(select(func.min(TimeEntry.created_at)).where(TimeEntry.deleted_at.is_(None)))
    if oldest is None or now - oldest <= BACKUP_MAX_AGE:
        return []
    return [_hint(
        f"backup-keins:{today.isoformat()}",
        "warn",
        "Es gibt noch keine Datensicherung.",
        "/export",
        "Sicherung",
    )]


RULES: tuple[Callable[[Session, datetime, date], list[dict[str, Any]]], ...] = (
    _rule_timer,
    _rule_missing_day,
    _rule_unconfirmed,
    _rule_month_close,
    _rule_backup,
)


def collect(db: Session, now: datetime | None = None) -> list[dict[str, Any]]:
    """All current reminders, most severe first. ``now`` is naive UTC (default: now)."""
    now = (now or now_utc()).replace(microsecond=0)
    today = local_date(now)
    hints: list[dict[str, Any]] = []
    if _current_period(db, today) is None:
        hints.append(_hint(
            "keine-periode",
            "info",
            "Es ist noch kein Schuljahr angelegt – Soll, Saldo und Erinnerungen brauchen eine Kontoperiode.",
            "/kalender/einrichtung",
            "Einrichten",
        ))
        rules = (_rule_timer, _rule_backup)
    else:
        rules = RULES
    for rule in rules:
        try:
            hints.extend(rule(db, now, today))
        except Exception:  # a reminder must never break a page
            log.exception("Reminder rule %s failed", rule.__name__)
            db.rollback()
    hints.sort(key=lambda h: LEVELS.index(h["level"]))
    return hints
