"""HTML – home screen "Erfassen": one duration field per weekday, week by week (mobile first).

Routes (mounted by app.main without prefix, no login):

- ``GET /erfassen``               the current ISO week (home; ``/`` redirects here, PWA ``start_url``)
- ``GET /erfassen/{YYYY-Www}``    a given ISO week (invalid -> 404); ``?tag=YYYY-MM-DD`` focuses that day's field

With the header ``X-Erfassen-Partial: 1`` only the week block (``partials/erfassen_week.html``) is returned;
``static/js/erfassen.js`` uses it to switch weeks without a full page load. Saving goes through
``PUT /api/days/{date}/total`` via the offline queue (``static/js/offline-queue.js``); the view model comes
from :func:`app.services.quick_entry.week_view`.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Category, EntryStatus, TimeEntry
from app.db.session import get_db
from app.services import quick_entry as svc
from app.util import to_local, today_local
from app.web.templating import templates

log = logging.getLogger("app.web.erfassen")

router = APIRouter()

PARTIAL_HEADER = "x-erfassen-partial"


def _running_timer(db: Session) -> dict[str, Any] | None:
    """Small indicator only: category and start of a running timer (the timer itself lives on /heute)."""
    entry = db.scalars(
        select(TimeEntry)
        .where(TimeEntry.end_at.is_(None), TimeEntry.deleted_at.is_(None), TimeEntry.status == EntryStatus.ACTIVE.value)
        .order_by(TimeEntry.start_at.desc())
    ).first()
    if entry is None:
        return None
    cat = db.get(Category, entry.category_id)
    return {
        "code": cat.code if cat else "?",
        "name": cat.name if cat else "Timer",
        "since": to_local(entry.start_at).strftime("%H:%M"),
    }


def _focus_date(request: Request, monday: date) -> str | None:
    """``?tag=YYYY-MM-DD`` (e.g. from the reminder "Nachtragen"): that day's field gets the focus."""
    raw = request.query_params.get("tag") or ""
    try:
        d = date.fromisoformat(raw)
    except ValueError:
        return None
    return d.isoformat() if monday <= d <= monday + timedelta(days=6) else None


def _render(request: Request, db: Session, monday: date) -> HTMLResponse:
    today = today_local()
    try:
        week = svc.week_view(db, monday, today=today)
    except Exception:
        log.exception("Erfassen: Woche konnte nicht aufgebaut werden")
        db.rollback()
        raise HTTPException(500, "Die Woche kann gerade nicht angezeigt werden.") from None
    ctx = {"page_title": "Erfassen", "week": week, "timer": _running_timer(db), "focus": _focus_date(request, monday)}
    if request.headers.get(PARTIAL_HEADER) == "1":
        return templates.TemplateResponse(request, "partials/erfassen_week.html", ctx,
                                          headers={"Cache-Control": "no-store", "Vary": "X-Erfassen-Partial"})
    return templates.TemplateResponse(request, "erfassen.html", ctx, headers={"Vary": "X-Erfassen-Partial"})


@router.get("/erfassen", response_class=HTMLResponse)
def erfassen_current(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    from app.web.today import _ensure_today_suggestions

    today = today_local()
    # Home screen: like /heute, the first open of the day creates missing timetable suggestions (Konzept §7.2).
    _ensure_today_suggestions(db, today)
    return _render(request, db, svc.monday_of(today))


@router.get("/erfassen/{ref}", response_class=HTMLResponse)
def erfassen_week(request: Request, ref: str, db: Session = Depends(get_db)) -> HTMLResponse:
    monday = svc.parse_week_ref(ref)
    if monday is None:
        raise HTTPException(404, "Unbekannte Kalenderwoche – Format z. B. 2026-W45.")
    return _render(request, db, monday)
