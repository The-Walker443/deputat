"""HTML – Screen 'Einstellungen' (Konzept §4 settings, §18 defaults, §5 categories, §16 sensor token).

Routes (mounted by app.main without prefix, no login; classic form posts with CSRF field, Post/Redirect/Get
with flash messages; a validation error re-renders the page with the entered values and status 422):

- ``GET  /einstellungen``                                  all settings sections, token, Datenschutz, version
- ``POST /einstellungen/{abschnitt}``                      save one section (profil | upz | konto | regeln |
                                                           feiertage | listen | zeitraster)
- ``POST /einstellungen/api-token``                        create/rotate the sensor token -> page showing the
                                                           plaintext ONCE (never stored in the session)
- ``POST /einstellungen/api-token/loeschen``               revoke the token
- ``GET  /einstellungen/kategorien``                       category catalogue (Konzept §5)
- ``POST /einstellungen/kategorien/{id}``                  rename / color / active / Notizpflicht
- ``POST /einstellungen/kategorien/{id}/verschieben``      ``richtung=up|down``
- ``POST /einstellungen/kategorien/{id}/unterkategorie``   add a subcategory

Validation lives in :mod:`app.services.settings_service` and :mod:`app.services.categories`; this module only
converts form strings (German decimal comma, ``TT.MM.`` dates, checkbox presence) into typed values.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from app.config import get_config
from app.core.types import KlassenfahrtVariante, LeaveMode
from app.db.models import MonthClose, PeriodKind
from app.db.session import get_db
from app.services import categories as cat_svc
from app.services import settings_service as svc
from app.services.calendar_service import CalendarLocked
from app.web.templating import flash, templates

log = logging.getLogger("app.web.settings")

router = APIRouter()


# --------------------------------------------------------------------------- field metadata


@dataclass(frozen=True)
class FieldMeta:
    label: str
    kind: str  # text | int | float | date | bool | select | md | list | radios
    help: str = ""
    unit: str = ""
    step: str = "1"
    min: float | None = None
    max: float | None = None
    choices: tuple[tuple[str, str], ...] = ()
    optional: bool = False


LEAVE_MODE_CHOICES = (
    (LeaveMode.SOMMERFERIEN_ZUERST.value, "Sommerferien zuerst (Standard)"),
    (LeaveMode.PROPORTIONAL.value, "Proportional auf alle Ferien"),
    (LeaveMode.MANUELL.value, "Manuell im Kalender"),
)

FIELDS: dict[str, FieldMeta] = {
    "name": FieldMeta("Name", "text", "Erscheint in Berichten und Schreiben.", optional=True),
    "schule": FieldMeta("Schule", "text", "Erscheint in Berichten und Schreiben.", optional=True),
    "schulart": FieldMeta("Schulart", "text", "Kürzel, z. B. GS für Grundschule."),
    "upz_voll": FieldMeta(
        "UPZ Vollzeit", "int", "Unterrichtspflichtzeit einer Vollzeitkraft deiner Schulart (Grundschule: 28).",
        "Std.", min=1, max=40),
    "upz_ist": FieldMeta(
        "UPZ tatsächlich", "float",
        "Deine tatsächliche Unterrichtspflichtzeit – Teilzeit, Altersermäßigung und Anrechnungsstunden sind "
        "bereits abgezogen. Halbe Stunden sind möglich.", "Std.", step="0.5", min=0, max=40),
    "anrechnungsstunden": FieldMeta(
        "Anrechnungsstunden", "float",
        "Stunden für Sonderaufgaben (z. B. Fachbetreuung). Sie senken nur die Unterrichtsmenge, nicht das Soll.",
        "Std.", step="0.5", min=0, max=40),
    "anrechnungs_grund": FieldMeta("Grund der Anrechnung", "text", "z. B. Medienbeauftragte, Mentorat.",
                                   optional=True),
    "geburtsdatum": FieldMeta(
        "Geburtsdatum", "date",
        "Nur für die Altersermäßigung (ab 58: 1 Std., ab 60: 2 Std., ab 62: 3 Std.; Stichtag 31.01.).",
        optional=True),
    "altersermaessigung_manuell": FieldMeta("Altersermäßigung von Hand festlegen", "bool"),
    "altersermaessigung": FieldMeta("Altersermäßigung", "float", "", "Std.", step="0.5", min=0, max=10),
    "wochen_soll_voll_h": FieldMeta(
        "Wochen-Soll Vollzeit", "float",
        "Regelmäßige Wochenarbeitszeit eines Vollzeitbeamten (Bayern: 40 h). Das Tages-Soll ist ein Fünftel davon "
        "× Teilzeitfaktor.", "h", step="0.5", min=1, max=60),
    "urlaubstage": FieldMeta("Urlaubstage", "int", "Erholungsurlaub pro Jahr (5-Tage-Woche, auch bei Teilzeit).",
                             "Tage", min=0, max=60),
    "unterrichtsstunde_min": FieldMeta("Länge einer Unterrichtsstunde", "int",
                                       "Für die Umrechnung in Unterrichtsstunden (UStd).", "min", min=1, max=120),
    "kontoperiode": FieldMeta(
        "Kontoperiode", "select", "Gilt für neu angelegte Perioden; bestehende bleiben unverändert.",
        choices=((PeriodKind.SCHULJAHR.value, "Schuljahr"), (PeriodKind.KALENDERJAHR.value, "Kalenderjahr"))),
    "periode_start_md": FieldMeta("Beginn der Kontoperiode", "md",
                                  "Tag und Monat (TT.MM.), Standard 01.08. Gilt für neu angelegte Perioden."),
    "urlaubs_modus": FieldMeta(
        "Urlaubsmodus", "select",
        "Wie die Urlaubstage auf die Ferien verteilt werden. Im Kalender siehst du beide automatischen Varianten "
        "im Vergleich.", choices=LEAVE_MODE_CHOICES),
    "uebertrag_verrechnen": FieldMeta(
        "Übertrag ins neue Jahr verrechnen", "bool",
        "Aus (Standard): Der Saldo am Ende des Vorjahres wird auf „Konto“ nur ausgewiesen. An: Er wird zu Saldo "
        "und Prognose des neuen Jahres addiert."),
    "erfassung_ab": FieldMeta(
        "Erfassung ab", "date",
        "Erst mitten im Schuljahr begonnen? Tage vor diesem Datum zählen neutral (Ist = Soll) statt als "
        "„0 Stunden gearbeitet“. Leer lassen, wenn du ab Periodenbeginn erfasst.", optional=True),
    "prognose_schultage": FieldMeta(
        "Prognose-Basis", "int", "Anzahl der letzten Schultage, aus denen die Prognose zum Jahresende rechnet.",
        "Schultage", min=1, max=200),
    "grosse_pause_zaehlt": FieldMeta(
        "Große Pausen zählen als Arbeitszeit", "bool",
        "Aus (Standard): Pausen ohne Aufsicht zählen nicht. Aufsichten sind immer Arbeitszeit."),
    "krank_neutralisiert": FieldMeta(
        "Krankheit neutralisiert das Soll", "bool",
        "An (Standard): An Krankheitstagen gilt Ist = Soll, das Konto bleibt unverändert."),
    "klassenfahrt_variante": FieldMeta(
        "Klassenfahrt-Bewertung", "radios", "",
        choices=((KlassenfahrtVariante.B.value, "Variante B (Standard): gekappt pro Tag, ohne Nachtbereitschaft"),
                 (KlassenfahrtVariante.A.value, "Variante A: alle Stunden inklusive Nachtbereitschaft"))),
    "klassenfahrt_cap_h": FieldMeta("Kappung pro Fahrtag (Variante B)", "float", "", "h", step="0.5", min=1, max=24),
    "regionale_feiertage": FieldMeta("Regionale Feiertage", "list"),
    "klassen": FieldMeta("Klassen", "list", "Eine Klasse pro Zeile, z. B. 3b. Erscheint als Auswahl bei der "
                         "Erfassung und im Stundenplan."),
    "faecher": FieldMeta("Fächer", "list", "Ein Fach pro Zeile."),
    "zeitraster": FieldMeta("Zeitraster", "list"),
}

SECTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "profil": ("Person und Schule", ("name", "schule", "schulart")),
    "upz": ("Unterrichtspflichtzeit", ("upz_voll", "upz_ist", "anrechnungsstunden", "anrechnungs_grund",
                                       "geburtsdatum", "altersermaessigung_manuell", "altersermaessigung")),
    "konto": ("Arbeitszeitkonto", ("wochen_soll_voll_h", "urlaubstage", "unterrichtsstunde_min", "urlaubs_modus",
                                   "kontoperiode", "periode_start_md", "uebertrag_verrechnen", "erfassung_ab",
                                   "prognose_schultage")),
    "regeln": ("Zählregeln", ("grosse_pause_zaehlt", "krank_neutralisiert", "klassenfahrt_variante",
                              "klassenfahrt_cap_h")),
    "feiertage": ("Regionale Feiertage", ("regionale_feiertage",)),
    "listen": ("Klassen und Fächer", ("klassen", "faecher")),
    "zeitraster": ("Zeitraster", ("zeitraster",)),
}

REGIONAL_HINTS = {
    "mariae_himmelfahrt": "Nur in Gemeinden mit überwiegend katholischer Bevölkerung – bitte für den Schulort prüfen.",
    "friedensfest": "Nur im Stadtgebiet Augsburg.",
}

ZEITRASTER_BLANK_ROWS = 2
_MD_DE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.?\s*$")
_MD_ISO = re.compile(r"^\s*(\d{2})-(\d{2})\s*$")


# --------------------------------------------------------------------------- formatting


def _num_str(value: Any) -> str:
    if value is None:
        return ""
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _md_display(value: str | None) -> str:
    m = _MD_ISO.match(value or "")
    return f"{m.group(2)}.{m.group(1)}." if m else (value or "")


def display_values(settings: Any) -> dict[str, Any]:
    """Settings as form values (strings for inputs, bools for checkboxes, lists for multi fields)."""
    out: dict[str, Any] = {}
    for field, meta in FIELDS.items():
        value = getattr(settings, field)
        if meta.kind in ("int", "float"):
            out[field] = _num_str(value)
        elif meta.kind == "bool":
            out[field] = bool(value)
        elif meta.kind == "date":
            out[field] = value.isoformat() if isinstance(value, date) else ""
        elif meta.kind == "md":
            out[field] = _md_display(value)
        elif field in ("klassen", "faecher"):
            out[field] = "\n".join(value or [])
        elif field == "zeitraster":
            out[field] = [dict(slot) for slot in (value or [])]
        elif field == "regionale_feiertage":
            out[field] = list(value or [])
        else:
            out[field] = "" if value is None else str(value)
    return out


# --------------------------------------------------------------------------- parsing


def _err(field: str, message: str) -> svc.SettingsValidationError:
    return svc.SettingsValidationError(field, message)


def _parse_number(field: str, raw: str, integer: bool) -> float | int:
    meta = FIELDS[field]
    text = raw.strip().replace(" ", "").replace(",", ".")
    if not text:
        raise _err(field, f"{meta.label}: Bitte einen Wert angeben.")
    try:
        number = float(text)
    except ValueError:
        raise _err(field, f"{meta.label}: Zahl erwartet.") from None
    if integer:
        if not number.is_integer():
            raise _err(field, f"{meta.label}: ganze Zahl erwartet.")
        return int(number)
    return number


def _parse_md(field: str, raw: str) -> str:
    raw = raw.strip()
    if m := _MD_ISO.match(raw):
        return f"{m.group(1)}-{m.group(2)}"
    if m := _MD_DE.match(raw):
        return f"{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    raise _err(field, f"{FIELDS[field].label}: Bitte als TT.MM. angeben (z. B. 01.08.).")


def _split_list(raw: str) -> list[str]:
    return [item.strip() for item in re.split(r"[\n,;]", raw) if item.strip()]


def _parse_zeitraster(form: FormData) -> list[dict[str, Any]]:
    nos, labels = form.getlist("zr_slot_no"), form.getlist("zr_label")
    starts, ends = form.getlist("zr_start"), form.getlist("zr_end")
    rows: list[dict[str, Any]] = []
    for i in range(max(len(nos), len(starts), len(ends))):
        no = str(nos[i] if i < len(nos) else "").strip()
        label = str(labels[i] if i < len(labels) else "").strip()
        start = str(starts[i] if i < len(starts) else "").strip()
        end = str(ends[i] if i < len(ends) else "").strip()
        if not (start or end or label):
            continue  # blank row (or removed; a lone pre-filled number from "Weitere Stunde" counts as blank)
        if not (no and start and end):
            raise _err("zeitraster", f"Zeitraster, Zeile {i + 1}: Bitte Nummer, Beginn und Ende angeben "
                                     "(oder die Zeile ganz leeren).")
        try:
            slot_no = int(no)
        except ValueError:
            raise _err("zeitraster", f"Zeitraster, Zeile {i + 1}: Die Nummer muss eine ganze Zahl sein.") from None
        rows.append({"slot_no": slot_no, "start": start[:5], "end": end[:5], "label": label or None})
    return rows


def parse_section(section: str, form: FormData) -> dict[str, Any]:
    """Typed values for the fields of one section (raises SettingsValidationError on unparsable input)."""
    fields: dict[str, Any] = {}
    for field in SECTIONS[section][1]:
        meta = FIELDS[field]
        raw = str(form.get(field) or "")
        if meta.kind == "bool":
            fields[field] = form.get(field) in ("1", "on", "true")
        elif meta.kind in ("int", "float"):
            if field == "altersermaessigung" and form.get("altersermaessigung_manuell") not in ("1", "on", "true"):
                continue  # computed from the birth date
            fields[field] = _parse_number(field, raw, meta.kind == "int")
        elif meta.kind == "date":
            if raw.strip():
                try:
                    fields[field] = date.fromisoformat(raw.strip())
                except ValueError:
                    raise _err(field, f"{meta.label}: Datum erwartet.") from None
            else:
                fields[field] = None
        elif meta.kind == "md":
            fields[field] = _parse_md(field, raw)
        elif field == "regionale_feiertage":
            fields[field] = [str(v) for v in form.getlist(field)]
        elif field in ("klassen", "faecher"):
            fields[field] = _split_list(raw)
        elif field == "zeitraster":
            fields[field] = _parse_zeitraster(form)
        elif field == "anrechnungs_grund":
            fields[field] = raw.strip() or None
        else:
            fields[field] = raw
    return fields


def _raw_values(section: str, form: FormData) -> dict[str, Any]:
    """The submitted values in display form (to re-render the form after an error)."""
    out: dict[str, Any] = {}
    for field in SECTIONS[section][1]:
        meta = FIELDS[field]
        if meta.kind == "bool":
            out[field] = form.get(field) in ("1", "on", "true")
        elif field == "regionale_feiertage":
            out[field] = [str(v) for v in form.getlist(field)]
        elif field == "zeitraster":
            out[field] = [
                {"slot_no": n, "label": lb, "start": s, "end": e}
                for n, lb, s, e in zip(form.getlist("zr_slot_no"), form.getlist("zr_label"),
                                       form.getlist("zr_start"), form.getlist("zr_end"), strict=False)
                if str(n).strip() or str(s).strip() or str(e).strip()
            ]
        elif meta.kind in ("int", "float"):
            # <input type="number"> only shows values with a decimal point ("20,5" would render empty).
            out[field] = str(form.get(field) or "").strip().replace(" ", "").replace(",", ".")
        else:
            out[field] = str(form.get(field) or "")
    return out


def _friendly(exc: svc.SettingsValidationError) -> str:
    meta = FIELDS.get(exc.field)
    if meta and exc.message.startswith(f"{exc.field}:"):
        return f"{meta.label}:{exc.message[len(exc.field) + 1:]}"
    return exc.message


# --------------------------------------------------------------------------- page


def _sensors_url() -> str:
    return f"{get_config().base_url}/api/sensors"


def _page(request: Request, db: Session, *, values: dict[str, Any] | None = None,
          errors: dict[str, str] | None = None, status_code: int = 200) -> HTMLResponse:
    settings = svc.get_settings(db)
    data = svc.settings_dict(settings)
    tree = cat_svc.list_tree(db, include_pseudo=False)
    zr = (values or {}).get("zeitraster")
    current = display_values(settings)
    if values:
        current.update(values)
    rows = list(zr if zr is not None else current["zeitraster"])
    ctx = {
        "page_title": "Einstellungen",
        "fields": FIELDS,
        "sections": SECTIONS,
        "v": current,
        "errors": errors or {},
        "derived": data,
        "regional_options": svc.REGIONAL_OPTIONS,
        "regional_hints": REGIONAL_HINTS,
        "zeitraster_rows": rows + [{"slot_no": "", "label": "", "start": "", "end": ""}] * ZEITRASTER_BLANK_ROWS,
        "next_slot_no": max([int(r["slot_no"]) for r in rows if str(r.get("slot_no", "")).strip().isdigit()] or [0]) + 1,
        "category_count": sum(1 for n in tree if n.category.active),
        "subcategory_count": sum(len(n.children) for n in tree),
        "token_configured": bool(settings.api_token_hash),
        "sensors_url": _sensors_url(),
        "has_closed_months": db.scalar(select(func.count()).select_from(MonthClose)) > 0,
    }
    return templates.TemplateResponse(request, "settings.html", ctx, status_code=status_code)


@router.get("/einstellungen", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _page(request, db)


# --------------------------------------------------------------------------- API token
# (declared before the generic section route so "/einstellungen/api-token" is not taken for a section)


@router.post("/einstellungen/api-token", response_class=HTMLResponse, include_in_schema=False)
def create_token(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    rotated = svc.api_token_configured(db)
    token = svc.generate_api_token(db)
    log.info("Sensor API token %s", "rotated" if rotated else "created")
    ctx = {
        "page_title": "Sensor-Token",
        "token": token,
        "rotated": rotated,
        "sensors_url": _sensors_url(),
        "has_closed_months": db.scalar(select(func.count()).select_from(MonthClose)) > 0,
    }
    return templates.TemplateResponse(request, "settings_token.html", ctx, headers={"Cache-Control": "no-store"})


@router.post("/einstellungen/api-token/loeschen", include_in_schema=False)
def delete_token(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    svc.clear_api_token(db)
    flash(request, "Sensor-Token gelöscht. /api/sensors ist jetzt nicht mehr erreichbar.", "success")
    return RedirectResponse("/einstellungen#sensor", status_code=303)


# --------------------------------------------------------------------------- categories


def _cat_redirect(anchor: str | int | None = None) -> RedirectResponse:
    return RedirectResponse("/einstellungen/kategorien" + (f"#kat-{anchor}" if anchor else ""), status_code=303)


@router.get("/einstellungen/kategorien", response_class=HTMLResponse)
def categories_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    tree = cat_svc.list_tree(db)
    ctx = {
        "page_title": "Kategorien",
        "tree": [n for n in tree if not n.category.is_pseudo],
        "pseudo": [n for n in tree if n.category.is_pseudo],
        "open_id": request.query_params.get("offen"),
    }
    return templates.TemplateResponse(request, "settings_categories.html", ctx)


@router.post("/einstellungen/kategorien/{category_id}", include_in_schema=False)
async def update_category(category_id: int, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = await request.form()
    try:
        cat = cat_svc.update_category(
            db, category_id,
            name=str(form.get("name") or ""),
            color=str(form.get("color") or "") or None,
            active=form.get("active") in ("1", "on"),
            requires_note=form.get("requires_note") in ("1", "on"),
        )
    except cat_svc.CategoryNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except cat_svc.CategoryError as exc:
        db.rollback()
        flash(request, str(exc), "danger")
        return _cat_redirect(category_id)
    flash(request, f"„{cat.name}“ gespeichert.", "success")
    return _cat_redirect(cat.parent_id or cat.id)


@router.post("/einstellungen/kategorien/{category_id}/verschieben", include_in_schema=False)
async def move_category(category_id: int, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = await request.form()
    try:
        cat = cat_svc.move_category(db, category_id, str(form.get("richtung") or ""))
    except cat_svc.CategoryNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except cat_svc.CategoryError as exc:
        db.rollback()
        flash(request, str(exc), "danger")
        return _cat_redirect()
    return _cat_redirect(cat.parent_id or cat.id)


@router.post("/einstellungen/kategorien/{category_id}/unterkategorie", include_in_schema=False)
async def add_subcategory(category_id: int, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = await request.form()
    try:
        child = cat_svc.add_subcategory(db, category_id, str(form.get("name") or ""),
                                        color=str(form.get("color") or "") or None)
    except cat_svc.CategoryNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except cat_svc.CategoryError as exc:
        db.rollback()
        flash(request, str(exc), "danger")
        return _cat_redirect(category_id)
    flash(request, f"Unterkategorie „{child.name}“ ({child.code}) angelegt.", "success")
    return _cat_redirect(category_id)


# --------------------------------------------------------------------------- sections


@router.post("/einstellungen/{section}", response_class=HTMLResponse, include_in_schema=False)
async def save_section(section: str, request: Request, db: Session = Depends(get_db)):
    if section not in SECTIONS:
        raise HTTPException(404, "Unbekannter Bereich.")
    form = await request.form()
    try:
        fields = parse_section(section, form)
        svc.update_settings(db, reason=str(form.get("grund") or "").strip() or None, **fields)
    except svc.SettingsValidationError as exc:
        db.rollback()
        field = exc.field if exc.field in FIELDS else SECTIONS[section][1][0]
        message = _friendly(exc)
        values = _raw_values(section, form)
        response = _page(request, db, values=values, errors={field: message, "_section": section},
                         status_code=422)
        return response
    except CalendarLocked as exc:
        values = _raw_values(section, form)
        return _page(request, db, values=values, errors={"grund": str(exc), "_section": section,
                                                         "_locked": section}, status_code=423)
    flash(request, f"{SECTIONS[section][0]} gespeichert.", "success")
    return RedirectResponse(f"/einstellungen#{section}", status_code=303)
