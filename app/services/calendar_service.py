"""Calendar master data: periods, ferien (CRUD + ICS import), holidays, day overrides, leave
assignment and the materialisation of ``calendar_day``.

Data flow
---------
User input lives in ``period``, ``ferien``, ``holiday`` (computed rows have ``source='berechnet'``)
and ``day_override``. :func:`compute_days` turns that input into core :class:`~app.core.types.Day`
objects (via :func:`app.core.calendar.build_days` and :func:`app.core.leave.plan_leave`);
:func:`rematerialize` stores the result in ``calendar_day`` (day type, absence, ferien, soll).
Every mutating function here rematerialises the affected periods and commits.

Ferien may cross a period boundary (Bavaria: the summer holidays can start before 31.07.). For a
period all ferien rows intersecting its date range are used; rows of the period itself win over
overlapping rows attached to a neighbouring period.

Errors: :class:`CalendarError` (``ValueError``, German message, invalid input),
:class:`NotFoundError` and :class:`NoPeriodError` (``LookupError``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.calendar import HolidayMap, build_days, classify_ferien, holidays_between
from app.core.leave import LeavePlan, is_leave_candidate, plan_leave
from app.core.soll import day_solls
from app.core.types import (
    AbsenceType,
    Day,
    DayType,
    FerienKind,
    FerienRange,
    HolidayScope,
    LeaveMode,
    Override,
)
from app.db.models import (
    CalendarDay,
    DayOverride,
    Ferien,
    Holiday,
    MonthClose,
    MonthCloseStatus,
    Period,
    PeriodKind,
    Settings,
)
from app.services.settings_service import get_settings, soll_params
from app.util import LOCAL_TZ, now_utc, today_local

NO_PERIOD_HINT = "Bitte zuerst ein Schuljahr anlegen."

SOURCE_BERECHNET = "berechnet"
SOURCE_ICS = "ics"
SOURCE_MANUELL = "manuell"

# Day types a user may set via override (weekend/holiday always come from the calendar).
OVERRIDE_DAY_TYPES = frozenset({DayType.SCHULTAG, DayType.FERIENTAG, DayType.UNTERRICHTSFREI})
# Leave is assigned by the leave plan (or ``urlaub_manuell``), never as a plain absence override.
OVERRIDE_ABSENCES = frozenset(AbsenceType) - {AbsenceType.URLAUB}

HOLIDAY_SCOPES = frozenset({HolidayScope.GESETZLICH.value, HolidayScope.REGIONAL.value})

MAX_FERIEN_DAYS = 120
MAX_ICS_BYTES = 1_000_000
MAX_OVERRIDE_RANGE_DAYS = 366

LEAVE_MODE_LABELS = {
    LeaveMode.SOMMERFERIEN_ZUERST: "Urlaub = Sommerferien",
    LeaveMode.PROPORTIONAL: "Urlaub proportional verteilt",
    LeaveMode.MANUELL: "Urlaub manuell gewählt",
}

FERIEN_KIND_LABELS = {
    FerienKind.HERBST: "Herbstferien",
    FerienKind.WEIHNACHTEN: "Weihnachtsferien",
    FerienKind.WINTER: "Frühjahrs-/Faschingsferien",
    FerienKind.OSTERN: "Osterferien",
    FerienKind.PFINGSTEN: "Pfingstferien",
    FerienKind.SOMMER: "Sommerferien",
    FerienKind.SONSTIGE: "Sonstige Ferien",
}

_UNSET: Any = object()


class CalendarError(ValueError):
    """Invalid calendar input; the message is German and meant for the UI."""


class CalendarLocked(CalendarError):
    """The change alters a closed month (Soll/absences) and no reason was given (HTTP 423)."""

    status_code = 423


class CalendarConflict(CalendarError):
    """The change is not possible in the current state (HTTP 409)."""

    status_code = 409


class NotFoundError(LookupError):
    pass


class NoPeriodError(LookupError):
    def __init__(self, message: str = NO_PERIOD_HINT):
        super().__init__(message)


def fmt_date_de(d: date) -> str:
    return d.strftime("%d.%m.%Y")


# =========================================================================== periods

_SCHULJAHR_LABEL = re.compile(r"^(\d{4})\s*/\s*(\d{2}|\d{4})$")
_KALENDERJAHR_LABEL = re.compile(r"^(\d{4})$")


def _period_start(settings: Settings, year: int) -> date:
    month, day = (int(x) for x in (settings.periode_start_md or "08-01").split("-"))
    return date(year, month, day)


def _one_year_later(start: date) -> date:
    try:
        nxt = start.replace(year=start.year + 1)
    except ValueError:  # 29.02.
        nxt = start.replace(year=start.year + 1, day=28)
    return nxt - timedelta(days=1)


def schuljahr_label(start: date) -> str:
    return f"{start.year}/{(start.year + 1) % 100:02d}"


MIN_PERIOD_YEAR = 2000
MAX_PERIOD_YEAR = 2098


def _check_period_year(year: int) -> None:
    if not MIN_PERIOD_YEAR <= year <= MAX_PERIOD_YEAR:
        raise CalendarError(f"Das Jahr muss zwischen {MIN_PERIOD_YEAR} und {MAX_PERIOD_YEAR} liegen.")


def period_bounds_for_label(settings: Settings, label: str) -> tuple[date, date, str]:
    """``'2026/27'`` → school year from ``periode_start_md``; ``'2027'`` → calendar year."""
    label = label.strip()
    m = _SCHULJAHR_LABEL.match(label)
    if m:
        year = int(m.group(1))
        _check_period_year(year)
        second = int(m.group(2))
        if second % 100 != (year + 1) % 100:
            raise CalendarError(f"Ungültige Schuljahresbezeichnung „{label}“ (z. B. 2026/27).")
        start = _period_start(settings, year)
        return start, _one_year_later(start), PeriodKind.SCHULJAHR.value
    m = _KALENDERJAHR_LABEL.match(label)
    if m:
        year = int(m.group(1))
        _check_period_year(year)
        return date(year, 1, 1), date(year, 12, 31), PeriodKind.KALENDERJAHR.value
    raise CalendarError(f"Ungültige Bezeichnung „{label}“ – erwartet z. B. „2026/27“ oder „2027“.")


def suggest_period_label(db: Session, today: date | None = None) -> str:
    """Label of the period containing ``today`` according to the settings (for the create form)."""
    settings = get_settings(db)
    today = today or today_local()
    if settings.kontoperiode == PeriodKind.KALENDERJAHR.value:
        return str(today.year)
    start = _period_start(settings, today.year)
    year = today.year if today >= start else today.year - 1
    return schuljahr_label(_period_start(settings, year))


def create_period(
    db: Session,
    label: str | None = None,
    *,
    start: date | None = None,
    end: date | None = None,
    kind: str | None = None,
    today: date | None = None,
) -> Period:
    """Create a period and materialise its days.

    ``label`` like ``'2026/27'`` (school year from ``settings.periode_start_md``) or ``'2027'``
    (calendar year). Without a label the period containing ``today`` (per ``settings.kontoperiode``)
    is created. With explicit ``start``/``end`` a custom period is created (label optional).
    """
    settings = get_settings(db)
    if start is not None or end is not None:
        if start is None or end is None:
            raise CalendarError("Für einen eigenen Zeitraum bitte Beginn und Ende angeben.")
        if end < start:
            raise CalendarError("Das Ende der Periode liegt vor dem Beginn.")
        _check_period_year(start.year)
        _check_period_year(end.year)
        if (end - start).days > 400:
            raise CalendarError("Eine Kontoperiode darf höchstens ein Jahr umfassen.")
        kind = kind or settings.kontoperiode
        label = (label or "").strip() or (
            schuljahr_label(start) if kind == PeriodKind.SCHULJAHR.value else str(start.year)
        )
    else:
        label = label or suggest_period_label(db, today)
        start, end, label_kind = period_bounds_for_label(settings, label)
        kind = kind or label_kind
    if kind not in {k.value for k in PeriodKind}:
        raise CalendarError("Art der Periode muss „schuljahr“ oder „kalenderjahr“ sein.")
    if len(label) > 32:
        raise CalendarError("Die Bezeichnung ist zu lang (höchstens 32 Zeichen).")

    clash = db.scalars(select(Period).where(Period.start <= end, Period.end >= start)).first()
    if clash is not None:
        raise CalendarError(
            f"Der Zeitraum überschneidet sich mit „{clash.label}“ "
            f"({fmt_date_de(clash.start)}–{fmt_date_de(clash.end)})."
        )
    period = Period(label=label, start=start, end=end, kind=kind)
    db.add(period)
    db.flush()
    rematerialize(db, period)
    return period


def list_periods(db: Session) -> list[Period]:
    return list(db.scalars(select(Period).order_by(Period.start)))


def get_period(db: Session, period_id: str) -> Period:
    period = db.get(Period, period_id)
    if period is None:
        raise NotFoundError("Schuljahr nicht gefunden.")
    return period


def period_for_date(db: Session, d: date) -> Period | None:
    return db.scalars(select(Period).where(Period.start <= d, Period.end >= d)).first()


def get_current_period(db: Session, today: date | None = None) -> Period | None:
    """The period containing ``today`` (or ``None``)."""
    return period_for_date(db, today or today_local())


def resolve_period(db: Session, ref: str | None = None, today: date | None = None) -> Period:
    """Period by id, label or ``'current'``/``None``.

    Without a reference: the period containing ``today``, otherwise the latest period that has
    started, otherwise the earliest one. Raises :class:`NoPeriodError` if there is none.
    """
    today = today or today_local()
    if ref and ref != "current":
        period = db.get(Period, ref) or db.scalars(select(Period).where(Period.label == ref)).first()
        if period is None:
            raise NotFoundError(f"Schuljahr „{ref}“ nicht gefunden.")
        return period
    period = get_current_period(db, today)
    if period is not None:
        return period
    periods = list_periods(db)
    if not periods:
        raise NoPeriodError()
    started = [p for p in periods if p.start <= today]
    return started[-1] if started else periods[0]


def delete_period(db: Session, period_id: str) -> None:
    """Delete a period with its ferien and materialised days (time entries are untouched).

    Refused (:class:`CalendarConflict`) while the period contains a closed month: its Soll would vanish.
    """
    period = get_period(db, period_id)
    closed = sorted(
        db.scalars(
            select(MonthClose.year_month).where(
                MonthClose.year_month.between(f"{period.start:%Y-%m}", f"{period.end:%Y-%m}")
            )
        )
    )
    if closed:
        raise CalendarConflict(
            f"„{period.label}“ enthält abgeschlossene Monate ({', '.join(closed)}) und kann nicht gelöscht werden."
        )
    ferien_ids = [f.id for f in period.ferien]
    db.execute(delete(CalendarDay).where(CalendarDay.period_id == period.id))
    db.delete(period)
    _drop_schaetzung(db, ferien_ids)
    db.commit()


# =========================================================================== closed months


LOCKED_CALENDAR_MESSAGE = (
    "Die Änderung betrifft den abgeschlossenen Monat {months} (Soll/Abwesenheiten) – "
    "Änderung nur mit Begründung."
)


def closed_month_signature(db: Session) -> dict[str, tuple]:
    """Per closed month the calendar facts that drive its Soll: (date, day type, absence, ferien?, holiday?).

    Computed in memory from the current (flushed) source tables with the configured leave mode – a change of
    overrides, ferien or the leave assignment that touches a closed month changes its signature.
    """
    closed = list(db.scalars(select(MonthClose.year_month)))
    if not closed:
        return {}
    result: dict[str, list[tuple]] = {ym: [] for ym in closed}
    lo, hi = min(closed), max(closed)
    settings = get_settings(db)
    for period in db.scalars(select(Period).order_by(Period.start)):
        if f"{period.end:%Y-%m}" < lo or f"{period.start:%Y-%m}" > hi:
            continue
        for d in compute_days(db, period, settings=settings).days:
            ym = f"{d.date:%Y-%m}"
            if ym in result:
                result[ym].append((d.date, d.day_type.value, d.absence.value if d.absence else None,
                                   d.ferien_id is not None, bool(d.is_holiday)))
    return {ym: tuple(rows) for ym, rows in result.items()}


def guard_closed_months(db: Session, before: dict[str, tuple], reason: str | None) -> list[str]:
    """Compare with ``before`` (taken before the change, changes flushed since).

    Without a reason a change of a closed month rolls the session back and raises :class:`CalendarLocked`
    (423). With a reason every affected month is marked ``nachtraeglich_korrigiert`` and the change is
    recorded in its ``hash_history`` (hash unchanged, ``kind='kalender'``), so the report shows it.
    """
    if not before:
        return []
    db.flush()
    after = closed_month_signature(db)
    changed = sorted(ym for ym, sig in before.items() if after.get(ym) != sig)
    if not changed:
        return []
    reason = (reason or "").strip() or None
    if reason is None:
        db.rollback()
        raise CalendarLocked(LOCKED_CALENDAR_MESSAGE.format(months=", ".join(changed)))
    mark_closed_months_corrected(db, changed, f"Kalenderänderung: {reason}", kind="kalender")
    return changed


def mark_closed_months_corrected(db: Session, months: Iterable[str], reason: str, *, kind: str) -> None:
    """Mark closed months ``nachtraeglich_korrigiert`` after a change of their Soll/figures (hash unchanged).

    The change is recorded in ``hash_history`` (``kind`` 'kalender' / 'einstellungen'), so the report shows it.
    """
    now = now_utc().replace(microsecond=0)
    for ym in months:
        mc = db.get(MonthClose, ym)
        if mc is None:
            continue
        valid_from = mc.corrected_at or mc.closed_at
        history = list(mc.hash_history or [])
        history.append({
            "hash": mc.entries_hash_sha256,
            "closed_at": valid_from.isoformat() + "Z" if valid_from else None,
            "replaced_at": now.isoformat() + "Z",
            "reason": reason,
            "kind": kind,
        })
        mc.hash_history = history
        mc.status = MonthCloseStatus.NACHTRAEGLICH_KORRIGIERT.value
        mc.corrected_at = now


# =========================================================================== inputs → days


@dataclass
class CalendarInputs:
    """Raw calendar input for a date range (one query per table)."""

    start: date
    end: date
    ferien: list[Ferien]
    holidays: HolidayMap
    holiday_sources: dict[date, str]
    overrides: dict[date, DayOverride]


def load_inputs(db: Session, start: date, end: date) -> CalendarInputs:
    ferien = list(
        db.scalars(select(Ferien).where(Ferien.start <= end, Ferien.end >= start).order_by(Ferien.start))
    )
    holidays: HolidayMap = {}
    sources: dict[date, str] = {}
    for row in db.scalars(select(Holiday).where(Holiday.date.between(start, end))):
        holidays[row.date] = (row.name, row.scope)
        sources[row.date] = row.source
    overrides = {
        row.date: row for row in db.scalars(select(DayOverride).where(DayOverride.date.between(start, end)))
    }
    return CalendarInputs(start, end, ferien, holidays, sources, overrides)


def ferien_range(row: Ferien) -> FerienRange:
    return FerienRange(id=row.id, name=row.name, kind=FerienKind(row.kind), start=row.start, end=row.end)


def ferien_for_period(period: Period, rows: Iterable[Ferien]) -> list[FerienRange]:
    """Ferien intersecting the period; own rows win over overlapping rows of other periods."""
    candidates = [r for r in rows if r.start <= period.end and r.end >= period.start]
    candidates.sort(key=lambda r: (r.period_id != period.id, r.start, r.id))
    chosen: list[Ferien] = []
    for row in candidates:
        if any(row.start <= c.end and row.end >= c.start for c in chosen):
            continue
        chosen.append(row)
    return [ferien_range(r) for r in sorted(chosen, key=lambda r: (r.start, r.id))]


def _core_override(row: DayOverride) -> Override:
    return Override(
        date=row.date,
        day_type=DayType(row.day_type) if row.day_type else None,
        absence=AbsenceType(row.absence_type) if row.absence_type else None,
        urlaub_manuell=bool(row.urlaub_manuell),
        note=row.note,
    )


@dataclass(frozen=True)
class PeriodDays:
    """Core days of one period plus the leave plan they were built with."""

    period_id: str
    start: date
    end: date
    days: list[Day]
    plan: LeavePlan
    mode: LeaveMode
    ferien: list[FerienRange]
    holidays: HolidayMap
    manual: frozenset[date] = field(default_factory=frozenset)


def compute_days(
    db: Session,
    period: Period,
    leave_mode: LeaveMode | str | None = None,
    *,
    settings: Settings | None = None,
    inputs: CalendarInputs | None = None,
) -> PeriodDays:
    """Build the period's days in memory (no writes). ``inputs`` may cover a wider range."""
    settings = settings or get_settings(db)
    if inputs is None or inputs.start > period.start or inputs.end < period.end:
        inputs = load_inputs(db, period.start, period.end)
    mode = LeaveMode(leave_mode or settings.urlaubs_modus)
    ferien = ferien_for_period(period, inputs.ferien)
    holidays = {d: v for d, v in inputs.holidays.items() if period.start <= d <= period.end}
    overrides = {
        d: _core_override(o) for d, o in inputs.overrides.items() if period.start <= d <= period.end
    }
    manual = frozenset(d for d, o in overrides.items() if o.urlaub_manuell)
    base = build_days(period.start, period.end, ferien, holidays, overrides, frozenset())
    plan = plan_leave(base, ferien, mode, n=int(settings.urlaubstage), manual=manual)
    days = build_days(period.start, period.end, ferien, holidays, overrides, plan.days)
    return PeriodDays(period.id, period.start, period.end, days, plan, mode, ferien, holidays, manual)


