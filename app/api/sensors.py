"""JSON API – Smart-home sensor endpoint (Konzept §16, §3.3 Nr. 16, Phase 3).

``GET /api/sensors`` (mounted by app.main under ``/api`` with ``require_sensor_token``): Bearer token
mandatory – 404 when no token is configured, 401 when it is missing or wrong. Read-only.

Response (hours as decimal numbers with 2 places; ``null`` when there is no account period)::

    {"saldo_h": 83.25, "heute_ist_h": 6.5, "woche_ist_h": 31.75, "eu_mittel_h": 44.1, "eu_ampel": "gelb",
     "timer_running": true, "timer_category": "Unterricht", "prognose_ende_h": 12.0,
     "updated_at": "2026-10-30T09:15:00Z"}

``heute_ist_h`` / ``woche_ist_h`` are the account values of the screen "Heute" (running timer included,
Klassenfahrt variant applied, sick days neutralised). Without a period they fall back to the recorded
time of today / this ISO week.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Category, EntryStatus, TimeEntry
from app.db.session import get_db
from app.util import local_date, now_utc

log = logging.getLogger("app.api.sensors")

router = APIRouter(tags=["sensors"])


def _h(seconds: int | float | None) -> float | None:
    return None if seconds is None else round(seconds / 3600, 2)


def _running(db: Session) -> TimeEntry | None:
    return db.scalars(
        select(TimeEntry)
        .where(TimeEntry.end_at.is_(None), TimeEntry.deleted_at.is_(None), TimeEntry.status == EntryStatus.ACTIVE.value)
        .order_by(TimeEntry.start_at.desc())
    ).first()


def _recorded(db: Session, start: date, end: date, now: datetime) -> int:
    """Recorded work for ``start <= day <= end`` without the ledger (no period / ledger failure)."""
    cats = {c.id: c for c in db.scalars(select(Category))}

    def pseudo(cid: int) -> bool:
        cat, depth = cats.get(cid), 0
        while cat is not None and depth < 5:
            if cat.is_pseudo:
                return True
            cat, depth = (cats.get(cat.parent_id) if cat.parent_id else None), depth + 1
        return False

    total = 0
    for e in db.scalars(select(TimeEntry).where(
        TimeEntry.day.between(start, end),
        TimeEntry.status == EntryStatus.ACTIVE.value,
        TimeEntry.deleted_at.is_(None),
    )):
        if pseudo(e.category_id):
            continue
        total += max(0, int((now - e.start_at).total_seconds())) if e.end_at is None else int(e.duration_seconds or 0)
    return total


def sensor_values(db: Session, now: datetime | None = None) -> dict[str, Any]:
    now = (now or now_utc()).replace(microsecond=0)
    today = local_date(now)
    running = _running(db)
    category = db.get(Category, running.category_id) if running is not None else None

    values: dict[str, Any] = {
        "saldo_h": None,
        "heute_ist_h": None,
        "woche_ist_h": None,
        "eu_mittel_h": None,
        "eu_ampel": None,
        "timer_running": running is not None,
        "timer_category": category.name if category is not None else None,
        "prognose_ende_h": None,
        "updated_at": now.isoformat() + "Z",
    }

    ctx = None
    try:
        from app.services import calendar_service as cal
        from app.services import ledger as ledger_service

        try:
            ctx = ledger_service.load_context(db, today=today)
        except cal.NoPeriodError:
            ctx = None
    except Exception:  # sensors must answer even if the ledger fails
        log.exception("Sensor values: ledger failed")
        db.rollback()
        ctx = None

    row = ctx.row(today) if ctx is not None else None
    week = ctx.week_of(today) if row is not None else None
    if ctx is not None:
        values["saldo_h"] = _h(ctx.saldo_heute)
        values["prognose_ende_h"] = _h(ctx.prognose_ende)
        values["eu_mittel_h"] = round(float(ctx.eu.hours), 2)
        values["eu_ampel"] = str(getattr(ctx.eu.ampel, "value", ctx.eu.ampel))
    if row is not None:
        values["heute_ist_h"] = _h(row.ist)
    else:
        values["heute_ist_h"] = _h(_recorded(db, today, today, now))
    if week is not None:
        values["woche_ist_h"] = _h(week.ist)
    else:
        monday = today - timedelta(days=today.weekday())
        values["woche_ist_h"] = _h(_recorded(db, monday, monday + timedelta(days=6), now))
    return values


@router.get("/sensors")
def sensors(db: Annotated[Session, Depends(get_db)]) -> JSONResponse:
    return JSONResponse(sensor_values(db), headers={"Cache-Control": "no-store"})
