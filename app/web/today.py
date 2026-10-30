"""HTML – Screen 'Heute' (Konzept §8.1, §8.4, §3.1): timer, category chips, today tiles, today's
entries and timetable suggestions.

Routes (mounted by app.main without prefix, no login):

- ``GET /heute``                     full page
- ``GET /heute/teil/kacheln``        tiles partial (HTMX, refreshed on ``deputat-refresh`` / every 2 min)
- ``GET /heute/teil/eintraege``      today's entries partial (HTMX)
- ``GET /heute/teil/vorschlaege``    timetable suggestions partial (HTMX; empty when there are none)

All timer *actions* go through ``/api/timer/*`` from ``static/js/timer.js`` (offline queue in
``static/js/offline-queue.js``); this module only renders. Key figures come from
``app.services.ledger.load_context`` wrapped in :func:`_kpis`, which degrades to "–" when no
period exists (hint card linking /kalender) or the ledger fails.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Category, EntryStatus, Location, Settings, TimeEntry, TimerState
from app.db.session import get_db
from app.services import timer as timer_svc
from app.util import now_utc, to_local, today_local
from app.web.templating import templates

log = logging.getLogger("app.web.today")

router = APIRouter()

RECENT_DAYS = 90  # category usage window for the chip order

LOCATION_LABELS = {
    Location.SCHULE.value: "Schule",
    Location.ZUHAUSE.value: "Zuhause",
    Location.UNTERWEGS.value: "Unterwegs",
    Location.EXTERN.value: "Extern",
}

FLAG_LABELS = {
    "ferienarbeit": "Ferienarbeit",
    "wochenende": "Wochenende",
    "feiertag": "Feiertag",
    "nachtraeglich": "nachträglich",
    "korrigiert": "korrigiert",
    "vertretung_angeordnet": "Vertretung angeordnet",
    "vertretung_genehmigt": "Vertretung genehmigt",
    "klassenfahrt": "Klassenfahrt",
    "nachtbereitschaft": "Nachtbereitschaft",
    "ohne_zeitgrenzen": "ohne Uhrzeit",
}
FLAG_KINDS = {"nachtraeglich": "badge-warn", "korrigiert": "badge-warn", "ohne_zeitgrenzen": "badge-outline"}

DAY_TYPE_LABELS = {
    "schultag": "Schultag",
    "ferientag": "Ferientag",
    "unterrichtsfrei": "Unterrichtsfrei",
    "feiertag": "Feiertag",
    "wochenende": "Wochenende",
}
ABSENCE_LABELS = {
    "urlaub": "Urlaub",
    "krank": "Krank",
    "fortbildung": "Fortbildung",
    "dienstbefreiung": "Dienstbefreiung",
    "sonderurlaub": "Sonderurlaub",
    "elternzeit": "Elternzeit",
}


# --------------------------------------------------------------------------- helpers


def _iso_z(dt: datetime | None) -> str | None:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def _running_entry(db: Session) -> TimeEntry | None:
    return db.scalars(
        select(TimeEntry)
        .where(TimeEntry.end_at.is_(None), TimeEntry.deleted_at.is_(None), TimeEntry.status == EntryStatus.ACTIVE.value)
        .order_by(TimeEntry.start_at.desc())
    ).first()


def _elapsed(entry: TimeEntry | None, now: datetime) -> int:
    if entry is None:
        return 0
    return max(0, int((now - entry.start_at).total_seconds()))


def _is_pseudo(entry: TimeEntry) -> bool:
    cat = entry.category
    return bool(cat is not None and (cat.is_pseudo or (cat.parent is not None and cat.parent.is_pseudo)))


def _ist_between(db: Session, start: date, end: date, now: datetime) -> int:
    """Recorded work (active, not deleted, no pseudo category) for ``start <= day <= end``.

    Private fallback for when the ledger/period is not available.
    """
    rows = db.scalars(
        select(TimeEntry).where(
            TimeEntry.day.between(start, end),
            TimeEntry.status == EntryStatus.ACTIVE.value,
            TimeEntry.deleted_at.is_(None),
        )
    )
    total = 0
    for e in rows:
        if _is_pseudo(e):
            continue
        total += _elapsed(e, now) if e.end_at is None else int(e.duration_seconds or 0)
    return total


@dataclass
class Kpis:
    has_period: bool | None  # None = unknown (ledger error)
    period_label: str | None = None
    error: str | None = None
    ist_heute: int = 0
    ist_heute_base: int = 0  # without the running timer's elapsed time (JS adds it live)
    soll_heute: int | None = None
    saldo: int | None = None
    week_ist: int = 0
    week_ist_base: int = 0
    week_soll: int | None = None
    next_ferien: dict[str, Any] | None = None
    day_label: str | None = None
    live: bool = False  # running timer belongs to today -> JS ticks the tiles


def _kpis(db: Session, today: date, *, now: datetime | None = None) -> Kpis:
    """Tiles for 'Heute' from :mod:`app.services.ledger`, degrading gracefully.

    Without a period (``NoPeriodError``) only the recorded Ist values are shown and ``has_period`` is
    False (the template renders the hint "Noch kein Schuljahr angelegt → Kalender"). Any other ledger
    failure is logged and rendered as "–".
    """
    now = now or now_utc()
    running = _running_entry(db)
    live_elapsed = _elapsed(running, now) if running is not None and running.day == today else 0
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    result = Kpis(has_period=None)
    ctx = None
    try:
        from app.services import calendar_service as cal
        from app.services import ledger as ledger_service

        try:
            ctx = ledger_service.load_context(db, today=today)
            result.has_period = True
        except cal.NoPeriodError:
            result.has_period = False
    except Exception:  # ledger is built concurrently / may be incomplete: never break the page
        log.exception("Kennzahlen für 'Heute' konnten nicht berechnet werden")
        db.rollback()
        result.error = "Kennzahlen sind gerade nicht verfügbar."
        ctx = None

    row = ctx.row(today) if ctx is not None else None
    if ctx is not None:
        result.period_label = ctx.period.label
        result.saldo = ctx.saldo_heute
        for f in sorted(ctx.holiday_forecast, key=lambda f: f.start):
            if f.end >= today:
                result.next_ferien = {
                    "name": f.name,
                    "start": f.start,
                    "end": f.end,
                    "laeuft": f.start <= today,
                    "abbau": f.abbau_kapazitaet,
                    "saldo_nach": f.saldo_nach,
                }
                break
        day = next((d for d in ctx.days if d.date == today), None)
        if day is not None:
            label = DAY_TYPE_LABELS.get(str(day.day_type.value), str(day.day_type.value))
            if day.holiday_name:
                label = day.holiday_name
            elif day.ferien_id:
                ferien = ctx.ferien_by_id(day.ferien_id)
                if ferien is not None:
                    label = f"{label} · {ferien.name}"
            if day.absence:
                label = f"{label} · {ABSENCE_LABELS.get(day.absence.value, day.absence.value)}"
            result.day_label = label

    if row is not None:
        result.ist_heute = int(row.ist)
        result.soll_heute = int(row.soll)
    else:
        result.ist_heute = _ist_between(db, today, today, now)

    week = ctx.week_of(today) if ctx is not None and row is not None else None
    if week is not None:
        result.week_ist = int(week.ist)
        result.week_soll = int(week.soll)
    else:
        result.week_ist = _ist_between(db, monday, sunday, now)

    result.live = live_elapsed > 0 or (running is not None and running.day == today)
    result.ist_heute_base = max(0, result.ist_heute - live_elapsed)
    result.week_ist_base = max(0, result.week_ist - live_elapsed)
    return result


def _main_categories(db: Session) -> list[Category]:
    return list(
        db.scalars(
            select(Category)
            .where(Category.parent_id.is_(None), Category.active.is_(True), Category.is_pseudo.is_(False))
            .order_by(Category.sort, Category.code)
        )
    )


def _chips(db: Session, now: datetime) -> list[Category]:
    """Main categories, recently used first (Konzept §8.1), the rest in catalogue order."""
    cats = _main_categories(db)
    by_id = {c.id: c for c in db.scalars(select(Category))}

    def main_id(cid: int) -> int:
        cat = by_id.get(cid)
        depth = 0
        while cat is not None and cat.parent_id is not None and depth < 5:
            cid, cat, depth = cat.parent_id, by_id.get(cat.parent_id), depth + 1
        return cid

    last_used: dict[int, datetime] = {}
    rows = db.execute(
        select(TimeEntry.category_id, func.max(TimeEntry.start_at))
        .where(
            TimeEntry.deleted_at.is_(None),
            TimeEntry.status == EntryStatus.ACTIVE.value,
            TimeEntry.start_at >= now - timedelta(days=RECENT_DAYS),
        )
        .group_by(TimeEntry.category_id)
    )
    for cid, last in rows:
        mid = main_id(cid)
        if last is not None and (mid not in last_used or last > last_used[mid]):
            last_used[mid] = last
    recent = sorted((c for c in cats if c.id in last_used), key=lambda c: last_used[c.id], reverse=True)
    rest = [c for c in cats if c.id not in last_used]
    return recent + rest


def _entry_view(entry: TimeEntry, now: datetime) -> dict[str, Any]:
    cat = entry.category
    sub = entry.subcategory
    running = entry.end_at is None
    flags = [f for f in (entry.flags or []) if not (f == "ohne_zeitgrenzen" and not entry.has_bounds)]
    return {
        "id": entry.id,
        "running": running,
        "has_bounds": bool(entry.has_bounds),
        "start": to_local(entry.start_at).strftime("%H:%M") if entry.has_bounds else None,
        "end": to_local(entry.end_at).strftime("%H:%M") if entry.end_at and entry.has_bounds else None,
        "start_iso": _iso_z(entry.start_at),
        "seconds": _elapsed(entry, now) if running else int(entry.duration_seconds or 0),
        "code": cat.code if cat else "?",
        "name": cat.name if cat else "Unbekannt",
        "color": cat.color if cat else "#64748B",
        "subcategory": sub.name if sub else None,
        "klasse": entry.klasse,
        "fach": entry.fach,
        "location": LOCATION_LABELS.get(entry.location or "", entry.location),
        "note": entry.note,
        "flags": [{"key": f, "label": FLAG_LABELS.get(f, f), "cls": FLAG_KINDS.get(f, "")} for f in flags],
        "source": entry.source,
        "locked": bool(entry.locked),
    }


def _entries_today(db: Session, today: date, now: datetime, status: str) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(TimeEntry)
        .where(TimeEntry.day == today, TimeEntry.status == status, TimeEntry.deleted_at.is_(None))
        .order_by(TimeEntry.start_at, TimeEntry.id)
    )
    return [_entry_view(e, now) for e in rows]


def _paused_info(db: Session, by_id: dict[int, Category]) -> dict[str, Any] | None:
    state = db.get(TimerState, 1)
    payload = state.paused_payload if state is not None and isinstance(state.paused_payload, dict) else {}
    pause = payload.get("pause") if isinstance(payload, dict) else None
    if not pause or not pause.get("category_id"):
        return None
    cat = by_id.get(int(pause["category_id"]))
    return {
        "category_id": int(pause["category_id"]),
        "code": cat.code if cat else "?",
        "name": cat.name if cat else "Unbekannt",
        "color": cat.color if cat else "#64748B",
        "klasse": pause.get("klasse"),
        "fach": pause.get("fach"),
        "location": pause.get("location") if pause.get("location_explicit") else None,
        "paused_at": pause.get("paused_at"),
    }


def _timer_state(db: Session, now: datetime, today: date) -> dict[str, Any]:
    """Server timer state for timer.js (rendered into ``data-state``)."""
    by_id = {c.id: c for c in db.scalars(select(Category))}
    running = _running_entry(db)
    run: dict[str, Any] | None = None
    if running is not None:
        cat = by_id.get(running.category_id)
        run = {
            "entry_id": running.id,
            "category_id": running.category_id,
            "code": cat.code if cat else "?",
            "name": cat.name if cat else "Unbekannt",
            "color": cat.color if cat else "#64748B",
            "started_at": _iso_z(running.start_at),
            "day": running.day.isoformat(),
            "subcategory_id": running.subcategory_id,
            "klasse": running.klasse,
            "fach": running.fach,
            "location": running.location,
            "note": running.note,
        }
    return {
        "running": run,
        "paused": _paused_info(db, by_id) if running is None else None,
        "today": today.isoformat(),
        "server_time": _iso_z(now),
    }


def _settings_lists(db: Session) -> tuple[list[str], list[str]]:
    settings = db.get(Settings, 1)
    if settings is None:
        return [], []
    return list(settings.klassen or []), list(settings.faecher or [])


def _default_location_label(db: Session, now: datetime) -> str:
    try:
        from app.services.timer import default_location

        return LOCATION_LABELS.get(default_location(db, now), "")
    except Exception:  # pragma: no cover - defensive, the timer service is stable
        return ""


# --------------------------------------------------------------------------- routes


def _now_today() -> tuple[date, datetime]:
    return today_local(), now_utc().replace(microsecond=0)


def _ensure_today_suggestions(db: Session, today: date) -> None:
    """Konzept §7.2: suggestions are created at 00:05 *or on the first open of the day* (idempotent).

    Covers a server that was down at 00:05 and a timetable created during the day. Never breaks the page.
    """
    try:
        from app.db.models import DayConfirmation, EntrySource
        from app.services import timetable as timetable_svc

        if db.get(DayConfirmation, today) is not None:
            return
        already = db.scalar(select(func.count()).select_from(TimeEntry).where(
            TimeEntry.day == today, TimeEntry.source == EntrySource.STUNDENPLAN.value,
            TimeEntry.deleted_at.is_(None)))
        if already:  # the 00:05 job (or an earlier open) already ran today
            return
        timetable_svc.generate_suggestions(db, today)
    except Exception:
        log.exception("Stundenplan-Vorschläge für heute konnten nicht erzeugt werden")
        db.rollback()


@router.get("/heute", response_class=HTMLResponse)
def today_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    today, now = _now_today()
    _ensure_today_suggestions(db, today)
    klassen, faecher = _settings_lists(db)
    ctx = {
        "page_title": "Heute",
        "today": today,
        "kpis": _kpis(db, today, now=now),
        "chips": _chips(db, now),
        "state": _timer_state(db, now, today),
        "entries": _entries_today(db, today, now, EntryStatus.ACTIVE.value),
        "suggestions": _entries_today(db, today, now, EntryStatus.VORSCHLAG.value),
        "klassen": klassen,
        "faecher": faecher,
        "locations": LOCATION_LABELS,
        "timer_flags_all": [(f, FLAG_LABELS[f]) for f in timer_svc.TIMER_FLAGS_ALL],
        "timer_flags_u": [(f, FLAG_LABELS[f]) for f in timer_svc.TIMER_FLAGS_UNTERRICHT],
        "default_location": _default_location_label(db, now),
        "aktion": request.query_params.get("aktion"),
    }
    return templates.TemplateResponse(request, "today.html", ctx)


@router.get("/heute/teil/kacheln", response_class=HTMLResponse, include_in_schema=False)
def today_tiles(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    today, now = _now_today()
    return templates.TemplateResponse(
        request, "partials/today_tiles.html", {"kpis": _kpis(db, today, now=now), "today": today}
    )


@router.get("/heute/teil/eintraege", response_class=HTMLResponse, include_in_schema=False)
def today_entries(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    today, now = _now_today()
    return templates.TemplateResponse(
        request,
        "partials/today_entries.html",
        {"entries": _entries_today(db, today, now, EntryStatus.ACTIVE.value), "today": today},
    )


@router.get("/heute/teil/vorschlaege", response_class=HTMLResponse, include_in_schema=False)
def today_suggestions(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    today, now = _now_today()
    return templates.TemplateResponse(
        request,
        "partials/today_suggestions.html",
        {"suggestions": _entries_today(db, today, now, EntryStatus.VORSCHLAG.value), "today": today},
    )