# =========================================================================== materialisation


def sync_holidays(db: Session, period: Period, settings: Settings | None = None) -> None:
    """Store the computed Bavarian holidays (incl. regional ones per settings) for the period.

    Only rows with ``source='berechnet'`` are touched; manual/ICS rows on a date win.
    """
    settings = settings or get_settings(db)
    computed = holidays_between(period.start, period.end, settings.regionale_feiertage or ())
    existing = {
        row.date: row
        for row in db.scalars(select(Holiday).where(Holiday.date.between(period.start, period.end)))
    }
    for d, row in existing.items():
        if row.source != SOURCE_BERECHNET:
            continue
        if d not in computed:
            db.delete(row)
        elif (row.name, row.scope) != computed[d]:
            row.name, row.scope = computed[d]
    for d, (name, scope) in computed.items():
        if d not in existing:
            db.add(Holiday(date=d, name=name, scope=scope, source=SOURCE_BERECHNET))
    db.flush()


def rematerialize(db: Session, period: Period, *, commit: bool = True) -> PeriodDays:
    """Rebuild holidays and ``calendar_day`` rows of ``period`` (idempotent)."""
    settings = get_settings(db)
    sync_holidays(db, period, settings)
    pdays = compute_days(db, period, settings=settings)
    solls = day_solls(pdays.days, soll_params(settings))

    existing = {
        row.date: row
        for row in db.scalars(
            select(CalendarDay).where(
                (CalendarDay.date.between(period.start, period.end)) | (CalendarDay.period_id == period.id)
            )
        )
    }
    for day, soll in zip(pdays.days, solls, strict=True):
        values = {
            "weekday": day.weekday,
            "day_type": day.day_type.value,
            "absence_type": day.absence.value if day.absence else None,
            "ferien_id": day.ferien_id,
            "holiday_name": day.holiday_name,
            "soll_seconds": soll,
            "note": day.note,
            "period_id": period.id,
        }
        row = existing.pop(day.date, None)
        if row is None:
            db.add(CalendarDay(date=day.date, **values))
        else:
            for key, value in values.items():
                if getattr(row, key) != value:
                    setattr(row, key, value)
    for row in existing.values():  # rows of this period outside its range
        if row.period_id == period.id:
            db.delete(row)
    from app.services.entries import refresh_derived_flags  # local import: entries reads calendar rows

    refresh_derived_flags(db, period.start, period.end)
    if commit:
        db.commit()
    else:
        db.flush()
    return pdays


