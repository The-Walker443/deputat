"""HTML – Screen 'Stundenplan' (Konzept §7.2): timetable versions and the weekly grid editor.

Routes (mounted by app.main without prefix, no login; classic form posts with CSRF field and
Post/Redirect/Get + flash messages; the slot editor re-renders with the entered values on errors):

- ``GET  /stundenplan``                                 versions + weekly grid (``?version=<id>``; default:
                                                        the version valid today, else the latest one)
- ``GET  /stundenplan/{id}/stunde?tag=1..5&nr=0..99``   slot editor (partial for HTMX, full page otherwise)
- ``POST /stundenplan/{id}/stunde``                     save the slot (``art=""`` / ``aktion=loeschen`` clears it)
- ``POST /stundenplan/versionen``                       create a version (optionally ending the previous one)
- ``POST /stundenplan/versionen/{id}``                  change label / validity
- ``POST /stundenplan/versionen/{id}/kopieren``         copy with all slots (Halbjahreswechsel)
- ``POST /stundenplan/versionen/{id}/loeschen``         delete a version
- ``POST /stundenplan/versionen/{id}/zeitraster``       re-apply the times of the school's Zeitraster

All rules (overlaps, default categories, retiring stale suggestions) live in :mod:`app.services.timetable`.
The Zeitraster itself is edited under /einstellungen#zeitraster.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.db.models import Category, SlotKind, Timetable, TimetableSlot
from app.db.session import get_db
from app.services import entries as entries_svc
from app.services import settings_service as settings_svc
from app.services import timetable as tt_svc
from app.util import today_local
from app.web.templating import flash, templates

log = logging.getLogger("app.web.timetable")

router = APIRouter()

WEEKDAYS = ((1, "Mo", "Montag"), (2, "Di", "Dienstag"), (3, "Mi", "Mittwoch"), (4, "Do", "Donnerstag"),
            (5, "Fr", "Freitag"))
KIND_CHOICES = (
    (SlotKind.UNTERRICHT.value, "Unterricht"),
    (SlotKind.AUFSICHT.value, "Aufsicht"),
    (SlotKind.SPRECHSTUNDE.value, "Sprechstunde"),
    (SlotKind.KONFERENZ_FIX.value, "Feste Konferenz"),
    (SlotKind.FREI.value, "Freistunde"),
)
KIND_SHORT = {
    SlotKind.UNTERRICHT.value: "Unterricht",
    SlotKind.AUFSICHT.value: "Aufsicht",
    SlotKind.SPRECHSTUNDE.value: "Sprechst.",
    SlotKind.KONFERENZ_FIX.value: "Konferenz",
    SlotKind.FREI.value: "frei",
}


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _grid_url(tt_id: str | None, anchor: str = "") -> str:
    return "/stundenplan" + (f"?version={tt_id}" if tt_id else "") + anchor


def _error_text(exc: Exception) -> str:
    return getattr(exc, "message", None) or str(exc) or "Ungültige Eingabe."


# --------------------------------------------------------------------------- grid model


@dataclass
class GridRow:
    slot_no: int
    label: str
    start: str | None
    end: str | None
    cells: list[dict[str, Any] | None]


def _raster(db: Session) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for item in settings_svc.get_settings(db).zeitraster or []:
        try:
            out[int(item["slot_no"])] = item
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _cell(slot: TimetableSlot, categories: dict[int, Category]) -> dict[str, Any]:
    cat = categories.get(slot.category_id) if slot.category_id else None
    if slot.kind == SlotKind.UNTERRICHT.value:
        main = slot.klasse or slot.fach or "Unterricht"
        sub = (slot.fach or "") if slot.klasse else ""
    else:
        main = KIND_SHORT.get(slot.kind, slot.kind)
        sub = " ".join(x for x in (slot.klasse, slot.fach) if x)
    return {
        "slot": slot,
        "main": main,
        "sub": sub,
        "time": f"{slot.start_time:%H:%M}–{slot.end_time:%H:%M}",
        "color": cat.color if cat else None,
        "category": cat.name if cat else None,
        "is_frei": slot.kind == SlotKind.FREI.value,
        "title": f"{tt_svc.slot_label(slot)}, {slot.start_time:%H:%M}–{slot.end_time:%H:%M}"
                 + (f" ({cat.name})" if cat else ""),
    }


def build_grid(db: Session, tt: Timetable) -> list[GridRow]:
    raster = _raster(db)
    categories = {c.id: c for c in db.scalars(select(Category))}
    by_pos = {(s.weekday, s.slot_no): s for s in tt.slots}
    numbers = sorted(set(raster) | {s.slot_no for s in tt.slots})
    rows: list[GridRow] = []
    for no in numbers:
        r = raster.get(no)
        cells = [(_cell(by_pos[(wd, no)], categories) if (wd, no) in by_pos else None) for wd, _, _ in WEEKDAYS]
        rows.append(GridRow(no, (r or {}).get("label") or f"{no}. Std.", (r or {}).get("start"), (r or {}).get("end"),
                            cells))
    return rows


def _selected_version(db: Session, ref: str | None, versions: list[Timetable]) -> Timetable | None:
    if ref:
        try:
            return tt_svc.get_timetable(db, ref)
        except tt_svc.TimetableNotFound:
            raise HTTPException(404, "Stundenplan nicht gefunden.") from None
    return tt_svc.timetable_for_date(db, today_local()) or (versions[-1] if versions else None)


def _version_info(tt: Timetable, today: date) -> dict[str, Any]:
    return {
        "tt": tt,
        "current": tt.valid_from <= today and (tt.valid_to is None or tt.valid_to >= today),
        "future": tt.valid_from > today,
        "slot_count": len(tt.slots),
        "unterricht": sum(1 for s in tt.slots if s.kind == SlotKind.UNTERRICHT.value),
    }


# --------------------------------------------------------------------------- page


@router.get("/stundenplan", response_class=HTMLResponse)
def timetable_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    versions = tt_svc.list_timetables(db)
    tt = _selected_version(db, request.query_params.get("version"), versions)
    today = today_local()
    settings = settings_svc.get_settings(db)
    ctx: dict[str, Any] = {
        "page_title": "Stundenplan",
        "versions": [_version_info(v, today) for v in versions],
        "tt": tt,
        "info": _version_info(tt, today) if tt else None,
        "weekdays": WEEKDAYS,
        "grid": build_grid(db, tt) if tt else [],
        "has_raster": bool(settings.zeitraster),
        "today": today,
        "default_from": today if not versions else max(today, (versions[-1].valid_to or today) + timedelta(days=1)),
        "unterricht_ustd": None,
    }
    if tt:
        minutes = sum(
            (s.end_time.hour * 60 + s.end_time.minute) - (s.start_time.hour * 60 + s.start_time.minute)
            for s in tt.slots if s.kind == SlotKind.UNTERRICHT.value
        )
        ctx["unterricht_ustd"] = minutes / (settings.unterrichtsstunde_min or 45)
        ctx["upz_ist"] = settings.upz_ist
    return templates.TemplateResponse(request, "timetable.html", ctx)


# --------------------------------------------------------------------------- slot editor


def _category_choices(db: Session) -> list[dict[str, Any]]:
    rows = list(db.scalars(select(Category).order_by(Category.sort, Category.code)))
    mains = [c for c in rows if c.parent_id is None and not c.is_pseudo and c.active]
    groups = []
    for m in mains:
        children = [c for c in rows if c.parent_id == m.id and c.active]
        groups.append({"main": m, "children": children})
    return groups


def _slot_ctx(db: Session, tt: Timetable, weekday: int, slot_no: int) -> dict[str, Any]:
    slot = next((s for s in tt.slots if s.weekday == weekday and s.slot_no == slot_no), None)
    raster = _raster(db).get(slot_no)
    settings = settings_svc.get_settings(db)
    values = {
        "art": slot.kind if slot else SlotKind.UNTERRICHT.value,
        "klasse": (slot.klasse or "") if slot else "",
        "fach": (slot.fach or "") if slot else "",
        "category_id": str(slot.category_id or "") if slot else "",
        "start": f"{slot.start_time:%H:%M}" if slot else ((raster or {}).get("start") or ""),
        "end": f"{slot.end_time:%H:%M}" if slot else ((raster or {}).get("end") or ""),
    }
    return {
        "tt": tt,
        "slot": slot,
        "weekday": weekday,
        "weekday_name": dict((n, long) for n, _, long in WEEKDAYS)[weekday],
        "slot_no": slot_no,
        "raster": raster,
        "values": values,
        "kind_choices": KIND_CHOICES,
        "klassen": list(settings.klassen or []),
        "faecher": list(settings.faecher or []),
        "category_groups": _category_choices(db),
        "default_codes": tt_svc.DEFAULT_CATEGORY_CODES,
        "back": _grid_url(tt.id, "#raster"),
        "error": None,
    }


def _slot_position(request_values: Any) -> tuple[int, int]:
    try:
        weekday = int(str(request_values.get("tag") or ""))
        slot_no = int(str(request_values.get("nr") or ""))
    except ValueError:
        raise HTTPException(404, "Ungültige Stunde.") from None
    if not (1 <= weekday <= 5 and 0 <= slot_no <= tt_svc.MAX_SLOT_NO):
        raise HTTPException(404, "Ungültige Stunde.")
    return weekday, slot_no


def _get_tt(db: Session, tt_id: str) -> Timetable:
    try:
        return tt_svc.get_timetable(db, tt_id)
    except tt_svc.TimetableNotFound:
        raise HTTPException(404, "Stundenplan nicht gefunden.") from None


@router.get("/stundenplan/{tt_id}/stunde", response_class=HTMLResponse)
def slot_editor(tt_id: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    tt = _get_tt(db, tt_id)
    weekday, slot_no = _slot_position(request.query_params)
    ctx = _slot_ctx(db, tt, weekday, slot_no)
    if _is_htmx(request):
        return templates.TemplateResponse(request, "partials/timetable_slot_form.html", ctx)
    ctx["page_title"] = "Stunde bearbeiten"
    return templates.TemplateResponse(request, "timetable_slot.html", ctx)


def _slot_data(form: FormData, weekday: int, slot_no: int) -> dict[str, Any]:
    data: dict[str, Any] = {
        "weekday": weekday,
        "slot_no": slot_no,
        "kind": str(form.get("art") or "").strip(),
        "klasse": str(form.get("klasse") or "").strip() or None,
        "fach": str(form.get("fach") or "").strip() or None,
        "category_id": str(form.get("category_id") or "").strip() or None,
    }
    start, end = str(form.get("start") or "").strip(), str(form.get("end") or "").strip()
    if start:
        data["start_time"] = start
    if end:
        data["end_time"] = end
    return data


@router.post("/stundenplan/{tt_id}/stunde", include_in_schema=False)
async def save_slot(tt_id: str, request: Request, db: Session = Depends(get_db)):
    tt = _get_tt(db, tt_id)
    form = await request.form()
    weekday, slot_no = _slot_position(form)
    existing = next((s for s in tt.slots if s.weekday == weekday and s.slot_no == slot_no), None)
    data = _slot_data(form, weekday, slot_no)
    target = _grid_url(tt.id, "#raster")
    where = f"{dict((n, long) for n, _, long in WEEKDAYS)[weekday]}, Stunde {slot_no}"
    try:
        if form.get("aktion") == "loeschen" or not data["kind"]:
            if existing is not None:
                tt_svc.delete_slot(db, existing.id)
                flash(request, f"{where} geleert.", "success")
            return _redirect(target)
        if existing is None:
            tt_svc.add_slot(db, tt.id, data)
        else:
            changes = {k: v for k, v in data.items() if k not in ("weekday", "slot_no")}
            tt_svc.update_slot(db, existing.id, changes)
    except (entries_svc.DomainError, ValueError) as exc:
        db.rollback()
        ctx = _slot_ctx(db, _get_tt(db, tt_id), weekday, slot_no)
        ctx["values"] = {
            "art": data["kind"], "klasse": data["klasse"] or "", "fach": data["fach"] or "",
            "category_id": data["category_id"] or "", "start": str(form.get("start") or ""),
            "end": str(form.get("end") or ""),
        }
        ctx["error"] = _error_text(exc)
        ctx["page_title"] = "Stunde bearbeiten"
        return templates.TemplateResponse(request, "timetable_slot.html", ctx, status_code=422)
    flash(request, f"{where} gespeichert.", "success")
    return _redirect(target)


# --------------------------------------------------------------------------- versions


def _version_fields(form: FormData) -> dict[str, Any]:
    return {
        "label": str(form.get("label") or "").strip() or None,
        "valid_from": str(form.get("valid_from") or "").strip() or None,
        "valid_to": str(form.get("valid_to") or "").strip() or None,
    }


@router.post("/stundenplan/versionen", include_in_schema=False)
async def create_version(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = await request.form()
    fields = _version_fields(form)
    try:
        tt = tt_svc.create_timetable(db, close_previous=form.get("vorherige_beenden") == "1", **fields)
    except (entries_svc.DomainError, ValueError) as exc:
        db.rollback()
        flash(request, _error_text(exc), "danger")
        return _redirect("/stundenplan#versionen")
    flash(request, f"„{tt.label}“ angelegt. Tippe auf ein Feld im Raster, um eine Stunde einzutragen.", "success")
    return _redirect(_grid_url(tt.id, "#raster"))


@router.post("/stundenplan/versionen/{tt_id}", include_in_schema=False)
async def update_version(tt_id: str, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    _get_tt(db, tt_id)
    form = await request.form()
    fields = _version_fields(form)
    try:
        tt = tt_svc.update_timetable(db, tt_id, fields)
    except (entries_svc.DomainError, ValueError) as exc:
        db.rollback()
        flash(request, _error_text(exc), "danger")
        return _redirect(_grid_url(tt_id, "#versionen"))
    flash(request, f"„{tt.label}“ gespeichert.", "success")
    return _redirect(_grid_url(tt.id))


@router.post("/stundenplan/versionen/{tt_id}/kopieren", include_in_schema=False)
async def copy_version(tt_id: str, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    _get_tt(db, tt_id)
    form = await request.form()
    fields = _version_fields(form)
    if not fields["valid_from"]:
        flash(request, "Bitte angeben, ab wann die Kopie gilt.", "danger")
        return _redirect(_grid_url(tt_id, "#versionen"))
    try:
        copy = tt_svc.copy_timetable(db, tt_id, close_source=form.get("vorherige_beenden") == "1", **fields)
    except (entries_svc.DomainError, ValueError) as exc:
        db.rollback()
        flash(request, _error_text(exc), "danger")
        return _redirect(_grid_url(tt_id, "#versionen"))
    flash(request, f"„{copy.label}“ mit {len(copy.slots)} Stunden angelegt – jetzt anpassen.", "success")
    return _redirect(_grid_url(copy.id, "#raster"))


@router.post("/stundenplan/versionen/{tt_id}/loeschen", include_in_schema=False)
def delete_version(tt_id: str, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    tt = _get_tt(db, tt_id)
    label = tt.label
    tt_svc.delete_timetable(db, tt_id)
    flash(request, f"„{label}“ gelöscht. Bestätigte Einträge bleiben erhalten.", "success")
    return _redirect("/stundenplan")


@router.post("/stundenplan/versionen/{tt_id}/zeitraster", include_in_schema=False)
def apply_raster(tt_id: str, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    tt = _get_tt(db, tt_id)
    raster = _raster(db)
    slots = []
    for slot in tt.slots:
        data = tt_svc.slot_input(slot)
        if slot.slot_no in raster:
            data.pop("start_time")
            data.pop("end_time")
        slots.append(data)
    try:
        tt_svc.set_slots(db, tt.id, slots)
    except (entries_svc.DomainError, ValueError) as exc:
        db.rollback()
        flash(request, _error_text(exc), "danger")
        return _redirect(_grid_url(tt_id))
    flash(request, "Zeiten aus dem Zeitraster übernommen.", "success")
    return _redirect(_grid_url(tt_id, "#raster"))
