"""The account ledger: the ONLY place where database data flows into the calculation core.

:func:`load_context` loads settings, the period's calendar input and the time entries (one query
per table) and returns a :class:`LedgerContext` with the ledger rows and every key figure the
screens need (Konto, Woche, Schuljahr, Heute, sensors, reports).

Ist (Konzept §6.3): sum of ``time_entry.duration_seconds`` per ``time_entry.day`` over *active*
entries (``status='active'``, ``deleted_at IS NULL``); a running timer counts with its elapsed
time. Entries of pseudo categories (``is_pseudo``, Krank 'KR') never count as work: they feed
``krank_partial`` (neutralising only the soll) and are excluded from statistics. The Klassenfahrt
variant is applied via :func:`app.core.klassenfahrt.evaluate_ist`.

Calendar days are computed in memory from the same source tables :func:`rematerialize` uses
(``calendar_service.compute_days``), so a different leave mode can be evaluated without writes
and a stale ``calendar_day`` table can never distort the figures.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import account as core_account
from app.core.account import EuMittel, HolidayForecast, MonthSummary, WeekSummary
from app.core.calendar import build_days, holidays_between
from app.core.klassenfahrt import evaluate_ist
from app.core.leave import LeavePlan
from app.core.mehrarbeit import MehrarbeitMonth, mehrarbeit_by_month
from app.core.timesplit import local_day, split_at_midnight
from app.core.types import (
    FLAG_NACHTBEREITSCHAFT,
    AbsenceType,
    Day,
    DayLedger,
    DayType,
    FerienRange,
    KlassenfahrtVariante,
    LeaveMode,
    SollParams,
)
from app.db.models import CalendarDay, Category, DayOverride, EntryStatus, Ferien, Holiday, Period, TimeEntry
from app.services import calendar_service as cal
from app.services.settings_service import get_settings, soll_params
from app.util import now_utc, to_local, today_local

HISTORY_PERIODS = 3  # previous periods used for the Ferienarbeit default (mean of past blocks)
EU_WINDOW_DAYS = core_account.EU_WINDOW_DAYS


@dataclass(frozen=True)
class EntryRow:
    """The columns of an active time entry the ledger needs."""

    day: date
    seconds: int
    flags: tuple[str, ...]
    category_id: int  # main category (subcategories resolved to their parent)


@dataclass(frozen=True)
class CategoryShare:
    category_id: int
    code: str
    name: str
    color: str
    seconds: int
    share: float  # 0..1 of all recorded (non-pseudo) time in the period
    is_unterricht: bool


@dataclass
class LedgerContext:
    period: Period
    today: date
    params: SollParams
    leave_mode: LeaveMode
    klassenfahrt_variante: KlassenfahrtVariante
    days: list[Day]
    ledger: list[DayLedger]
    plan: LeavePlan
    ferien: list[FerienRange]
    holidays: dict[date, tuple[str, str]]
    ist_by_day: dict[date, int]  # recorded, Klassenfahrt-evaluated, without Krank credit
    krank_partial: dict[date, int]
    saldo_heute: int
    saldo_tage: float
    j_soll: int
    soll_bisher: int
    ist_bisher: int
    ferienarbeit_bisher: int
    ferienarbeit_geplant: int  # expected work in the remaining ferien (sum of the forecast)
    prognose_ende: int
    w_max: int
    eu: EuMittel
    schulwochen_ueber_48: int
    holiday_forecast: list[HolidayForecast]
    series: list[tuple[date, int]]
    weeks: list[WeekSummary]
    months: list[MonthSummary]
    categories: list[CategoryShare]
    unterricht_seconds: int
    unterricht_share: float
    mehrarbeit: dict[tuple[int, int], MehrarbeitMonth]
    planned_work: dict[str, int] = field(default_factory=dict)  # ferien_id -> seconds (user estimate)
    # First day of the EU window that is covered by data (period or entry); None = whole window covered.
    # Before that day the window is neutralised (divisor shrinks), see load_context.
    eu_daten_ab: date | None = None
    erfassung_ab: date | None = None  # settings.erfassung_ab: earlier days are neutral (ist = soll)
    uebertrag: int | None = None  # closing saldo of the previous period (None: no previous period)
    uebertrag_label: str | None = None
    uebertrag_verrechnet: bool = False
    _rows: dict[date, DayLedger] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._rows = {r.date: r for r in self.ledger}

    def row(self, d: date) -> DayLedger | None:
        return self._rows.get(d)

    @property
    def as_of(self) -> date:
        """``today`` clamped into the period."""
        return min(max(self.today, self.period.start), self.period.end)

    @property
    def series_bis_heute(self) -> list[tuple[date, int]]:
        return [(d, s) for d, s in self.series if d <= self.today]

    @property
    def saldo_offset(self) -> int:
        """Übertrag added to every saldo figure (0 unless ``uebertrag_verrechnen``)."""
        return int(self.uebertrag or 0) if self.uebertrag_verrechnet else 0

    def saldo_at(self, d: date) -> int:
        """Cumulative saldo after day ``d`` incl. a booked Übertrag."""
        return core_account.saldo_at(self.ledger, d) + self.saldo_offset

    def week_of(self, d: date) -> WeekSummary | None:
        iso = d.isocalendar()
        return next((w for w in self.weeks if (w.iso_year, w.iso_week) == (iso[0], iso[1])), None)

    def ferien_by_id(self, ferien_id: str) -> FerienRange | None:
        return next((f for f in self.ferien if f.id == ferien_id), None)


# =========================================================================== loading


def _categories(db: Session) -> dict[int, Category]:
    return {c.id: c for c in db.scalars(select(Category))}


def _main_category_id(cats: dict[int, Category], category_id: int) -> int:
    cat = cats.get(category_id)
    seen = 0
    while cat is not None and cat.parent_id is not None and seen < 5:
        category_id = cat.parent_id
        cat = cats.get(category_id)
        seen += 1
    return category_id


def load_entries(db: Session, start: date, end: date, cats: dict[int, Category]) -> list[EntryRow]:
    """Active, non-deleted entries with ``start <= day <= end`` (single query)."""
    stmt = select(
        TimeEntry.day,
        TimeEntry.duration_seconds,
        TimeEntry.flags,
        TimeEntry.category_id,
        TimeEntry.start_at,
        TimeEntry.end_at,
    ).where(
        TimeEntry.day.between(start, end),
        TimeEntry.status == EntryStatus.ACTIVE.value,
        TimeEntry.deleted_at.is_(None),
    )
    now = None
    rows: list[EntryRow] = []
    for day, seconds, flags, category_id, start_at, end_at in db.execute(stmt):
        main_id = _main_category_id(cats, category_id)
        if end_at is None:  # running timer: elapsed time so far, booked per local day
            now = now or now_utc()
            for part_day, part_seconds in running_parts(start_at, now):
                if start <= part_day <= end:
                    rows.append(EntryRow(part_day, part_seconds, tuple(flags or ()), main_id))
            continue
        rows.append(
            EntryRow(
                day=day,
                seconds=int(seconds or 0),
                flags=tuple(flags or ()),
                category_id=main_id,
            )
        )
    return rows


def running_parts(start_at: datetime, now: datetime) -> list[tuple[date, int]]:
    """Elapsed time of a running timer split at local midnight: ``[(day, seconds), ...]``.

    Uses the same split as the stop (``core.timesplit.split_at_midnight``), so the live figures of
    "today"/the week match what the stop will store.
    """
    if now <= start_at:
        return [(local_day(start_at), 0)]
    return [(local_day(a), int((b - a).total_seconds())) for a, b in split_at_midnight(start_at, now)]


def evaluated_rows(
    entries: list[EntryRow],
    cats: dict[int, Category],
    ist_by_day: dict[date, int],
    variante: KlassenfahrtVariante | str,
) -> list[EntryRow]:
    """Work entries (pseudo categories dropped) scaled so that they add up to the evaluated Ist per day.

    The Klassenfahrt variant B drops ``nachtbereitschaft`` entries and caps trip days
    (``core.klassenfahrt.evaluate_ist``); category statistics must use the same basis as the Ist, otherwise
    a report shows a category total above its own Ist. Each entry of a capped day is scaled
    proportionally (largest remainder, so the day sum matches ``ist_by_day`` to the second).
    """
    variante = KlassenfahrtVariante(variante)
    by_day: dict[date, list[EntryRow]] = defaultdict(list)
    for e in entries:
        cat = cats.get(e.category_id)
        if cat is None or cat.is_pseudo:
            continue
        if variante is KlassenfahrtVariante.B and FLAG_NACHTBEREITSCHAFT in e.flags:
            continue
        by_day[e.day].append(e)
    result: list[EntryRow] = []
    for day, rows in by_day.items():
        raw = sum(r.seconds for r in rows)
        target = ist_by_day.get(day, raw)
        if raw <= 0 or target >= raw:
            result.extend(rows)
            continue
        shares = [r.seconds * target / raw for r in rows]
        floors = [int(s) for s in shares]
        rest = target - sum(floors)
        order = sorted(range(len(rows)), key=lambda i: shares[i] - floors[i], reverse=True)
        for i in order[:rest]:
            floors[i] += 1
        result.extend(replace(r, seconds=s) for r, s in zip(rows, floors, strict=True))
    return result


def _data_start(db: Session) -> date | None:
    """First day with account data: the earliest period start or the earliest active entry."""
    first_period = db.scalar(select(func.min(Period.start)))
    first_entry = db.scalar(
        select(func.min(TimeEntry.day)).where(
            TimeEntry.status == EntryStatus.ACTIVE.value, TimeEntry.deleted_at.is_(None)
        )
    )
    candidates = [d for d in (first_period, first_entry) if d is not None]
    return min(candidates) if candidates else None


def split_ist(
    entries: list[EntryRow],
    cats: dict[int, Category],
    variante: KlassenfahrtVariante | str,
    cap_h: float,
) -> tuple[dict[date, int], dict[date, int]]:
    """(ist_by_day via the Klassenfahrt variant, krank_partial from pseudo entries)."""
    work: list[tuple[date, int, tuple[str, ...]]] = []
    krank: dict[date, int] = defaultdict(int)
    for e in entries:
        cat = cats.get(e.category_id)
        if cat is not None and cat.is_pseudo:
            krank[e.day] += e.seconds
        else:
            work.append((e.day, e.seconds, e.flags))
    return evaluate_ist(work, variante, cap_h), dict(krank)


def _history_periods(db: Session, period: Period) -> list[Period]:
    earlier = db.scalars(
        select(Period).where(Period.end < period.start).order_by(Period.start.desc()).limit(HISTORY_PERIODS)
    ).all()
    return sorted(earlier, key=lambda p: p.start)


def _eu_holidays(settings: Any, start: date, end: date) -> set[date]:
    return {
        d
        for d, (_name, scope) in holidays_between(start, end, settings.regionale_feiertage or ()).items()
        if scope in cal.HOLIDAY_SCOPES
    }


def _planned_seconds(settings: Any) -> dict[str, int]:
    return {
        str(k): int(round(float(v) * 3600))
        for k, v in (settings.ferienarbeit_schaetzung or {}).items()
        if v is not None
    }


def load_context(
    db: Session,
    period: Period | str | None = None,
    today: date | None = None,
    leave_mode: LeaveMode | str | None = None,
    klassenfahrt_variante: KlassenfahrtVariante | str | None = None,
) -> LedgerContext:
    """Compute the full account context for ``period`` (default: current) as of ``today``.

    Raises :class:`app.services.calendar_service.NoPeriodError` if no period exists.
    """
    today = today or today_local()
    if not isinstance(period, Period):
        period = cal.resolve_period(db, period, today)
    settings = get_settings(db)
    params = soll_params(settings)
    mode = LeaveMode(leave_mode or settings.urlaubs_modus)
    variante = KlassenfahrtVariante(klassenfahrt_variante or settings.klassenfahrt_variante or "B")
    n_schultage = int(settings.prognose_schultage or core_account.DEFAULT_PROGNOSE_SCHULTAGE)

    history = _history_periods(db, period)
    eu_start = today - timedelta(days=EU_WINDOW_DAYS - 1)
    lo = min([period.start, eu_start, *(p.start for p in history)])
    hi = max(period.end, today)

    cats = _categories(db)
    inputs = cal.load_inputs(db, lo, hi)
    entries = load_entries(db, lo, hi, cats)
    ist_all, krank_all = split_ist(entries, cats, variante, float(settings.klassenfahrt_cap_h or 10.0))

    pdays = cal.compute_days(db, period, mode, settings=settings, inputs=inputs)
    erfassung_ab = settings.erfassung_ab
    ledger = core_account.build_ledger(
        pdays.days, params, ist_all, bool(settings.krank_neutralisiert), krank_all, erfassung_ab
    )

    # History (previous periods) for the Ferienarbeit default and the EU window's absences.
    absences: dict[date, AbsenceType | None] = {}
    hist_ledger: list[DayLedger] = []
    hist_ferien: dict[str, FerienRange] = {}
    verrechnen = bool(settings.uebertrag_verrechnen)
    carry: int | None = None  # closing saldo of the latest previous period with data (Konzept §1.3)
    carry_label: str | None = None
    for prev in history:
        prev_days = cal.compute_days(db, prev, settings=settings, inputs=inputs)
        prev_ledger = core_account.build_ledger(
            prev_days.days, params, ist_all, bool(settings.krank_neutralisiert), krank_all, erfassung_ab
        )
        hist_ledger.extend(prev_ledger)
        if any(r.ist_erfasst > 0 or r.krank_gutschrift > 0 for r in prev_ledger):
            end_saldo = sum(r.delta for r in prev_ledger)
            if verrechnen and carry is not None:
                end_saldo += carry
            carry, carry_label = end_saldo, prev.label
        else:
            carry, carry_label = None, None
        hist_ferien.update({f.id: f for f in prev_days.ferien})
        absences.update({d.date: d.absence for d in prev_days.days if d.absence})
    absences.update({d.date: d.absence for d in pdays.days if d.absence})
    for f in pdays.ferien:
        hist_ferien.setdefault(f.id, f)
    combined = {r.date: r for r in hist_ledger}
    combined.update({r.date: r for r in ledger})
    work_history = core_account.ferien_work_history(combined.values(), hist_ferien.values(), today)

    planned = _planned_seconds(settings)
    forecast = core_account.holiday_forecast(
        ledger, today, pdays.ferien, planned, n_schultage, history=work_history
    )
    geplant = sum(f.erwartete_ferienarbeit for f in forecast)

    saldo = core_account.saldo_at(ledger, today)
    # EU window (Konzept §6.5): days before the first recorded data (first period / first entry) are
    # unknown, not "0 h worked". They are neutralised like absences (every working day of that stretch
    # shrinks the divisor by 1/5 week), so the mean refers to the covered weeks only.
    eu_absences = dict(absences)
    data_start = _data_start(db)
    if erfassung_ab is not None and (data_start is None or erfassung_ab > data_start):
        data_start = erfassung_ab
    eu_daten_ab = data_start if data_start is not None and data_start > eu_start else None
    eu_holiday_from = eu_start
    if eu_daten_ab is not None:
        d = eu_start
        while d < min(eu_daten_ab, today + timedelta(days=1)):
            eu_absences[d] = AbsenceType.URLAUB
            d += timedelta(days=1)
        eu_holiday_from = eu_daten_ab  # uncovered holidays count as neutralised days as well
    eu = core_account.eu_mittel(
        {d: s for d, s in ist_all.items() if d <= today},
        eu_absences,
        today,
        _eu_holidays(settings, eu_holiday_from, today) if eu_holiday_from <= today else set(),
    )

    # Statistics over the period (pseudo categories excluded), on the same basis as the Ist (Klassenfahrt
    # variant applied, see evaluated_rows).
    in_period = [e for e in entries if period.start <= e.day <= period.end]
    per_cat: dict[int, int] = defaultdict(int)
    teaching: list[tuple[date, int, tuple[str, ...]]] = []
    for e in in_period:
        cat = cats.get(e.category_id)
        if cat is not None and not cat.is_pseudo and cat.is_unterricht:
            teaching.append((e.day, e.seconds, e.flags))  # Mehrarbeit counts recorded lessons
    for e in evaluated_rows(in_period, cats, ist_all, variante):
        per_cat[e.category_id] += e.seconds
    total = sum(per_cat.values())
    categories = sorted(
        (
            CategoryShare(
                category_id=cid,
                code=cats[cid].code,
                name=cats[cid].name,
                color=cats[cid].color,
                seconds=secs,
                share=(secs / total) if total else 0.0,
                is_unterricht=bool(cats[cid].is_unterricht),
            )
            for cid, secs in per_cat.items()
        ),
        key=lambda c: (cats[c.category_id].sort, c.code),
    )
    unterricht = sum(c.seconds for c in categories if c.is_unterricht)

    # Übertrag (Konzept §1.3, §18): always shown; added to the saldo figures only with uebertrag_verrechnen.
    offset = carry if (verrechnen and carry) else 0
    series = core_account.saldo_series(ledger)
    months = core_account.month_summary(ledger)
    if offset:
        saldo += offset
        series = [(d, v + offset) for d, v in series]
        forecast = [replace(f, saldo_vor=f.saldo_vor + offset, saldo_nach=f.saldo_nach + offset) for f in forecast]
        months = [replace(m, saldo_end=m.saldo_end + offset) for m in months]

    return LedgerContext(
        period=period,
        today=today,
        params=params,
        leave_mode=mode,
        klassenfahrt_variante=variante,
        days=pdays.days,
        ledger=ledger,
        plan=pdays.plan,
        ferien=pdays.ferien,
        holidays=pdays.holidays,
        ist_by_day={d: s for d, s in ist_all.items() if period.start <= d <= period.end},
        krank_partial={d: s for d, s in krank_all.items() if period.start <= d <= period.end},
        saldo_heute=saldo,
        saldo_tage=core_account.saldo_tage(saldo, params),
        j_soll=core_account.j_soll(ledger),
        soll_bisher=sum(r.soll for r in ledger if r.date <= today),
        ist_bisher=core_account.ist_bisher(ledger, today),
        ferienarbeit_bisher=core_account.ferienarbeit(ledger, until=today),
        ferienarbeit_geplant=geplant,
        prognose_ende=core_account.prognose_ende(ledger, today, n_schultage, geplant) + offset,
        w_max=core_account.w_max(ledger, today, geplant),
        eu=eu,
        schulwochen_ueber_48=core_account.schulwochen_ueber(ledger, 48),
        holiday_forecast=forecast,
        series=series,
        weeks=core_account.week_summary(ledger),
        months=months,
        categories=categories,
        unterricht_seconds=unterricht,
        unterricht_share=(unterricht / total) if total else 0.0,
        mehrarbeit=mehrarbeit_by_month(teaching, int(settings.unterrichtsstunde_min or 45)),
        planned_work=planned,
        eu_daten_ab=eu_daten_ab,
        erfassung_ab=erfassung_ab,
        uebertrag=carry,
        uebertrag_label=carry_label,
        uebertrag_verrechnet=bool(offset),
    )


# =========================================================================== leave-mode comparison


def compare_leave_modes(db: Session, period: Period | str | None = None, today: date | None = None) -> list[dict[str, Any]]:
    """Konzept §6.2: the account for 'sommerferien_zuerst' and 'proportional' side by side.

    Each entry: mode, label, massgeblich (the configured mode), saldo_heute, prognose_ende,
    saldo_periodenende (saldo over the whole period with the data recorded so far), j_soll and
    the leave distribution per ferien block.
    """
    today = today or today_local()
    if not isinstance(period, Period):
        period = cal.resolve_period(db, period, today)
    settings = get_settings(db)
    result = []
    for mode in (LeaveMode.SOMMERFERIEN_ZUERST, LeaveMode.PROPORTIONAL):
        ctx = load_context(db, period, today, leave_mode=mode)
        info = cal.leave_plan_info_from(
            cal.PeriodDays(
                ctx.period.id, period.start, period.end, ctx.days, ctx.plan, mode, ctx.ferien, ctx.holidays
            )
        )
        result.append(
            {
                "mode": mode.value,
                "label": cal.LEAVE_MODE_LABELS[mode],
                "massgeblich": settings.urlaubs_modus == mode.value,
                "saldo_heute": ctx.saldo_heute,
                "prognose_ende": ctx.prognose_ende,
                "saldo_periodenende": core_account.saldo_at(ctx.ledger, period.end),
                "j_soll": ctx.j_soll,
                "urlaub": info,
            }
        )
    return result


# =========================================================================== single day


@dataclass(frozen=True)
class DayDetail:
    date: date
    weekday: int
    day_type: str
    absence: str | None
    ferien_id: str | None
    ferien_name: str | None
    holiday_name: str | None
    is_holiday: bool
    note: str | None
    override: dict[str, Any] | None
    period_id: str | None
    soll: int
    ist: int
    ist_erfasst: int
    krank_gutschrift: int
    entries: list[dict[str, Any]]
    vorschlaege: list[dict[str, Any]]

    @property
    def delta(self) -> int:
        return self.ist - self.soll


def entry_dict(entry: TimeEntry, cats: dict[int, Category], now: datetime | None = None) -> dict[str, Any]:
    cat = cats.get(entry.category_id)
    sub = cats.get(entry.subcategory_id) if entry.subcategory_id else None
    seconds = entry.duration_seconds or 0
    if entry.end_at is None:
        seconds = max(0, int(((now or now_utc()) - entry.start_at).total_seconds()))
    return {
        "id": entry.id,
        "start_at": entry.start_at,
        "end_at": entry.end_at,
        "start_local": to_local(entry.start_at).isoformat() if entry.start_at else None,
        "end_local": to_local(entry.end_at).isoformat() if entry.end_at else None,
        "duration_seconds": int(seconds),
        "running": entry.end_at is None,
        "has_bounds": bool(entry.has_bounds),
        "category": (
            {"id": cat.id, "code": cat.code, "name": cat.name, "color": cat.color, "is_pseudo": cat.is_pseudo}
            if cat
            else None
        ),
        "subcategory": {"id": sub.id, "code": sub.code, "name": sub.name} if sub else None,
        "klasse": entry.klasse,
        "fach": entry.fach,
        "location": entry.location,
        "note": entry.note,
        "flags": list(entry.flags or []),
        "source": entry.source,
        "status": entry.status,
        "locked": bool(entry.locked),
    }


def day_detail(db: Session, d: date) -> DayDetail:
    """Type, soll, ist and entries of one day (for ``GET /api/days/{date}``)."""
    settings = get_settings(db)
    params = soll_params(settings)
    cats = _categories(db)
    cal_row = db.get(CalendarDay, d)
    holiday = db.get(Holiday, d)
    override = db.get(DayOverride, d)
    is_holiday = holiday is not None and holiday.scope in cal.HOLIDAY_SCOPES

    if cal_row is not None:
        day = Day(
            date=d,
            day_type=DayType(cal_row.day_type),
            absence=AbsenceType(cal_row.absence_type) if cal_row.absence_type else None,
            ferien_id=cal_row.ferien_id,
            holiday_name=cal_row.holiday_name,
            is_holiday=is_holiday or cal_row.day_type == DayType.FEIERTAG.value,
            note=cal_row.note,
        )
    else:  # outside every period: weekday/holiday logic only
        holidays = holidays_between(d, d, settings.regionale_feiertage or ())
        overrides = {d: cal._core_override(override)} if override is not None else {}
        day = build_days(d, d, [], holidays, overrides, frozenset())[0]
    ferien = db.get(Ferien, day.ferien_id) if day.ferien_id else None

    all_entries = list(
        db.scalars(
            select(TimeEntry)
            .where(TimeEntry.day == d, TimeEntry.deleted_at.is_(None))
            .order_by(TimeEntry.start_at)
        )
    )
    now = now_utc()
    active = [e for e in all_entries if e.status == EntryStatus.ACTIVE.value]
    rows = [
        EntryRow(
            day=e.day,
            seconds=int(e.duration_seconds or 0),
            flags=tuple(e.flags or ()),
            category_id=_main_category_id(cats, e.category_id),
        )
        for e in active
        if e.end_at is not None
    ]
    # A running timer counts with its elapsed time on the day it covers (also when started before midnight).
    running = db.scalars(
        select(TimeEntry).where(
            TimeEntry.end_at.is_(None),
            TimeEntry.deleted_at.is_(None),
            TimeEntry.status == EntryStatus.ACTIVE.value,
            TimeEntry.day <= d,
        )
    ).all()
    for e in running:
        for part_day, part_seconds in running_parts(e.start_at, now):
            if part_day == d:
                rows.append(
                    EntryRow(d, part_seconds, tuple(e.flags or ()), _main_category_id(cats, e.category_id))
                )
    ist_by_day, krank = split_ist(
        rows, cats, settings.klassenfahrt_variante or "B", float(settings.klassenfahrt_cap_h or 10.0)
    )
    row = core_account.build_ledger(
        [day], params, ist_by_day, bool(settings.krank_neutralisiert), krank, settings.erfassung_ab
    )[0]

    return DayDetail(
        date=d,
        weekday=d.isoweekday(),
        day_type=day.day_type.value,
        absence=day.absence.value if day.absence else None,
        ferien_id=day.ferien_id,
        ferien_name=ferien.name if ferien else None,
        holiday_name=day.holiday_name,
        is_holiday=day.is_holiday,
        note=day.note,
        override=(
            {
                "absence_type": override.absence_type,
                "day_type": override.day_type,
                "urlaub_manuell": bool(override.urlaub_manuell),
                "note": override.note,
            }
            if override is not None
            else None
        ),
        period_id=cal_row.period_id if cal_row is not None else None,
        soll=row.soll,
        ist=row.ist,
        ist_erfasst=row.ist_erfasst,
        krank_gutschrift=row.krank_gutschrift,
        entries=[entry_dict(e, cats, now) for e in active],
        vorschlaege=[entry_dict(e, cats, now) for e in all_entries if e.status == EntryStatus.VORSCHLAG.value],
    )