def rematerialize_all(db: Session) -> None:
    for period in list_periods(db):
        rematerialize(db, period, commit=False)
    db.commit()


def rematerialize_range(db: Session, start: date, end: date) -> None:
    """Rematerialise every period intersecting ``[start, end]``."""
    periods = db.scalars(select(Period).where(Period.start <= end, Period.end >= start)).all()
    for period in periods:
        rematerialize(db, period, commit=False)
    db.commit()


def list_calendar_days(db: Session, start: date, end: date) -> list[CalendarDay]:
    return list(
        db.scalars(select(CalendarDay).where(CalendarDay.date.between(start, end)).order_by(CalendarDay.date))
    )


def list_holidays(db: Session, period: Period) -> list[Holiday]:
    return list(
        db.scalars(
            select(Holiday).where(Holiday.date.between(period.start, period.end)).order_by(Holiday.date)
        )
    )


# =========================================================================== ferien


def list_ferien(db: Session, period: Period) -> list[Ferien]:
    return list(db.scalars(select(Ferien).where(Ferien.period_id == period.id).order_by(Ferien.start)))


def get_ferien(db: Session, ferien_id: str) -> Ferien:
    row = db.get(Ferien, ferien_id)
    if row is None:
        raise NotFoundError("Ferien nicht gefunden.")
    return row


