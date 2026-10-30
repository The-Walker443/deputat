"""Months: overview, month close (hash + lock + stored report) and Mehrarbeit per month (Konzept §10.1, §6.6).

Public API
----------
- :func:`month_overview` – figures of one month (soll, ist, saldo, saldo kumuliert, Ferienarbeit,
  weekend/holiday work, EU-Mittel at month end, Mehrarbeit incl. status and Frist-Hinweis), the open
  items that block closing, the weeks and categories of the month and the close state.
- :func:`list_months` – one compact row per month of a period (for the month list / Schuljahr screen).
- :func:`close_month` – refuses with open items unless ``force=True`` *with* a note; computes the hash
  with :func:`app.services.entries.month_entries_hash` (the canonical mapping lives there and in
  ``core.hashing`` – never duplicated here), locks the month's entries, writes ``month_close`` and stores
  the report as ``DATA_DIR/reports/YYYY-MM.html``.
- :func:`update_mehrarbeit` – status (offen | beantragt | ausgeglichen), note and the optional
  "Vertretung erhalten" figure of a month's ``mehrarbeit_month`` row.

A closed month can NOT be reopened (Beweiskraft, Konzept L4). Corrections go through
``services/entries.py`` with a mandatory reason; that service marks the month
``nachtraeglich_korrigiert`` and keeps the old hash in ``hash_history``. The stored report file of the
close is never overwritten; :func:`app.services.reports.render_month_report` always renders the
current state.

Figures come from :func:`app.services.ledger.load_context` (the only DB → core path) evaluated as of
``min(month end, today)``; for a running month the figures cover the days up to today.

Open items (Konzept §10.1 step 1):

- ``monat_laeuft`` – the month has not ended yet (blocking, cannot be forced),
- ``timer_laeuft`` – a timer is running in or before the month (blocking: its duration is unknown),
- ``vorschlag_unbestaetigt`` – a day with unconfirmed timetable suggestions,
- ``schultag_ohne_eintrag`` – a school day (with soll, no absence other than Fortbildung) without any
  entry, suggestion or day confirmation.

Only the last two can be overridden with ``force=True`` plus a note. The note and the overridden items
are printed in the stored report (``month_close`` has no column for them, see open issues).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_config
from app.core import account as core_account
from app.core.account import EuMittel, WeekSummary
from app.core.mehrarbeit import MehrarbeitMonth
from app.core.types import AbsenceType, DayLedger, DayType
from app.db.models import (
    Category,
    DayConfirmation,
    EntryStatus,
    MehrarbeitMonth as MehrarbeitRow,
    MehrarbeitStatus,
    MonthClose,
    MonthCloseStatus,
    Period,
    TimeEntry,
)
from app.services import calendar_service as cal
from app.services import entries as entries_service
from app.services import ledger as ledger_service
from app.util import fmt_dec, fmt_hm, now_utc, to_local, today_local

log = logging.getLogger(__name__)

MONTH_NAMES = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)
WEEKDAYS_SHORT = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")

FREIZEITAUSGLEICH_MONATE = 3  # KMBek Mehrarbeit: Freizeitausgleich within three months

STATUS_OFFEN = "offen"
STATUS_LABELS = {
    STATUS_OFFEN: "offen",
    MonthCloseStatus.ABGESCHLOSSEN.value: "abgeschlossen",
    MonthCloseStatus.NACHTRAEGLICH_KORRIGIERT.value: "nachträglich korrigiert",
}
MEHRARBEIT_STATUS_LABELS = {
    MehrarbeitStatus.OFFEN.value: "offen",
    MehrarbeitStatus.BEANTRAGT.value: "beantragt",
    MehrarbeitStatus.AUSGEGLICHEN.value: "ausgeglichen",
}

ITEM_MONAT_LAEUFT = "monat_laeuft"
ITEM_TIMER_LAEUFT = "timer_laeuft"
ITEM_VORSCHLAG = "vorschlag_unbestaetigt"
ITEM_OHNE_EINTRAG = "schultag_ohne_eintrag"

EXPORT_HINT = (
    "Bericht jetzt exportieren und an die eigene Mailadresse senden – der Zeitstempel der Mail ist ein "
    "externer Nachweis."
)
REOPEN_MESSAGE = (
    "Ein abgeschlossener Monat kann nicht wieder geöffnet werden. Korrekturen sind über den Eintrag mit "
    "Begründung möglich (der Abschluss wird dann als „nachträglich korrigiert“ markiert)."
)


# =========================================================================== errors


class MonthError(Exception):
    """Base class; ``status_code`` is the HTTP status the API uses, ``message`` is German."""

    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class MonthValidationError(MonthError):
    status_code = 422


class MonthNotFound(MonthError):
    status_code = 404


class MonthConflict(MonthError):
    status_code = 409


class MonthOpenItems(MonthConflict):
    """Closing refused because of open items; ``items`` lists them."""

    def __init__(self, message: str, items: list[OpenItem]):
        super().__init__(message)
        self.items = items


# =========================================================================== data


@dataclass(frozen=True)
class OpenItem:
    kind: str
    message: str
    date: date | None = None
    blocking: bool = False  # cannot be overridden with force


@dataclass(frozen=True)
class MehrarbeitInfo:
    year_month: str
    ustd_angeordnet: float
    ustd_genehmigt: float
    ustd_total: float
    ustd_ausgleichsfaehig: float
    ustd_vertretung_erhalten: float
    status: str
    status_label: str
    note: str | None
    frist: date | None  # Freizeitausgleich deadline (only when ausgleichsfähig)
    frist_abgelaufen: bool
    hinweis: str | None


@dataclass(frozen=True)
class CategoryMonth:
    category_id: int
    code: str
    name: str
    color: str
    seconds: int
    share: float
    is_unterricht: bool


@dataclass(frozen=True)
class CloseInfo:
    closed_at: datetime
    hash: str
    soll_seconds: int
    ist_seconds: int
    saldo_seconds: int
    report_file: str | None
    status: str
    corrected_at: datetime | None
    hash_history: list[dict[str, Any]]
    note: str | None = None  # reason of a forced close
    forced_items: list[dict[str, Any]] = field(default_factory=list)  # overridden open items


@dataclass
class MonthOverview:
    year_month: str
    label: str
    start: date
    end: date
    stichtag: date  # figures cover start..stichtag
    period_id: str
    period_label: str
    status: str  # offen | abgeschlossen | nachtraeglich_korrigiert
    status_label: str
    laufend: bool  # month not ended yet
    zukunft: bool  # month not started yet
    soll: int
    soll_monat_gesamt: int  # soll of the whole month (differs from soll for a running month)
    ist: int
    ist_erfasst: int
    krank_gutschrift: int
    saldo: int  # month delta
    saldo_kumuliert: int  # period saldo at stichtag
    ferienarbeit: int
    wochenend_feiertag_arbeit: int
    eu: EuMittel
    w_max: int
    weeks: list[WeekSummary]
    categories: list[CategoryMonth]
    mehrarbeit: MehrarbeitInfo
    open_items: list[OpenItem]
    entries_count: int
    abschluss: CloseInfo | None
    can_close: bool
    can_force: bool
    ledger: list[DayLedger] = field(default_factory=list, repr=False)

    @property
    def is_closed(self) -> bool:
        return self.abschluss is not None


@dataclass(frozen=True)
class MonthRow:
    year_month: str
    label: str
    start: date
    end: date
    soll: int
    ist: int
    saldo: int
    saldo_kumuliert: int
    ferienarbeit: int
    ustd_total: float
    ustd_ausgleichsfaehig: float
    mehrarbeit_status: str
    status: str
    status_label: str
    hash: str | None
    closed_at: datetime | None
    laufend: bool
    zukunft: bool


@dataclass(frozen=True)
class CloseResult:
    month_close: MonthClose
    overview: MonthOverview
    report_path: Path
    forced_items: list[OpenItem]
    hinweis: str


# =========================================================================== helpers


def parse_ym(ym: str) -> tuple[date, date]:
    """'YYYY-MM' -> (first day, last day); raises :class:`MonthValidationError`."""
    text = (ym or "").strip()
    try:
        if len(text) != 7 or text[4] != "-":
            raise ValueError
        year, month = int(text[:4]), int(text[5:])
        if not 2000 <= year <= 2099:
            raise ValueError
        start = date(year, month, 1)
    except (ValueError, TypeError):
        raise MonthValidationError(f"Ungültiger Monat: {ym!r} (erwartet JJJJ-MM).") from None
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return start, nxt - timedelta(days=1)


def ym_of(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_label(ym: str) -> str:
    start, _ = parse_ym(ym)
    return f"{MONTH_NAMES[start.month - 1]} {start.year}"


def _add_months(d: date, months: int) -> date:
    """First day of the month ``months`` after the month of ``d``."""
    index = d.year * 12 + (d.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def freizeitausgleich_frist(ym: str) -> date:
    """Last day for Freizeitausgleich: end of the third month after ``ym``."""
    start, _ = parse_ym(ym)
    return _add_months(start, FREIZEITAUSGLEICH_MONATE + 1) - timedelta(days=1)


def _date_de(d: date) -> str:
    return f"{WEEKDAYS_SHORT[d.weekday()]} {d.strftime('%d.%m.%Y')}"


def _period_for_month(db: Session, start: date, end: date) -> Period:
    period = cal.period_for_date(db, start) or cal.period_for_date(db, end)
    if period is None:
        raise MonthNotFound(
            f"Für {MONTH_NAMES[start.month - 1]} {start.year} ist kein Schuljahr angelegt. "
            + cal.NO_PERIOD_HINT
        )
    return period


def _context(db: Session, period: Period, as_of: date) -> ledger_service.LedgerContext:
    try:
        return ledger_service.load_context(db, period, today=as_of)
    except (cal.NoPeriodError, cal.NotFoundError) as exc:
        raise MonthNotFound(str(exc)) from None


def _close_info(mc: MonthClose | None) -> CloseInfo | None:
    if mc is None:
        return None
    return CloseInfo(
        closed_at=mc.closed_at,
        hash=mc.entries_hash_sha256,
        soll_seconds=mc.soll_seconds,
        ist_seconds=mc.ist_seconds,
        saldo_seconds=mc.saldo_seconds,
        report_file=mc.report_file,
        status=mc.status,
        corrected_at=mc.corrected_at,
        hash_history=list(mc.hash_history or []),
        note=mc.note,
        forced_items=list(mc.forced_items or []),
    )


# =========================================================================== mehrarbeit


def _mehrarbeit_info(ym: str, mm: MehrarbeitMonth | None, row: MehrarbeitRow | None, today: date) -> MehrarbeitInfo:
    angeordnet = mm.ustd_angeordnet if mm else 0.0
    genehmigt = mm.ustd_genehmigt if mm else 0.0
    total = mm.total if mm else 0.0
    ausgleich = mm.ausgleichsfaehig if mm else 0.0
    status = row.status if row is not None else MehrarbeitStatus.OFFEN.value
    erhalten = float(row.ustd_vertretung_erhalten or 0.0) if row is not None else 0.0

    frist: date | None = None
    abgelaufen = False
    hinweis: str | None = None
    if ausgleich > 0:
        frist = freizeitausgleich_frist(ym)
        abgelaufen = today > frist
        frist_text = frist.strftime("%d.%m.%Y")
        if status == MehrarbeitStatus.AUSGEGLICHEN.value:
            hinweis = "Mehrarbeit ausgeglichen."
        elif status == MehrarbeitStatus.BEANTRAGT.value:
            hinweis = f"Ausgleich beantragt (Frist für Freizeitausgleich: {frist_text})."
        elif abgelaufen:
            hinweis = (
                f"Frist für Freizeitausgleich am {frist_text} abgelaufen – Mehrarbeitsvergütung beantragen."
            )
        else:
            hinweis = (
                f"Freizeitausgleich innerhalb von drei Monaten, also bis {frist_text}; "
                "sonst Mehrarbeitsvergütung beantragen."
            )
    elif total > 0:
        hinweis = "Bagatellgrenze (3 UStd im Monat) nicht überschritten – kein Ausgleichsanspruch."
    return MehrarbeitInfo(
        year_month=ym,
        ustd_angeordnet=angeordnet,
        ustd_genehmigt=genehmigt,
        ustd_total=total,
        ustd_ausgleichsfaehig=ausgleich,
        ustd_vertretung_erhalten=erhalten,
        status=status,
        status_label=MEHRARBEIT_STATUS_LABELS.get(status, status),
        note=row.note if row is not None else None,
        frist=frist,
        frist_abgelaufen=abgelaufen,
        hinweis=hinweis,
    )


def sync_mehrarbeit(db: Session, ym: str, mm: MehrarbeitMonth | None, *, commit: bool = True) -> MehrarbeitRow | None:
    """Store the computed UStd figures in ``mehrarbeit_month`` (status/note/erhalten are kept).

    A row is created only when the month has Mehrarbeit; existing rows are updated (also to 0).
    Commits only if something changed.
    """
    row = db.get(MehrarbeitRow, ym)
    values = {
        "ustd_angeordnet": mm.ustd_angeordnet if mm else 0.0,
        "ustd_genehmigt": mm.ustd_genehmigt if mm else 0.0,
        "ustd_ausgleichsfaehig": mm.ausgleichsfaehig if mm else 0.0,
    }
    if row is None:
        if mm is None or mm.total <= 0:
            return None
        row = MehrarbeitRow(year_month=ym, status=MehrarbeitStatus.OFFEN.value, **values)
        db.add(row)
        changed = True
    else:
        changed = any(float(getattr(row, k) or 0.0) != float(v) for k, v in values.items())
        if changed:
            for key, value in values.items():
                setattr(row, key, value)
    if changed and commit:
        db.commit()
    return row


# =========================================================================== open items


def _open_items(
    db: Session, ctx: ledger_service.LedgerContext, start: date, end: date, today: date
) -> list[OpenItem]:
    items: list[OpenItem] = []
    if end > today:
        items.append(OpenItem(
            ITEM_MONAT_LAEUFT,
            f"Der Monat ist noch nicht zu Ende (letzter Tag: {end.strftime('%d.%m.%Y')}).",
            None,
            blocking=True,
        ))
    running = entries_service.get_running_entry(db)
    if running is not None and running.day <= end:
        since = to_local(running.start_at).strftime("%d.%m.%Y %H:%M")
        items.append(OpenItem(
            ITEM_TIMER_LAEUFT, f"Timer läuft noch (seit {since}) – bitte zuerst stoppen.", running.day, blocking=True
        ))

    last = min(end, today)
    if last < start:
        return items

    active_days: set[date] = set()
    vorschlag_days: set[date] = set()
    for day, status in db.execute(
        select(TimeEntry.day, TimeEntry.status).where(
            TimeEntry.day.between(start, last), TimeEntry.deleted_at.is_(None)
        )
    ):
        if status == EntryStatus.ACTIVE.value:
            active_days.add(day)
        elif status == EntryStatus.VORSCHLAG.value:
            vorschlag_days.add(day)
    confirmed = set(db.scalars(select(DayConfirmation.date).where(DayConfirmation.date.between(start, last))))

    for d in sorted(vorschlag_days - confirmed):
        items.append(OpenItem(ITEM_VORSCHLAG, f"Unbestätigte Stundenplan-Vorschläge am {_date_de(d)}.", d))

    for row in ctx.ledger:
        if not (start <= row.date <= last):
            continue
        if row.day_type != DayType.SCHULTAG or row.soll <= 0 or row.vor_erfassung:
            continue
        if row.absence is not None and row.absence != AbsenceType.FORTBILDUNG:
            continue
        if row.date in active_days or row.date in vorschlag_days or row.date in confirmed:
            continue
        items.append(OpenItem(ITEM_OHNE_EINTRAG, f"Schultag ohne Einträge: {_date_de(row.date)}.", row.date))
    items.sort(key=lambda i: (not i.blocking, i.date or date.min, i.kind))
    return items


# =========================================================================== overview


def _month_categories(
    db: Session, start: date, end: date, ctx: ledger_service.LedgerContext
) -> tuple[list[CategoryMonth], int]:
    """Time per main category (pseudo categories excluded) and the number of active entries.

    Same basis as the month's Ist: the Klassenfahrt variant is applied (``ledger.evaluated_rows``), so the
    category total equals ``ist_erfasst``.
    """
    cats = {c.id: c for c in db.scalars(select(Category))}
    rows = ledger_service.load_entries(db, start, end, cats)
    per_cat: dict[int, int] = defaultdict(int)
    for row in ledger_service.evaluated_rows(rows, cats, ctx.ist_by_day, ctx.klassenfahrt_variante):
        per_cat[row.category_id] += row.seconds
    total = sum(per_cat.values())
    result = [
        CategoryMonth(
            category_id=cid,
            code=cats[cid].code,
            name=cats[cid].name,
            color=cats[cid].color,
            seconds=secs,
            share=(secs / total) if total else 0.0,
            is_unterricht=bool(cats[cid].is_unterricht),
        )
        for cid, secs in per_cat.items()
    ]
    result.sort(key=lambda c: (cats[c.category_id].sort, c.code))
    return result, len(rows)


def _is_we_or_holiday(row: DayLedger) -> bool:
    return row.day_type in (DayType.WOCHENENDE, DayType.FEIERTAG) or row.is_holiday


def month_overview(
    db: Session, ym: str, *, today: date | None = None, sync: bool = True
) -> MonthOverview:
    """All figures of one month (see module docstring). ``sync`` stores the Mehrarbeit figures."""
    start, end = parse_ym(ym)
    ym = ym_of(start)
    today = today or today_local()
    period = _period_for_month(db, start, end)
    as_of = max(start, min(end, today, period.end))
    # Key figures of the ledger (EU mean, W_max) are never evaluated at a future date: for a future month
    # the context is computed as of today (clamped into the period).
    ctx_day = as_of if start <= today else max(period.start, min(today, period.end))
    ctx = _context(db, period, ctx_day)
    return _build_overview(db, ym, start, end, today, as_of, period, ctx, sync=sync)


def _build_overview(
    db: Session,
    ym: str,
    start: date,
    end: date,
    today: date,
    as_of: date,
    period: Period,
    ctx: ledger_service.LedgerContext,
    *,
    sync: bool,
) -> MonthOverview:
    month_rows = [r for r in ctx.ledger if start <= r.date <= end]
    rows = [r for r in month_rows if r.date <= as_of] if start <= today else []
    soll = sum(r.soll for r in rows)
    ist = sum(r.ist for r in rows)

    mm = ctx.mehrarbeit.get((start.year, start.month))
    row = sync_mehrarbeit(db, ym, mm) if sync else db.get(MehrarbeitRow, ym)
    mehrarbeit = _mehrarbeit_info(ym, mm, row, today)

    mc = db.get(MonthClose, ym)
    abschluss = _close_info(mc)
    status = mc.status if mc is not None else STATUS_OFFEN
    items = _open_items(db, ctx, start, end, today) if mc is None else []
    categories, count = _month_categories(db, start, as_of, ctx) if start <= today else ([], 0)
    blocking = any(i.blocking for i in items)

    return MonthOverview(
        year_month=ym,
        label=f"{MONTH_NAMES[start.month - 1]} {start.year}",
        start=start,
        end=end,
        stichtag=as_of,
        period_id=period.id,
        period_label=period.label,
        status=status,
        status_label=STATUS_LABELS.get(status, status),
        laufend=start <= today < end,
        zukunft=today < start,
        soll=soll,
        soll_monat_gesamt=sum(r.soll for r in month_rows),
        ist=ist,
        ist_erfasst=sum(r.ist_erfasst for r in rows),
        krank_gutschrift=sum(r.krank_gutschrift for r in rows),
        saldo=ist - soll,
        saldo_kumuliert=ctx.saldo_at(as_of if start <= today else today),
        ferienarbeit=sum(r.ist_erfasst for r in rows if core_account.is_ferien_row(r)),
        wochenend_feiertag_arbeit=sum(r.ist_erfasst for r in rows if _is_we_or_holiday(r)),
        eu=ctx.eu,
        w_max=ctx.w_max,
        weeks=core_account.week_summary(rows),
        categories=categories,
        mehrarbeit=mehrarbeit,
        open_items=items,
        entries_count=count,
        abschluss=abschluss,
        can_close=mc is None and not items,
        can_force=mc is None and not blocking,
        ledger=month_rows,
    )


def list_months(db: Session, period: Period | str | None = None, *, today: date | None = None) -> list[MonthRow]:
    """One row per month of ``period`` (default: current period). Read-only."""
    today = today or today_local()
    if not isinstance(period, Period):
        try:
            period = cal.resolve_period(db, period, today)
        except (cal.NoPeriodError, cal.NotFoundError) as exc:
            raise MonthNotFound(str(exc)) from None
    as_of = max(period.start, min(today, period.end))
    ctx = _context(db, period, as_of)
    closes = {
        mc.year_month: mc
        for mc in db.scalars(select(MonthClose).where(
            MonthClose.year_month.between(ym_of(period.start), ym_of(period.end))
        ))
    }
    ma_rows = {
        r.year_month: r
        for r in db.scalars(select(MehrarbeitRow).where(
            MehrarbeitRow.year_month.between(ym_of(period.start), ym_of(period.end))
        ))
    }
    by_month: dict[tuple[int, int], list[DayLedger]] = defaultdict(list)
    for r in ctx.ledger:
        by_month[(r.date.year, r.date.month)].append(r)

    result: list[MonthRow] = []
    for (year, month), month_rows in sorted(by_month.items()):
        ym = f"{year:04d}-{month:02d}"
        start, end = parse_ym(ym)
        rows = [r for r in month_rows if r.date <= as_of] if start <= today else []
        soll = sum(r.soll for r in rows)
        ist = sum(r.ist for r in rows)
        # Cumulative saldo never runs past the Stichtag: a future month carries the saldo as of today
        # (the ledger holds full Soll and no Ist for the days after the Stichtag).
        last = min(rows[-1].date if rows else start - timedelta(days=1), as_of)
        mm = ctx.mehrarbeit.get((year, month))
        mc = closes.get(ym)
        status = mc.status if mc is not None else STATUS_OFFEN
        ma_row = ma_rows.get(ym)
        result.append(MonthRow(
            year_month=ym,
            label=f"{MONTH_NAMES[month - 1]} {year}",
            start=start,
            end=end,
            soll=soll,
            ist=ist,
            saldo=ist - soll,
            saldo_kumuliert=ctx.saldo_at(last),
            ferienarbeit=sum(r.ist_erfasst for r in rows if core_account.is_ferien_row(r)),
            ustd_total=mm.total if mm else 0.0,
            ustd_ausgleichsfaehig=mm.ausgleichsfaehig if mm else 0.0,
            mehrarbeit_status=ma_row.status if ma_row is not None else MehrarbeitStatus.OFFEN.value,
            status=status,
            status_label=STATUS_LABELS.get(status, status),
            hash=mc.entries_hash_sha256 if mc is not None else None,
            closed_at=mc.closed_at if mc is not None else None,
            laufend=start <= today < end,
            zukunft=today < start,
        ))
    return result


# =========================================================================== close


def _report_target(ym: str) -> tuple[Path, str]:
    cfg = get_config()
    cfg.reports_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.reports_dir / f"{ym}.html"
    return path, f"reports/{ym}.html"


def report_path(mc: MonthClose) -> Path | None:
    """Absolute path of a stored report (``report_file`` is relative to DATA_DIR)."""
    if not mc.report_file:
        return None
    cfg = get_config()
    reports_dir = cfg.reports_dir.resolve()
    path = (cfg.data_dir.resolve() / mc.report_file).resolve()
    # Only files directly in DATA_DIR/reports named like a month report (never other DATA_DIR files such as
    # the secret or the database, even if ``report_file`` was tampered with through a restore).
    if path.parent != reports_dir or not re.fullmatch(r"\d{4}-\d{2}\.html", path.name):
        return None
    return path


def close_month(
    db: Session,
    ym: str,
    *,
    force: bool = False,
    note: str | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> CloseResult:
    """Close a month: hash, lock the entries, store ``month_close`` and the report file.

    Raises :class:`MonthConflict` if already closed or a blocking item exists,
    :class:`MonthOpenItems` if open items exist and ``force`` is not set, and
    :class:`MonthValidationError` if ``force`` is set without a note.
    """
    from app.services import reports  # local import: reports imports this module

    start, end = parse_ym(ym)
    ym = ym_of(start)
    today = today or today_local()
    now = (now or now_utc()).replace(microsecond=0)
    note = (note or "").strip() or None

    if db.get(MonthClose, ym) is not None:
        raise MonthConflict(f"{month_label(ym)} ist bereits abgeschlossen. " + REOPEN_MESSAGE)
    if today < start:
        raise MonthConflict("Ein Monat in der Zukunft kann nicht abgeschlossen werden.")
    if force and not note:
        raise MonthValidationError("Bitte begründen, warum der Monat trotz offener Punkte abgeschlossen wird.")

    period = _period_for_month(db, start, end)
    as_of = max(start, min(end, today, period.end))
    ctx = _context(db, period, as_of)
    overview = _build_overview(db, ym, start, end, today, as_of, period, ctx, sync=True)

    blocking = [i for i in overview.open_items if i.blocking]
    if blocking:
        raise MonthOpenItems(" ".join(i.message for i in blocking), blocking)
    if overview.open_items and not force:
        n = len(overview.open_items)
        raise MonthOpenItems(
            f"{n} offene(r) Punkt(e) – bitte prüfen oder mit Begründung trotzdem abschließen.",
            overview.open_items,
        )
    forced = list(overview.open_items)

    entries_hash = entries_service.month_entries_hash(db, ym)
    path, relative = _report_target(ym)
    mc = MonthClose(
        year_month=ym,
        closed_at=now,
        entries_hash_sha256=entries_hash,
        soll_seconds=overview.soll,
        ist_seconds=overview.ist,
        saldo_seconds=overview.saldo,
        report_file=relative,
        status=MonthCloseStatus.ABGESCHLOSSEN.value,
        hash_history=[],
        note=note,
        forced_items=[
            {"kind": i.kind, "message": i.message, "date": i.date.isoformat() if i.date else None} for i in forced
        ],
    )
    try:
        db.add(mc)
        # Lock every entry of the month; ``locked``/``updated_at`` are not hash fields and the
        # entry's "geändert am" must not change because of the close.
        db.execute(
            update(TimeEntry)
            .where(TimeEntry.day.between(start, end))
            .values(locked=True, updated_at=TimeEntry.updated_at)
            .execution_options(synchronize_session=False)
        )
        db.flush()
        db.expire_all()
        html = reports.render_month_report(
            db, ym, today=today, now=now, close_note=note, forced_items=forced
        )
        if path.exists():  # report of an earlier database (e.g. before a restore): keep it
            path.rename(path.with_name(f"{ym}_alt_{now.strftime('%Y%m%d%H%M%S')}.html"))
        tmp = path.with_suffix(".html.part")
        tmp.write_text(html, encoding="utf-8")
        tmp.replace(path)
        db.commit()
    except Exception:
        db.rollback()
        raise
    log.info("Month %s closed (hash %s, forced items: %d)", ym, entries_hash, len(forced))
    db.refresh(mc)
    overview = month_overview(db, ym, today=today, sync=False)
    return CloseResult(month_close=mc, overview=overview, report_path=path, forced_items=forced, hinweis=EXPORT_HINT)


# =========================================================================== mehrarbeit edit

_UNSET: Any = object()


def update_mehrarbeit(
    db: Session,
    ym: str,
    *,
    status: str | None = None,
    note: Any = _UNSET,
    ustd_vertretung_erhalten: float | None = None,
    today: date | None = None,
) -> MehrarbeitInfo:
    """Edit status (offen | beantragt | ausgeglichen), note and "Vertretung erhalten" of a month."""
    start, end = parse_ym(ym)
    ym = ym_of(start)
    today = today or today_local()
    if status is not None and status not in MEHRARBEIT_STATUS_LABELS:
        raise MonthValidationError("Ungültiger Status (erlaubt: offen, beantragt, ausgeglichen).")
    if ustd_vertretung_erhalten is not None and not (0 <= float(ustd_vertretung_erhalten) <= 200):
        raise MonthValidationError("„Vertretung erhalten“ muss zwischen 0 und 200 UStd liegen.")
    period = _period_for_month(db, start, end)
    ctx = _context(db, period, max(start, min(end, today, period.end)))
    mm = ctx.mehrarbeit.get((start.year, start.month))
    row = sync_mehrarbeit(db, ym, mm, commit=False)
    if row is None:
        row = MehrarbeitRow(year_month=ym, status=MehrarbeitStatus.OFFEN.value, ustd_angeordnet=0.0,
                            ustd_genehmigt=0.0, ustd_ausgleichsfaehig=0.0)
        db.add(row)
    if status is not None:
        row.status = status
    if note is not _UNSET:
        text = (str(note).strip() if note is not None else "") or None
        if text is not None and len(text) > 2000:
            raise MonthValidationError("Die Notiz ist zu lang (höchstens 2000 Zeichen).")
        row.note = text
    if ustd_vertretung_erhalten is not None:
        row.ustd_vertretung_erhalten = float(ustd_vertretung_erhalten)
    db.commit()
    return _mehrarbeit_info(ym, mm, row, today)


# =========================================================================== serialisation


def _hm(seconds: int, plus: bool = False) -> str:
    return fmt_hm(seconds, plus=plus)


def eu_dict(eu: EuMittel) -> dict[str, Any]:
    return {
        "hours": round(eu.hours, 2),
        "hours_text": fmt_dec(eu.hours * 3600),
        "ampel": eu.ampel.value,
        "ist_seconds": eu.ist_seconds,
        "absence_days": eu.absence_days,
        "divisor_weeks": eu.divisor_weeks,
        "window_start": eu.window_start,
        "window_end": eu.window_end,
    }


def overview_to_dict(ov: MonthOverview) -> dict[str, Any]:
    """JSON-friendly representation (``*_hm`` fields are formatted h:mm strings)."""
    return {
        "year_month": ov.year_month,
        "label": ov.label,
        "start": ov.start,
        "end": ov.end,
        "stichtag": ov.stichtag,
        "period": {"id": ov.period_id, "label": ov.period_label},
        "status": ov.status,
        "status_label": ov.status_label,
        "is_closed": ov.is_closed,
        "laufend": ov.laufend,
        "zukunft": ov.zukunft,
        "soll": ov.soll,
        "soll_hm": _hm(ov.soll),
        "soll_monat_gesamt": ov.soll_monat_gesamt,
        "ist": ov.ist,
        "ist_hm": _hm(ov.ist),
        "ist_erfasst": ov.ist_erfasst,
        "krank_gutschrift": ov.krank_gutschrift,
        "saldo": ov.saldo,
        "saldo_hm": _hm(ov.saldo, True),
        "saldo_kumuliert": ov.saldo_kumuliert,
        "saldo_kumuliert_hm": _hm(ov.saldo_kumuliert, True),
        "ferienarbeit": ov.ferienarbeit,
        "ferienarbeit_hm": _hm(ov.ferienarbeit),
        "wochenend_feiertag_arbeit": ov.wochenend_feiertag_arbeit,
        "wochenend_feiertag_arbeit_hm": _hm(ov.wochenend_feiertag_arbeit),
        "eu_mittel": eu_dict(ov.eu),
        "w_max": ov.w_max,
        "w_max_hm": _hm(ov.w_max),
        "weeks": [
            {
                "iso_year": w.iso_year,
                "iso_week": w.iso_week,
                "monday": w.monday,
                "soll": w.soll,
                "ist": w.ist,
                "ist_erfasst": w.ist_erfasst,
                "schultage": w.schultage,
                "soll_hm": _hm(w.soll),
                "ist_hm": _hm(w.ist),
                "delta_hm": _hm(w.delta, True),
            }
            for w in ov.weeks
        ],
        "categories": [{**asdict(c), "hm": _hm(c.seconds)} for c in ov.categories],
        "mehrarbeit": asdict(ov.mehrarbeit),
        "open_items": [asdict(i) for i in ov.open_items],
        "entries_count": ov.entries_count,
        "abschluss": asdict(ov.abschluss) if ov.abschluss else None,
        "can_close": ov.can_close,
        "can_force": ov.can_force,
    }


def month_row_to_dict(row: MonthRow) -> dict[str, Any]:
    data = asdict(row)
    data.update(
        soll_hm=_hm(row.soll),
        ist_hm=_hm(row.ist),
        saldo_hm=_hm(row.saldo, True),
        saldo_kumuliert_hm=_hm(row.saldo_kumuliert, True),
        ferienarbeit_hm=_hm(row.ferienarbeit),
    )
    return data
