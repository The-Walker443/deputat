"""HTML – Day confirmation (Konzept §3.1 #3, §7.2, §8): confirm the timetable pre-fill of a school day per lesson.

Routes (mounted by app.main without prefix, no login):

- ``GET  /tage``         to-do list: school days up to today whose timetable is not confirmed or that have no entry
- ``GET  /tag/{date}``   one day: the planned lessons/slots with a deviation per lesson (wie geplant, entfallen,
                         Vertretung gehalten, Exkursion, Zeit angepasst), an additional Vertretung, reason field
- ``POST /tag/{date}``   confirm the day (``app.services.timetable.confirm_day``; works without JavaScript)

Opening a day page creates the day's timetable suggestions first (idempotent), so past days that were never
pre-filled (server down at 00:05, timetable created later) can be confirmed as well.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Category, Settings
from app.db.session import get_db
from app.security import safe_redirect_target
from app.services import calendar_service as cal
from app.services import entries as entries_svc
from app.services import timetable as tt_svc
from app.util import today_local
from app.web.templating import flash, templates

log = logging.getLogger("app.web.days")

router = APIRouter()

DEVIATION_CHOICES = (
    (tt_svc.DEVIATION_WIE_GEPLANT, "wie geplant"),
    (tt_svc.DEVIATION_ENTFALLEN, "entfallen"),
    (tt_svc.DEVIATION_VERTRETUNG, "Vertretung gehalten"),
    (tt_svc.DEVIATION_EXKURSION, "Exkursion / Unterrichtsgang"),
    (tt_svc.DEVIATION_ZEIT, "Zeit angepasst"),
)
FREE_CHOICES = (
    (tt_svc.DEVIATION_WIE_GEPLANT, "frei (wie geplant)"),
    (tt_svc.DEVIATION_VERTRETUNG, "Vertretung gehalten"),
)
ART_CHOICES = (("angeordnet", "angeordnet"), ("genehmigt", "genehmigt"))


def _parse_day(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(404, "Ungültiges Datum.") from None


def _lists(db: Session) -> tuple[list[str], list[str]]:
    settings = db.get(Settings, 1)
    if settings is None:
        return [], []
    return list(settings.klassen or []), list(settings.faecher or [])


def _ensure_suggestions(db: Session, d: date, today: date) -> None:
    """First open of a day (Konzept §7.2): create the planned suggestions (idempotent, never raises)."""
    if d > today:
        return
    try:
        tt_svc.generate_suggestions(db, d)
    except Exception:  # never break the page because of the pre-fill
        log.exception("Stundenplan-Vorschläge für %s konnten nicht erzeugt werden", d)
        db.rollback()


def _form_values(form: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """(deviations, extra Vertretungen, posted values per key) from the submitted form."""
    deviations: list[dict[str, Any]] = []
    posted: dict[str, dict[str, Any]] = {}
    for i, key in enumerate(form.getlist("key")):
        key = str(key)
        values = {
            "type": str(form.get(f"typ_{i}") or tt_svc.DEVIATION_WIE_GEPLANT),
            "klasse": str(form.get(f"klasse_{i}") or "").strip() or None,
            "fach": str(form.get(f"fach_{i}") or "").strip() or None,
            "art": str(form.get(f"art_{i}") or "angeordnet"),
            "start": str(form.get(f"start_{i}") or "").strip() or None,
            "end": str(form.get(f"end_{i}") or "").strip() or None,
            "note": str(form.get(f"notiz_{i}") or "").strip() or None,
        }
        posted[key] = values
        kind = values["type"]
        if kind == tt_svc.DEVIATION_WIE_GEPLANT:
            continue
        dev: dict[str, Any] = {"slot_id": key, "type": kind, "note": values["note"]}
        if kind == tt_svc.DEVIATION_VERTRETUNG:
            dev.update(klasse=values["klasse"], fach=values["fach"], art=values["art"],
                       start=values["start"], end=values["end"])
        elif kind in (tt_svc.DEVIATION_ZEIT, tt_svc.DEVIATION_EXKURSION):
            dev.update(start=values["start"], end=values["end"])
        deviations.append({k: v for k, v in dev.items() if v is not None})
    extra: list[dict[str, Any]] = []
    x_klasse = str(form.get("extra_klasse") or "").strip()
    x_start = str(form.get("extra_start") or "").strip()
    x_end = str(form.get("extra_end") or "").strip()
    if x_klasse or x_start or x_end:
        extra.append({
            "klasse": x_klasse or None, "start": x_start or None, "end": x_end or None,
            "fach": str(form.get("extra_fach") or "").strip() or None,
            "art": str(form.get("extra_art") or "angeordnet"),
        })
    return deviations, extra, posted


def _page(request: Request, db: Session, d: date, *, error: str | None = None,
          posted: dict[str, dict[str, Any]] | None = None, extra: dict[str, Any] | None = None,
          reason: str = "", next_url: str | None = None, status_code: int = 200) -> HTMLResponse:
    today = today_local()
    view = tt_svc.day_view(db, d, today=today)
    items = tt_svc.confirm_form_items(db, d) if view["schultag"] else []
    for row in items:  # re-show what was submitted after an error
        if posted and row["key"] in posted:
            p = posted[row["key"]]
            row["deviation"] = p["type"]
            row["dev"] = {k: v for k, v in p.items() if k != "type" and v}
    klassen, faecher = _lists(db)
    exkursion = db.scalars(select(Category).where(Category.code == tt_svc.EXKURSION_CATEGORY_CODE)).first()
    ctx = {
        "page_title": f"Tag {d.strftime('%d.%m.%Y')}",
        "day": d,
        "today": today,
        "view": view,
        "items": items,
        "has_lessons": any(not r["frei"] for r in items),
        "error": error,
        "reason": reason,
        "extra": extra or {},
        "klassen": klassen,
        "faecher": faecher,
        "deviation_choices": DEVIATION_CHOICES,
        "free_choices": FREE_CHOICES,
        "art_choices": ART_CHOICES,
        "exkursion_name": exkursion.name if exkursion else "Schulveranstaltung",
        "next_url": next_url or "",
        "back_url": next_url or "/tage",
    }
    return templates.TemplateResponse(request, "day_confirm.html", ctx, status_code=status_code)


@router.get("/tage", response_class=HTMLResponse)
def open_days_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    today = today_local()
    result = None
    message = None
    try:
        result = tt_svc.open_days(db, until=today, today=today)
    except cal.NoPeriodError:
        message = cal.NO_PERIOD_HINT
    days = list(reversed(result.days)) if result is not None else []
    ctx = {
        "page_title": "Offene Tage",
        "today": today,
        "result": result,
        "days": days,
        "message": message,
        "erfassung_ab": (db.get(Settings, 1).erfassung_ab if db.get(Settings, 1) is not None else None),
    }
    return templates.TemplateResponse(request, "days_open.html", ctx)


@router.get("/tag/{day}", response_class=HTMLResponse)
def day_page(day: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    d = _parse_day(day)
    _ensure_suggestions(db, d, today_local())
    return _page(request, db, d, next_url=safe_redirect_target(request.query_params.get("next")))


@router.post("/tag/{day}", response_class=HTMLResponse, include_in_schema=False)
async def confirm_day(day: str, request: Request, db: Session = Depends(get_db)):
    d = _parse_day(day)
    form = await request.form()
    next_url = safe_redirect_target(str(form.get("next") or "")) or None
    deviations, extra, posted = _form_values(form)
    reason = str(form.get("grund") or "").strip()
    try:
        result = tt_svc.confirm_day(db, d, deviations, extra=extra, reason=reason or None)
    except entries_svc.DomainError as exc:
        db.rollback()
        extra_values = {k: str(form.get(f"extra_{k}") or "") for k in ("klasse", "fach", "start", "end", "art")}
        return _page(request, db, d, error=exc.message, posted=posted, extra=extra_values, reason=reason,
                     next_url=next_url, status_code=exc.status_code if exc.status_code in (409, 422, 423) else 422)
    except cal.NoPeriodError as exc:
        flash(request, str(exc), "danger")
        return RedirectResponse("/kalender", status_code=303)
    counted = len(result.active)
    dropped = len(result.verworfen)
    text = f"{d.strftime('%d.%m.%Y')} bestätigt: {counted} Eintrag/Einträge zählen"
    if dropped:
        text += f", {dropped} entfallen"
    if result.extra:
        text += f", {len(result.extra)} zusätzliche Vertretung(en)"
    flash(request, text + ".", "success")
    return RedirectResponse(next_url or "/tage", status_code=303)