def werktage(start: date, end: date, holidays: HolidayMap | None = None) -> int:
    """Mon–Fri days in ``[start, end]`` that are no statutory/regional holiday."""
    holidays = holidays or {}
    count = 0
    d = start
    while d <= end:
        if d.isoweekday() <= 5 and holidays.get(d, ("", ""))[1] not in HOLIDAY_SCOPES:
            count += 1
        d += timedelta(days=1)
    return count


def _check_ferien(
    db: Session, period: Period, name: str, start: date, end: date, exclude_id: str | None = None
) -> None:
    if not name or not name.strip():
        raise CalendarError("Bitte einen Namen für die Ferien angeben.")
    if len(name) > 100:
        raise CalendarError("Der Name ist zu lang (höchstens 100 Zeichen).")
    if end < start:
        raise CalendarError("Das Ende der Ferien liegt vor dem Beginn.")
    if (end - start).days + 1 > MAX_FERIEN_DAYS:
        raise CalendarError(f"Ein Ferienabschnitt darf höchstens {MAX_FERIEN_DAYS} Tage lang sein.")
    if end < period.start or start > period.end:
        raise CalendarError(
            f"Die Ferien liegen außerhalb von „{period.label}“ "
            f"({fmt_date_de(period.start)}–{fmt_date_de(period.end)})."
        )
    for other in list_ferien(db, period):
        if other.id != exclude_id and other.start <= end and other.end >= start:
            raise CalendarError(
                f"Überschneidung mit „{other.name}“ ({fmt_date_de(other.start)}–{fmt_date_de(other.end)})."
            )


def _kind(value: str | None, name: str) -> str:
    if value is None or value == "":
        return classify_ferien(name).value
    try:
        return FerienKind(value).value
    except ValueError:
        raise CalendarError(
            f"Unbekannte Ferienart „{value}“ (erlaubt: {', '.join(k.value for k in FerienKind)})."
        ) from None


def create_ferien(
    db: Session,
    period: Period,
    *,
    name: str,
    start: date,
    end: date,
    kind: str | None = None,
    source: str = SOURCE_MANUELL,
    quelle_url: str | None = None,
    rematerialize_after: bool = True,
    reason: str | None = None,
) -> Ferien:
    """Create a ferien block. ``reason`` is required when it changes a closed month (else 423)."""
    name = (name or "").strip()
    _check_ferien(db, period, name, start, end)
    before = closed_month_signature(db) if rematerialize_after else {}
    row = Ferien(
        period_id=period.id,
        name=name,
        kind=_kind(kind, name),
        start=start,
        end=end,
        source=source,
        quelle_url=(quelle_url or None),
    )
    db.add(row)
    db.flush()
    if rematerialize_after:
        guard_closed_months(db, before, reason)
        rematerialize_range(db, min(start, period.start), max(end, period.end))
    return row


def update_ferien(
    db: Session,
    ferien_id: str,
    *,
    name: str = _UNSET,
    start: date = _UNSET,
    end: date = _UNSET,
    kind: str | None = _UNSET,
    quelle_url: str | None = _UNSET,
    reason: str | None = None,
) -> Ferien:
    row = get_ferien(db, ferien_id)
    before = closed_month_signature(db)
    period = row.period
    old_start, old_end, old_name = row.start, row.end, row.name
    new_name = old_name if name is _UNSET else (name or "").strip()
    new_start = old_start if start is _UNSET or start is None else start
    new_end = old_end if end is _UNSET or end is None else end
    _check_ferien(db, period, new_name, new_start, new_end, exclude_id=row.id)
    if kind is not _UNSET:
        row.kind = _kind(kind, new_name)
    elif new_name != old_name and row.kind == classify_ferien(old_name).value:
        row.kind = classify_ferien(new_name).value  # follow the name unless set explicitly
    row.name, row.start, row.end = new_name, new_start, new_end
    if quelle_url is not _UNSET:
        row.quelle_url = quelle_url or None
    db.flush()
    guard_closed_months(db, before, reason)
    rematerialize_range(db, min(old_start, new_start, period.start), max(old_end, new_end, period.end))
    return row


def _drop_schaetzung(db: Session, ferien_ids: Iterable[str]) -> None:
    settings = get_settings(db)
    current = dict(settings.ferienarbeit_schaetzung or {})
    remaining = {k: v for k, v in current.items() if k not in set(ferien_ids)}
    if remaining != current:
        settings.ferienarbeit_schaetzung = remaining


def delete_ferien(db: Session, ferien_id: str, *, reason: str | None = None) -> None:
    row = get_ferien(db, ferien_id)
    period = row.period
    start, end = min(row.start, period.start), max(row.end, period.end)
    before = closed_month_signature(db)
    db.delete(row)
    _drop_schaetzung(db, [ferien_id])
    db.flush()
    guard_closed_months(db, before, reason)
    rematerialize_range(db, start, end)


# =========================================================================== ICS import


@dataclass(frozen=True)
class IcsEvent:
    uid: str | None
    name: str
    start: date
    end: date  # inclusive


@dataclass(frozen=True)
class IcsPreviewItem:
    index: int
    name: str
    kind: str
    start: date
    end: date
    tage: int
    werktage: int
    status: str  # neu | vorhanden | ueberschneidung | ausserhalb | feiertag | doppelt
    hinweis: str
    importierbar: bool
    vorausgewaehlt: bool
    konflikt_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class IcsPreview:
    period_id: str
    items: list[IcsPreviewItem]
    warnings: list[str]

    @property
    def importable_count(self) -> int:
        return sum(1 for i in self.items if i.vorausgewaehlt)


@dataclass(frozen=True)
class IcsCommitResult:
    created: list[Ferien]
    replaced: list[str]
    skipped: list[tuple[int, str]]  # (index, reason)
    warnings: list[str]


def _to_date(value: date | datetime, *, is_end: bool) -> date:
    if isinstance(value, datetime):
        local = value.astimezone(LOCAL_TZ) if value.tzinfo else value  # floating time = local
        if is_end and local.time() == datetime.min.time():
            return local.date() - timedelta(days=1)  # exclusive midnight end
        return local.date()
    return value - timedelta(days=1) if is_end else value


def parse_ics(content: bytes | str) -> tuple[list[IcsEvent], list[str]]:
    """Parse VEVENTs into inclusive date ranges (DTEND is exclusive for all-day events)."""
    try:
        from icalendar import Calendar
    except ImportError as exc:  # pragma: no cover - dependency is installed
        raise CalendarError("ICS-Import nicht verfügbar (icalendar fehlt).") from exc

    raw = content.encode("utf-8") if isinstance(content, str) else content
    if len(raw) > MAX_ICS_BYTES:
        raise CalendarError("Die ICS-Datei ist zu groß (höchstens 1 MB).")
    if not raw.strip():
        raise CalendarError("Die ICS-Datei ist leer.")
    try:
        cal = Calendar.from_ical(raw)
    except Exception as exc:  # icalendar raises ValueError and friends
        raise CalendarError(f"Die ICS-Datei konnte nicht gelesen werden ({exc}).") from None

    events: list[IcsEvent] = []
    warnings: list[str] = []
    for vevent in cal.walk("VEVENT"):
        name = str(vevent.get("summary") or "").strip() or "Ferien"
        dtstart = vevent.get("dtstart")
        if dtstart is None:
            warnings.append(f"„{name}“ ohne Beginn wurde übersprungen.")
            continue
        if vevent.get("rrule") is not None:
            warnings.append(f"„{name}“ ist ein Serientermin und wurde übersprungen.")
            continue
        start_value = dtstart.dt
        start = _to_date(start_value, is_end=False)
        dtend = vevent.get("dtend")
        duration = vevent.get("duration")
        if dtend is not None:
            end = _to_date(dtend.dt, is_end=True)
        elif duration is not None:
            end = _to_date(start_value + duration.dt, is_end=True)
        else:
            end = start  # single day (RFC 5545: all-day event without DTEND lasts one day)
        if end < start:
            end = start
        uid = vevent.get("uid")
        events.append(IcsEvent(uid=str(uid) if uid else None, name=name, start=start, end=end))
    if not events:
        warnings.append("Die Datei enthält keine Termine (VEVENT).")
    events.sort(key=lambda e: (e.start, e.end, e.name))
    return events, warnings


def preview_ics(
    db: Session, period: Period, content: bytes | str, *, replace: bool = False
) -> IcsPreview:
    """Classify each ICS event for import into ``period`` (nothing is written)."""
    events, warnings = parse_ics(content)
    settings = get_settings(db)
    own = list_ferien(db, period)
    lo = min([period.start, *(e.start for e in events)])
    hi = max([period.end, *(e.end for e in events)])
    nearby = list(db.scalars(select(Ferien).where(Ferien.start <= hi, Ferien.end >= lo)))
    holidays = holidays_between(lo, hi, settings.regionale_feiertage or ())

    items: list[IcsPreviewItem] = []
    accepted: list[IcsEvent] = []
    for index, ev in enumerate(events):
        kind = classify_ferien(ev.name).value
        tage = (ev.end - ev.start).days + 1
        wt = werktage(ev.start, ev.end, holidays)
        conflicts: tuple[str, ...] = ()
        if ev.end < period.start or ev.start > period.end:
            status, hint = "ausserhalb", f"Liegt außerhalb von „{period.label}“."
        elif tage > MAX_FERIEN_DAYS:
            status, hint = "ausserhalb", f"Länger als {MAX_FERIEN_DAYS} Tage – kein Ferienabschnitt."
        elif any(f.start == ev.start and f.end == ev.end for f in nearby):
            status, hint = "vorhanden", "Bereits angelegt."
        elif any(a.start <= ev.end and a.end >= ev.start for a in accepted):
            status, hint = "doppelt", "Überschneidet sich mit einem anderen Termin dieser Datei."
        elif conflicts := tuple(f.id for f in own if f.start <= ev.end and f.end >= ev.start):
            names = ", ".join(f.name for f in own if f.id in conflicts)
            status = "ueberschneidung"
            hint = f"Überschneidet sich mit „{names}“" + (" (wird ersetzt)." if replace else ".")
        elif wt == 0 or all(
            d.isoweekday() >= 6 or d in holidays
            for d in (ev.start + timedelta(days=i) for i in range(tage))
        ):
            status = "feiertag"
            hint = "Nur Feiertage/unterrichtsfreie Tage – werden automatisch berechnet."
        else:
            status, hint = "neu", ""
        importable = status in ("neu", "feiertag") or (status == "ueberschneidung" and replace)
        preselected = status == "neu" or (status == "ueberschneidung" and replace)
        if preselected:
            accepted.append(ev)
        items.append(
            IcsPreviewItem(
                index=index,
                name=ev.name,
                kind=kind,
                start=ev.start,
                end=ev.end,
                tage=tage,
                werktage=wt,
                status=status,
                hinweis=hint,
                importierbar=importable,
                vorausgewaehlt=preselected,
                konflikt_ids=conflicts,
            )
        )
    return IcsPreview(period_id=period.id, items=items, warnings=warnings)


def commit_ics(
    db: Session,
    period: Period,
    content: bytes | str,
    *,
    selected: Iterable[int] | None = None,
    replace: bool = False,
    quelle_url: str | None = None,
    reason: str | None = None,
) -> IcsCommitResult:
    """Import the selected events (default: all preselected) as ferien with ``source='ics'``.

    ``reason`` is required when the import changes a closed month (else 423, nothing imported).
    """
    preview = preview_ics(db, period, content, replace=replace)
    before = closed_month_signature(db)
    wanted = {i.index for i in preview.items if i.vorausgewaehlt} if selected is None else set(selected)
    by_index = {i.index: i for i in preview.items}
    unknown = sorted(i for i in wanted if i not in by_index)
    if unknown:
        raise CalendarError(f"Unbekannte Auswahl: {', '.join(map(str, unknown))}.")

    created: list[Ferien] = []
    replaced: list[str] = []
    skipped: list[tuple[int, str]] = []
    for item in preview.items:
        if item.index not in wanted:
            continue
        if not item.importierbar:
            skipped.append((item.index, item.hinweis or item.status))
            continue
        for conflict_id in item.konflikt_ids:
            row = db.get(Ferien, conflict_id)
            if row is not None:
                replaced.append(row.name)
                db.delete(row)
                _drop_schaetzung(db, [conflict_id])
        db.flush()
        start, end = item.start, item.end
        try:
            row = create_ferien(
                db,
                period,
                name=item.name,
                start=start,
                end=end,
                kind=item.kind,
                source=SOURCE_ICS,
                quelle_url=quelle_url,
                rematerialize_after=False,
            )
        except CalendarError as exc:  # e.g. overlap between two explicitly selected events
            skipped.append((item.index, str(exc)))
            continue
        created.append(row)
    db.flush()
    guard_closed_months(db, before, reason)
    rematerialize_all(db)
    return IcsCommitResult(created=created, replaced=replaced, skipped=skipped, warnings=preview.warnings)


# =========================================================================== overrides


def list_overrides(db: Session, start: date | None = None, end: date | None = None) -> list[DayOverride]:
    stmt = select(DayOverride).order_by(DayOverride.date)
    if start is not None:
        stmt = stmt.where(DayOverride.date >= start)
    if end is not None:
        stmt = stmt.where(DayOverride.date <= end)
    return list(db.scalars(stmt))


def get_override(db: Session, d: date) -> DayOverride | None:
    return db.get(DayOverride, d)


def _normalise_override(
    absence_type: str | None, day_type: str | None, urlaub_manuell: bool, note: str | None
) -> tuple[str | None, str | None, bool, str | None]:
    if day_type:
        try:
            dt = DayType(day_type)
        except ValueError:
            dt = None
        if dt not in OVERRIDE_DAY_TYPES:
            raise CalendarError(
                f"Tagestyp „{day_type}“ kann nicht gesetzt werden "
                "(erlaubt: schultag, ferientag, unterrichtsfrei)."
            )
        day_type = dt.value
    else:
        day_type = None
    if absence_type:
        if absence_type == AbsenceType.URLAUB.value:
            raise CalendarError("Urlaub bitte über die Urlaubszuordnung (Urlaub manuell) setzen.")
        try:
            absence_type = AbsenceType(absence_type).value
        except ValueError:
            raise CalendarError(
                f"Unbekannte Abwesenheit „{absence_type}“ "
                f"(erlaubt: {', '.join(a.value for a in sorted(OVERRIDE_ABSENCES))})."
            ) from None
    else:
        absence_type = None
    if urlaub_manuell and absence_type:
        raise CalendarError(f"Ein Tag kann nicht gleichzeitig Urlaub und „{absence_type}“ sein.")
    note = (note or "").strip() or None
    if note and len(note) > 500:
        raise CalendarError("Die Notiz ist zu lang (höchstens 500 Zeichen).")
    return absence_type, day_type, bool(urlaub_manuell), note


def _check_manual_leave(db: Session, period: Period, d: date, override: Override) -> None:
    """``urlaub_manuell`` only on leave candidates, and at most ``urlaubstage`` per period."""
    settings = get_settings(db)
    inputs = load_inputs(db, d, d)
    ferien = ferien_for_period(period, db.scalars(select(Ferien).where(Ferien.start <= d, Ferien.end >= d)))
    day = build_days(d, d, ferien, inputs.holidays, {d: override}, frozenset())[0]
    if not is_leave_candidate(day):
        raise CalendarError(
            f"{fmt_date_de(d)} kann kein Urlaubstag sein – nur Ferien- oder unterrichtsfreie Werktage."
        )
    taken = db.scalars(
        select(DayOverride.date).where(
            DayOverride.urlaub_manuell.is_(True),
            DayOverride.date.between(period.start, period.end),
            DayOverride.date != d,
        )
    ).all()
    if len(taken) >= int(settings.urlaubstage):
        raise CalendarError(f"Alle {settings.urlaubstage} Urlaubstage sind bereits zugeordnet.")


def _apply_override(
    db: Session,
    d: date,
    absence_type: str | None,
    day_type: str | None,
    urlaub_manuell: bool,
    note: str | None,
) -> DayOverride | None:
    row = db.get(DayOverride, d)
    if not (absence_type or day_type or urlaub_manuell or note):
        if row is not None:
            db.delete(row)
        return None
    if row is None:
        row = DayOverride(date=d)
        db.add(row)
    row.absence_type, row.day_type, row.urlaub_manuell, row.note = absence_type, day_type, urlaub_manuell, note
    return row


def set_override(
    db: Session,
    d: date,
    *,
    absence_type: str | None = None,
    day_type: str | None = None,
    urlaub_manuell: bool = False,
    note: str | None = None,
    reason: str | None = None,
) -> DayOverride | None:
    """Replace the override of day ``d`` (all fields empty → override removed); rematerialises.

    ``reason`` is required when the change alters a closed month (else :class:`CalendarLocked`).
    """
    period = period_for_date(db, d)
    if period is None:
        raise CalendarError(f"Der {fmt_date_de(d)} liegt in keinem angelegten Schuljahr. {NO_PERIOD_HINT}")
    absence_type, day_type, urlaub_manuell, note = _normalise_override(
        absence_type, day_type, urlaub_manuell, note
    )
    if urlaub_manuell:
        candidate = Override(
            date=d, day_type=DayType(day_type) if day_type else None, absence=None, urlaub_manuell=True
        )
        _check_manual_leave(db, period, d, candidate)
    before = closed_month_signature(db)
    row = _apply_override(db, d, absence_type, day_type, urlaub_manuell, note)
    db.flush()
    guard_closed_months(db, before, reason)
    rematerialize(db, period)
    return row


def set_override_range(
    db: Session,
    start: date,
    end: date,
    *,
    absence_type: str | None = None,
    day_type: str | None = None,
    note: str | None = None,
    weekdays_only: bool = True,
    reason: str | None = None,
) -> list[DayOverride]:
    """Apply the same absence/day type to every (working) day in ``[start, end]``.

    Manual leave is not available here (it is chosen day by day). Empty values remove the
    overrides of the range.
    """
    if end < start:
        raise CalendarError("Das Ende liegt vor dem Beginn.")
    if (end - start).days + 1 > MAX_OVERRIDE_RANGE_DAYS:
        raise CalendarError("Der Zeitraum ist zu lang (höchstens ein Jahr).")
    absence_type, day_type, _, note = _normalise_override(absence_type, day_type, False, note)
    periods = db.scalars(select(Period).where(Period.start <= end, Period.end >= start)).all()
    before = closed_month_signature(db)
    rows: list[DayOverride] = []
    d = start
    while d <= end:
        in_period = any(p.start <= d <= p.end for p in periods)
        if not in_period:
            raise CalendarError(f"Der {fmt_date_de(d)} liegt in keinem angelegten Schuljahr. {NO_PERIOD_HINT}")
        if not weekdays_only or d.isoweekday() <= 5:
            row = _apply_override(db, d, absence_type, day_type, False, note)
            if row is not None:
                rows.append(row)
        d += timedelta(days=1)
    db.flush()
    guard_closed_months(db, before, reason)
    rematerialize_range(db, start, end)
    return rows


def delete_override(db: Session, d: date, *, reason: str | None = None) -> None:
    row = db.get(DayOverride, d)
    if row is None:
        raise NotFoundError("Für diesen Tag gibt es keine Eingabe.")
    before = closed_month_signature(db)
    db.delete(row)
    db.flush()
    guard_closed_months(db, before, reason)
    period = period_for_date(db, d)
    if period is not None:
        rematerialize(db, period)
    else:
        db.commit()


# =========================================================================== leave


@dataclass(frozen=True)
class FerienLeave:
    ferien_id: str
    name: str
    kind: str
    start: date
    end: date
    kandidaten: int
    urlaubstage: int


@dataclass(frozen=True)
class LeavePlanInfo:
    mode: str
    mode_label: str
    requested: int
    assigned: int
    candidates: int
    shortfall: int
    noch_zuzuordnen: int
    excess: int
    invalid: list[date]
    is_complete: bool
    days: list[date]
    per_ferien: list[FerienLeave]
    ausserhalb_ferien: int
    hinweise: list[str]  # warnings (real problems)
    info: list[str] = field(default_factory=list)  # neutral status, e.g. manual mode "Noch zuzuordnen: n"


def leave_plan_info(db: Session, period: Period, mode: LeaveMode | str | None = None) -> LeavePlanInfo:
    pdays = compute_days(db, period, mode)
    return leave_plan_info_from(pdays)


def leave_shortfall(plan: LeavePlan, mode: LeaveMode) -> int:
    """Requested leave days that *cannot* be placed.

    In the automatic modes that is the core's ``shortfall``. In manual mode the core counts every day not yet
    picked as shortfall; only a lack of candidate days is a real problem there (the rest is
    ``noch_zuzuordnen``, an open to-do).
    """
    if mode is LeaveMode.MANUELL:
        return max(0, plan.requested - plan.candidates)
    return plan.shortfall


def leave_plan_info_from(pdays: PeriodDays) -> LeavePlanInfo:
    """Leave plan figures plus German hints: ``hinweise`` are warnings, ``info`` neutral status lines."""
    plan = pdays.plan
    manual = pdays.mode is LeaveMode.MANUELL
    shortfall = leave_shortfall(plan, pdays.mode)
    base_candidates = {d.date for d in pdays.days if is_leave_candidate(d)}
    per_ferien = []
    in_ferien: set[date] = set()
    for f in pdays.ferien:
        cands = [d for d in base_candidates if f.contains(d)]
        taken = [d for d in plan.days if f.contains(d)]
        in_ferien.update(taken)
        per_ferien.append(
            FerienLeave(f.id, f.name, f.kind.value, f.start, f.end, len(cands), len(taken))
        )
    hints: list[str] = []
    info: list[str] = []
    if shortfall:
        hints.append(
            f"Es gibt nur {plan.candidates} mögliche Urlaubstage – {shortfall} Tag(e) "
            + ("können" if manual else "konnten")
            + " nicht zugeordnet werden. Sind alle Ferien erfasst?"
        )
    if plan.excess:
        hints.append(
            f"{plan.excess} Urlaubstag(e) zu viel ausgewählt."
            + (" Bitte im Kalender wieder abwählen." if manual else "")
        )
    if plan.invalid:
        hints.append(
            "Diese Tage können kein Urlaub sein: "
            + ", ".join(fmt_date_de(d) for d in sorted(plan.invalid))
            + "."
        )
    if manual and plan.noch_zuzuordnen > 0:
        info.append(f"Noch zuzuordnen: {plan.noch_zuzuordnen} Urlaubstag(e).")
    return LeavePlanInfo(
        mode=pdays.mode.value,
        mode_label=LEAVE_MODE_LABELS[pdays.mode],
        requested=plan.requested,
        assigned=len(plan.days),
        candidates=plan.candidates,
        shortfall=shortfall,
        noch_zuzuordnen=plan.noch_zuzuordnen,
        excess=plan.excess,
        invalid=sorted(plan.invalid),
        is_complete=plan.is_complete,
        days=sorted(plan.days),
        per_ferien=per_ferien,
        ausserhalb_ferien=len(plan.days - in_ferien),
        hinweise=hints,
        info=info,
    )


def adopt_leave_plan(
    db: Session, period: Period, mode: LeaveMode | str, *, reason: str | None = None
) -> LeavePlanInfo:
    """Copy an automatic plan into ``urlaub_manuell`` flags of the period (basis for manual edits).

    The global ``urlaubs_modus`` is not changed; the flags take effect in mode ``manuell``.
    """
    mode = LeaveMode(mode)
    if mode is LeaveMode.MANUELL:
        raise CalendarError("Bitte einen automatischen Modus als Vorlage wählen.")
    plan = compute_days(db, period, mode).plan
    before = closed_month_signature(db)
    for row in db.scalars(
        select(DayOverride).where(
            DayOverride.urlaub_manuell.is_(True), DayOverride.date.between(period.start, period.end)
        )
    ):
        row.urlaub_manuell = False
        if not (row.absence_type or row.day_type or row.note):
            db.delete(row)
    db.flush()
    for d in sorted(plan.days):
        row = db.get(DayOverride, d)
        if row is None:
            db.add(DayOverride(date=d, urlaub_manuell=True))
        else:
            row.urlaub_manuell = True
    db.flush()
    guard_closed_months(db, before, reason)
    rematerialize(db, period)
    return leave_plan_info(db, period, LeaveMode.MANUELL)


def compare_leave_modes(db: Session, period: Period, today: date | None = None) -> list[dict[str, Any]]:
    """Saldo today / at the end for 'sommerferien_zuerst' and 'proportional' (Konzept §6.2).

    Delegates to :func:`app.services.ledger.compare_leave_modes` (the ledger owns DB → core).
    """
    from app.services.ledger import compare_leave_modes as _compare

    return _compare(db, period, today=today)
