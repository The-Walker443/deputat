"""HTML – Screen 'Kalender' (Konzept §7.1, §6.2): periods, onboarding wizard, month view with day types,
day overrides (absences, päd. Tag, notes, manual leave), ferien CRUD + ICS import, holidays, leave plan.

Routes (mounted by app.main without prefix, no login; all POSTs are classic form posts with CSRF field
and Post/Redirect/Get + flash messages):

- ``GET  /kalender``                         month view (``?periode=<id>&monat=YYYY-MM``); wizard step 1
                                             when no period exists
- ``GET  /kalender/einrichtung``             onboarding wizard (``?schritt=1..4&periode=<id>``)
- ``POST /kalender/perioden``                create period (label like 2026/27 or 2027)
- ``POST /kalender/perioden/{id}/loeschen``  delete period (ferien + materialised days; entries stay)
- ``GET  /kalender/ferien``                  ferien list with CRUD + ICS upload
- ``POST /kalender/ferien``                  create ferien
- ``POST /kalender/ferien/{id}``             update ferien
- ``POST /kalender/ferien/{id}/loeschen``    delete ferien
- ``POST /kalender/ics/vorschau``            upload ICS -> preview page (nothing written)
- ``POST /kalender/ics/import``              commit the selected preview items
- ``GET  /kalender/tag/{YYYY-MM-DD}``        day editor (partial for HTMX, full page otherwise)
- ``POST /kalender/tag/{YYYY-MM-DD}``        save day override (``aktion=entfernen`` removes it)
- ``POST /kalender/zeitraum``                same absence/day type for a date range
- ``POST /kalender/feiertage``               regional holiday toggles (settings.regionale_feiertage)
- ``POST /kalender/urlaubsmodus``            leave mode (settings.urlaubs_modus)
- ``POST /kalender/urlaub/vorlage``          manual mode: copy an automatic plan as starting point

All calendar logic lives in :mod:`app.services.calendar_service`; this module only parses forms and renders.
"""

from __future__ import annotations

import base64
import binascii
import logging
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.types import AbsenceType, DayType, FerienKind, LeaveMode
from app.db.models import CalendarDay, Category, EntryStatus, Ferien, Period, PeriodKind, TimeEntry
from app.db.session import get_db
from app.security import safe_redirect_target
from app.services import calendar_service as cal
from app.services import settings_service as settings_svc
from app.util import today_local
from app.web.templating import MONTHS, flash, templates

log = logging.getLogger("app.web.calendar")

router = APIRouter()

DAY_TYPE_LABELS = {
    DayType.SCHULTAG.value: "Schultag",
    DayType.FERIENTAG.value: "Ferientag",
    DayType.UNTERRICHTSFREI.value: "Unterrichtsfrei",
    DayType.FEIERTAG.value: "Feiertag",
    DayType.WOCHENENDE.value: "Wochenende",
}
ABSENCE_LABELS = {
    AbsenceType.URLAUB.value: "Urlaub",
    AbsenceType.KRANK.value: "Krank",
    AbsenceType.FORTBILDUNG.value: "Fortbildung",
    AbsenceType.DIENSTBEFREIUNG.value: "Dienstbefreiung",
    AbsenceType.SONDERURLAUB.value: "Sonderurlaub",
    AbsenceType.ELTERNZEIT.value: "Elternzeit",
}
# Short marks in the month grid; deliberately not the category codes (U = Unterricht, K = Korrektur, ...).
ABSENCE_SHORT = {
    AbsenceType.URLAUB.value: "Url",
    AbsenceType.KRANK.value: "kr",
    AbsenceType.FORTBILDUNG.value: "Fb",
    AbsenceType.DIENSTBEFREIUNG.value: "Db",
    AbsenceType.SONDERURLAUB.value: "SU",
    AbsenceType.ELTERNZEIT.value: "EZ",
}
# Absences that may be set as override (Urlaub comes from the leave plan).
OVERRIDE_ABSENCE_CHOICES = [
    (a.value, ABSENCE_LABELS[a.value])
    for a in (AbsenceType.KRANK, AbsenceType.FORTBILDUNG, AbsenceType.DIENSTBEFREIUNG,
              AbsenceType.SONDERURLAUB, AbsenceType.ELTERNZEIT)
]
OVERRIDE_DAY_TYPE_CHOICES = [
    (DayType.UNTERRICHTSFREI.value, "Unterrichtsfrei (z. B. pädagogischer Tag)"),
    (DayType.SCHULTAG.value, "Schultag (z. B. Unterricht trotz Ferien)"),
    (DayType.FERIENTAG.value, "Ferientag"),
]
LEAVE_MODE_CHOICES = [
    (LeaveMode.SOMMERFERIEN_ZUERST.value, "Sommerferien zuerst (Standard)",
     "Die Urlaubstage liegen chronologisch in den Sommerferien; reicht das nicht, folgen Weihnachts-, "
     "Pfingst-, Oster-, Faschings- und Herbstferien."),
    (LeaveMode.PROPORTIONAL.value, "Proportional",
     "Die Urlaubstage werden anteilig nach Länge auf alle Ferienabschnitte verteilt."),
    (LeaveMode.MANUELL.value, "Manuell",
     "Du tippst die Urlaubstage selbst im Kalender an; die App zeigt „noch zuzuordnen: n“."),
]
FERIEN_KIND_CHOICES = [(k.value, cal.FERIEN_KIND_LABELS[k]) for k in FerienKind]
HOLIDAY_SCOPE_LABELS = {"gesetzlich": "gesetzlich", "regional": "regional", "unterrichtsfrei": "unterrichtsfrei"}
WEEKDAY_HEADERS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
WIZARD_STEPS = (
    (1, "Schuljahr"),
    (2, "Ferien"),
    (3, "Feiertage"),
    (4, "Urlaub"),
)
EXAMPLE_ICS_HINT = (
    "Bitte die Ferientermine nach dem Import immer mit den amtlichen Terminen des Bayerischen "
    "Kultusministeriums abgleichen – eine Datei mit „BEISPIEL“ im Namen enthält nur Beispieldaten."
)


# --------------------------------------------------------------------------- helpers


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    parts = text.replace(" ", "").split(".")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        try:
            year = int(parts[2])
            return date(year + 2000 if year < 100 else year, int(parts[1]), int(parts[0]))
        except ValueError:
            return None
    return None


def _parse_month(value: str | None) -> tuple[int, int] | None:
    if not value or len(value) != 7 or value[4] != "-":
        return None
    try:
        year, month = int(value[:4]), int(value[5:])
    except ValueError:
        return None
    return (year, month) if 1 <= month <= 12 and 1900 < year < 2200 else None


def _ym(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _safe_next(value: Any, default: str = "/kalender") -> str:
    """A same-site ``next`` target below /kalender or /einstellungen, else ``default``."""
    text = safe_redirect_target(str(value or "").strip())
    if text and text.startswith(("/kalender", "/einstellungen")) and "//" not in text:
        return text
    return default


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _period_or_none(db: Session, ref: str | None) -> Period | None:
    try:
        return cal.resolve_period(db, ref or None)
    except (cal.NoPeriodError, cal.NotFoundError):
        return None


def _next_period_label(db: Session, periods: list[Period]) -> str:
    settings = settings_svc.get_settings(db)
    if not periods:
        return cal.suggest_period_label(db)
    nxt = periods[-1].end + timedelta(days=1)
    if settings.kontoperiode == PeriodKind.KALENDERJAHR.value:
        return str(nxt.year)
    return cal.schuljahr_label(nxt)


def _month_label(year: int, month: int) -> str:
    return f"{MONTHS[month - 1]} {year}"


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _ferien_names(db: Session, start: date, end: date) -> dict[str, Ferien]:
    from sqlalchemy import select

    rows = db.scalars(select(Ferien).where(Ferien.start <= end, Ferien.end >= start))
    return {f.id: f for f in rows}


@dataclass
class DayCell:
    date: date
    in_month: bool
    in_period: bool
    is_today: bool
    day_type: str | None = None
    absence: str | None = None
    holiday_name: str | None = None
    ferien_name: str | None = None
    note: str | None = None
    soll_seconds: int = 0
    has_override: bool = False
    urlaub_manuell: bool = False

    @property
    def type_label(self) -> str:
        return DAY_TYPE_LABELS.get(self.day_type or "", "")

    @property
    def absence_label(self) -> str:
        return ABSENCE_LABELS.get(self.absence or "", "")

    @property
    def absence_short(self) -> str:
        return ABSENCE_SHORT.get(self.absence or "", "")

    @property
    def title(self) -> str:
        parts = [self.date.strftime("%d.%m.%Y")]
        if self.holiday_name:
            parts.append(self.holiday_name)
        elif self.type_label:
            parts.append(self.type_label)
        if self.ferien_name and not self.holiday_name:
            parts.append(self.ferien_name)
        if self.absence_label:
            parts.append(self.absence_label)
        if self.note:
            parts.append(f"Notiz: {self.note}")
        return " · ".join(parts)


@dataclass
class MonthView:
    year: int
    month: int
    label: str
    weeks: list[list[DayCell]]
    events: list[dict[str, Any]] = field(default_factory=list)
    prev: str | None = None
    next: str | None = None


def _month_view(db: Session, period: Period, year: int, month: int, today: date) -> MonthView:
    first = date(year, month, 1)
    last = date(year, month, monthrange(year, month)[1])
    grid_start = first - timedelta(days=first.weekday())
    grid_end = last + timedelta(days=6 - last.weekday())
    rows: dict[date, CalendarDay] = {r.date: r for r in cal.list_calendar_days(db, grid_start, grid_end)}
    overrides = {o.date: o for o in cal.list_overrides(db, grid_start, grid_end)}
    ferien = _ferien_names(db, grid_start, grid_end)

    weeks: list[list[DayCell]] = []
    d = grid_start
    while d <= grid_end:
        week: list[DayCell] = []
        for _ in range(7):
            row = rows.get(d)
            ov = overrides.get(d)
            cell = DayCell(
                date=d,
                in_month=d.month == month,
                in_period=row is not None and period.start <= d <= period.end,
                is_today=d == today,
            )
            if row is not None:
                cell.day_type = row.day_type
                cell.absence = row.absence_type
                cell.holiday_name = row.holiday_name
                cell.soll_seconds = int(row.soll_seconds or 0)
                cell.note = row.note
                if row.ferien_id and row.ferien_id in ferien:
                    cell.ferien_name = ferien[row.ferien_id].name
            if ov is not None:
                cell.has_override = True
                cell.urlaub_manuell = bool(ov.urlaub_manuell)
                cell.note = ov.note or cell.note
            week.append(cell)
            d += timedelta(days=1)
        weeks.append(week)

    events: list[dict[str, Any]] = []
    for f in sorted(ferien.values(), key=lambda f: f.start):
        if f.start <= last and f.end >= first:
            events.append({"kind": "ferien", "date": f.start, "end": f.end, "text": f.name})
    for cell in (c for w in weeks for c in w if c.in_month):
        if cell.holiday_name:
            events.append({"kind": cell.day_type or "feiertag", "date": cell.date, "end": None,
                           "text": cell.holiday_name})
        if cell.absence and cell.absence != AbsenceType.URLAUB.value:
            events.append({"kind": "absence", "date": cell.date, "end": None, "text": cell.absence_label})
        if cell.has_override and cell.day_type == DayType.UNTERRICHTSFREI.value and not cell.holiday_name:
            events.append({"kind": "unterrichtsfrei", "date": cell.date, "end": None, "text": "Unterrichtsfrei"})
        if cell.note:
            events.append({"kind": "note", "date": cell.date, "end": None, "text": cell.note})
    urlaub = [c.date for w in weeks for c in w if c.in_month and c.absence == AbsenceType.URLAUB.value]
    if urlaub:
        events.append({"kind": "urlaub", "date": urlaub[0], "end": urlaub[-1] if len(urlaub) > 1 else None,
                       "text": f"{len(urlaub)} Urlaubstag(e)"})
    events.sort(key=lambda e: (e["date"], e["kind"] != "ferien"))

    view = MonthView(year, month, _month_label(year, month), weeks, events)
    py, pm = _shift_month(year, month, -1)
    ny, nm = _shift_month(year, month, 1)
    if date(py, pm, monthrange(py, pm)[1]) >= period.start:
        view.prev = f"{py:04d}-{pm:02d}"
    if date(ny, nm, 1) <= period.end:
        view.next = f"{ny:04d}-{nm:02d}"
    return view


def _leave_info(db: Session, period: Period) -> cal.LeavePlanInfo | None:
    try:
        return cal.leave_plan_info(db, period)
    except Exception:  # never break the page because of the leave plan
        log.exception("Urlaubszuordnung konnte nicht berechnet werden")
        db.rollback()
        return None


def _leave_compare(db: Session, period: Period) -> list[dict[str, Any]] | None:
    try:
        return cal.compare_leave_modes(db, period)
    except Exception:  # ledger may be incomplete: show the page without the comparison
        log.exception("Vergleich der Urlaubsmodi fehlgeschlagen")
        db.rollback()
        return None


def _base_ctx(db: Session, period: Period | None) -> dict[str, Any]:
    settings = settings_svc.get_settings(db)
    periods = cal.list_periods(db)
    return {
        "periods": periods,
        "period": period,
        "settings": settings,
        "manual_mode": settings.urlaubs_modus == LeaveMode.MANUELL.value,
        "leave_mode_label": cal.LEAVE_MODE_LABELS.get(LeaveMode(settings.urlaubs_modus), settings.urlaubs_modus),
        "next_period_label": _next_period_label(db, periods),
        "day_type_labels": DAY_TYPE_LABELS,
        "absence_labels": ABSENCE_LABELS,
        "ferien_kind_choices": FERIEN_KIND_CHOICES,
        "ferien_kind_labels": {k: v for k, v in FERIEN_KIND_CHOICES},
        "example_ics_hint": EXAMPLE_ICS_HINT,
    }


# --------------------------------------------------------------------------- main page


@router.get("/kalender", response_class=HTMLResponse)
def calendar_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    q = request.query_params
    period = _period_or_none(db, q.get("periode"))
    if period is None:
        if q.get("periode") and cal.list_periods(db):
            raise HTTPException(404, "Schuljahr nicht gefunden.")
        return _wizard(request, db, None, 1)
    today = today_local()
    ym = _parse_month(q.get("monat"))
    if ym is None or not (period.start <= date(ym[0], ym[1], 1) <= period.end
                          or period.start <= date(ym[0], ym[1], monthrange(*ym)[1]) <= period.end):
        anchor = today if period.start <= today <= period.end else period.start
        ym = (anchor.year, anchor.month)
    month = _month_view(db, period, ym[0], ym[1], today)
    ctx = _base_ctx(db, period)
    ctx.update(
        page_title="Kalender",
        month=month,
        weekday_headers=WEEKDAY_HEADERS,
        leave=_leave_info(db, period),
        compare=_leave_compare(db, period),
        ferien=cal.list_ferien(db, period),
        absence_choices=OVERRIDE_ABSENCE_CHOICES,
        day_type_choices=OVERRIDE_DAY_TYPE_CHOICES,
        selected_day=_parse_date(q.get("tag")),
    )
    return templates.TemplateResponse(request, "calendar.html", ctx)


# --------------------------------------------------------------------------- wizard


def _wizard(request: Request, db: Session, period: Period | None, step: int) -> HTMLResponse:
    if period is None:
        step = 1
    ctx = _base_ctx(db, period)
    ctx.update(
        page_title="Kalender einrichten",
        step=step,
        steps=WIZARD_STEPS,
        suggested_label=_next_period_label(db, ctx["periods"]),
        regional_options=settings_svc.REGIONAL_OPTIONS,
        leave_mode_choices=LEAVE_MODE_CHOICES,
        holiday_scope_labels=HOLIDAY_SCOPE_LABELS,
    )
    if period is not None:
        ctx["wizard_url"] = f"/kalender/einrichtung?periode={period.id}"
        ctx["ferien"] = cal.list_ferien(db, period)
        if step == 3:
            ctx["holidays"] = cal.list_holidays(db, period)
        if step == 4:
            ctx["leave"] = _leave_info(db, period)
            ctx["compare"] = _leave_compare(db, period)
            today = today_local()
            ctx["erfassung_ab"] = settings_svc.get_settings(db).erfassung_ab
            ctx["erfassung_vorschlag"] = today if period.start < today <= period.end else None
    return templates.TemplateResponse(request, "calendar_wizard.html", ctx)


@router.get("/kalender/einrichtung", response_class=HTMLResponse)
def wizard_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    q = request.query_params
    try:
        step = max(1, min(4, int(q.get("schritt") or 1)))
    except ValueError:
        step = 1
    period = None
    if q.get("periode"):
        try:
            period = cal.get_period(db, q["periode"])
        except cal.NotFoundError:
            raise HTTPException(404, "Schuljahr nicht gefunden.") from None
    elif step > 1:
        period = _period_or_none(db, None)
    return _wizard(request, db, period, step)


# --------------------------------------------------------------------------- periods


@router.post("/kalender/perioden", include_in_schema=False)
async def create_period(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = await request.form()
    label = str(form.get("label") or "").strip()
    wizard = form.get("wizard") == "1"
    start, end = _parse_date(form.get("start")), _parse_date(form.get("end"))
    try:
        if start or end:
            period = cal.create_period(db, label or None, start=start, end=end)
        else:
            period = cal.create_period(db, label or None)
    except cal.CalendarError as exc:
        db.rollback()
        flash(request, str(exc), "danger")
        return _redirect("/kalender/einrichtung" if wizard else "/kalender")
    flash(request, f"„{period.label}“ angelegt ({cal.fmt_date_de(period.start)}–{cal.fmt_date_de(period.end)}).",
          "success")
    if wizard:
        return _redirect(f"/kalender/einrichtung?periode={period.id}&schritt=2")
    return _redirect(f"/kalender?periode={period.id}")


@router.post("/kalender/perioden/{period_id}/loeschen", include_in_schema=False)
def delete_period(period_id: str, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    try:
        period = cal.get_period(db, period_id)
        label = period.label
        cal.delete_period(db, period_id)
    except cal.NotFoundError as exc:
        flash(request, str(exc), "danger")
        return _redirect("/kalender")
    flash(request, f"„{label}“ wurde gelöscht. Zeiteinträge bleiben erhalten.", "success")
    return _redirect("/kalender")


# --------------------------------------------------------------------------- ferien


@router.get("/kalender/ferien", response_class=HTMLResponse)
def ferien_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    period = _period_or_none(db, request.query_params.get("periode"))
    if period is None:
        return _redirect("/kalender")  # type: ignore[return-value]
    ctx = _base_ctx(db, period)
    ferien = cal.list_ferien(db, period)
    holidays = {h.date: (h.name, h.scope) for h in cal.list_holidays(db, period)}
    ctx.update(
        page_title="Ferien",
        ferien=ferien,
        werktage={f.id: cal.werktage(f.start, f.end, holidays) for f in ferien},
        edit_id=request.query_params.get("bearbeiten"),
    )
    return templates.TemplateResponse(request, "calendar_ferien.html", ctx)


def _ferien_next(form: Any, period: Period) -> str:
    return _safe_next(form.get("next"), f"/kalender/ferien?periode={period.id}")


@router.post("/kalender/ferien", include_in_schema=False)
async def create_ferien(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = await request.form()
    period = _period_or_none(db, str(form.get("periode") or "") or None)
    if period is None:
        flash(request, cal.NO_PERIOD_HINT, "danger")
        return _redirect("/kalender")
    target = _ferien_next(form, period)
    start, end = _parse_date(form.get("start")), _parse_date(form.get("end"))
    if start is None or end is None:
        flash(request, "Bitte Beginn und Ende der Ferien angeben.", "danger")
        return _redirect(target)
    try:
        row = cal.create_ferien(
            db, period, name=str(form.get("name") or ""), start=start, end=end,
            kind=str(form.get("kind") or "") or None, quelle_url=str(form.get("quelle_url") or "").strip() or None,
        )
    except cal.CalendarError as exc:
        db.rollback()
        flash(request, str(exc), "danger")
        return _redirect(target)
    flash(request, f"„{row.name}“ angelegt ({cal.fmt_date_de(row.start)}–{cal.fmt_date_de(row.end)}).", "success")
    return _redirect(target)


@router.post("/kalender/ferien/{ferien_id}", include_in_schema=False)
async def update_ferien(ferien_id: str, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = await request.form()
    try:
        row = cal.get_ferien(db, ferien_id)
    except cal.NotFoundError as exc:
        flash(request, str(exc), "danger")
        return _redirect("/kalender/ferien")
    target = _ferien_next(form, row.period)
    start, end = _parse_date(form.get("start")), _parse_date(form.get("end"))
    if start is None or end is None:
        flash(request, "Bitte Beginn und Ende der Ferien angeben.", "danger")
        return _redirect(target)
    try:
        cal.update_ferien(
            db, ferien_id, name=str(form.get("name") or ""), start=start, end=end,
            kind=str(form.get("kind") or "") or None, quelle_url=str(form.get("quelle_url") or "").strip() or None,
        )
    except cal.CalendarError as exc:
        db.rollback()
        flash(request, str(exc), "danger")
        return _redirect(target)
    flash(request, "Ferien gespeichert.", "success")
    return _redirect(target)


@router.post("/kalender/ferien/{ferien_id}/loeschen", include_in_schema=False)
async def delete_ferien(ferien_id: str, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = await request.form()
    try:
        row = cal.get_ferien(db, ferien_id)
    except cal.NotFoundError as exc:
        flash(request, str(exc), "danger")
        return _redirect("/kalender/ferien")
    period, name = row.period, row.name
    cal.delete_ferien(db, ferien_id)
    flash(request, f"„{name}“ gelöscht.", "success")
    return _redirect(_ferien_next(form, period))


# --------------------------------------------------------------------------- ICS import


@router.post("/kalender/ics/vorschau", response_class=HTMLResponse, include_in_schema=False)
async def ics_preview(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    form = await request.form()
    period = _period_or_none(db, str(form.get("periode") or "") or None)
    wizard = form.get("wizard") == "1"
    if period is None:
        flash(request, cal.NO_PERIOD_HINT, "danger")
        return _redirect("/kalender")  # type: ignore[return-value]
    back = (f"/kalender/einrichtung?periode={period.id}&schritt=2" if wizard
            else f"/kalender/ferien?periode={period.id}")
    upload = form.get("datei")
    content: bytes = b""
    if upload is not None and hasattr(upload, "read"):
        content = await upload.read(cal.MAX_ICS_BYTES + 1)
        filename = getattr(upload, "filename", "") or ""
    else:
        filename = ""
    if not content:
        flash(request, "Bitte eine ICS-Datei auswählen.", "danger")
        return _redirect(back)  # type: ignore[return-value]
    replace = form.get("ersetzen") == "1"
    try:
        preview = cal.preview_ics(db, period, content, replace=replace)
    except cal.CalendarError as exc:
        flash(request, str(exc), "danger")
        return _redirect(back)  # type: ignore[return-value]
    is_example = "BEISPIEL" in content.decode("utf-8", "replace").upper() or "beispiel" in filename.lower()
    ctx = _base_ctx(db, period)
    ctx.update(
        page_title="Ferien importieren",
        preview=preview,
        content_b64=base64.b64encode(content).decode("ascii"),
        filename=filename,
        replace=replace,
        wizard=wizard,
        back=back,
        is_example=is_example,
        quelle_url=str(form.get("quelle_url") or "").strip(),
    )
    return templates.TemplateResponse(request, "calendar_ics_preview.html", ctx)


@router.post("/kalender/ics/import", include_in_schema=False)
async def ics_commit(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = await request.form()
    period = _period_or_none(db, str(form.get("periode") or "") or None)
    wizard = form.get("wizard") == "1"
    if period is None:
        flash(request, cal.NO_PERIOD_HINT, "danger")
        return _redirect("/kalender")
    back = (f"/kalender/einrichtung?periode={period.id}&schritt=2" if wizard
            else f"/kalender/ferien?periode={period.id}")
    try:
        content = base64.b64decode(str(form.get("inhalt") or ""), validate=True)
    except (binascii.Error, ValueError):
        content = b""
    if not content:
        flash(request, "Die Importdaten fehlen – bitte die Datei erneut hochladen.", "danger")
        return _redirect(back)
    selected: list[int] = []
    for raw in form.getlist("auswahl"):
        try:
            selected.append(int(str(raw)))
        except ValueError:
            continue
    if not selected:
        flash(request, "Keine Termine ausgewählt – es wurde nichts importiert.", "warn")
        return _redirect(back)
    try:
        result = cal.commit_ics(
            db, period, content, selected=selected, replace=form.get("ersetzen") == "1",
            quelle_url=str(form.get("quelle_url") or "").strip() or None,
        )
    except cal.CalendarError as exc:
        db.rollback()
        flash(request, str(exc), "danger")
        return _redirect(back)
    message = f"{len(result.created)} Ferienabschnitt(e) importiert."
    if result.replaced:
        message += f" Ersetzt: {', '.join(result.replaced)}."
    flash(request, message, "success")
    for _, reason in result.skipped:
        flash(request, f"Übersprungen: {reason}", "warn")
    return _redirect(back)


# --------------------------------------------------------------------------- day override


def _day_ctx(db: Session, d: date) -> dict[str, Any]:
    period = cal.period_for_date(db, d)
    row = db.get(CalendarDay, d)
    override = cal.get_override(db, d)
    ctx = _base_ctx(db, period)
    ferien_name = None
    if row is not None and row.ferien_id:
        f = db.get(Ferien, row.ferien_id)
        ferien_name = f.name if f else None
    is_candidate = bool(
        row is not None
        and row.day_type in (DayType.FERIENTAG.value, DayType.UNTERRICHTSFREI.value)
        and d.isoweekday() <= 5
        and row.absence_type in (None, AbsenceType.URLAUB.value)
    )
    f_cat = db.scalars(select(Category).where(Category.code == "F", Category.parent_id.is_(None))).first()
    f_seconds = 0
    if f_cat is not None:
        f_seconds = int(db.scalar(
            select(func.coalesce(func.sum(TimeEntry.duration_seconds), 0)).where(
                TimeEntry.day == d, TimeEntry.deleted_at.is_(None), TimeEntry.status == EntryStatus.ACTIVE.value,
                TimeEntry.category_id == f_cat.id,
            )
        ) or 0)
    ctx.update(
        day=d,
        row=row,
        override=override,
        ferien_name=ferien_name,
        fortbildung_category_id=f_cat.id if f_cat is not None else None,
        fortbildung_seconds=f_seconds,
        is_leave_candidate=is_candidate,
        absence_choices=OVERRIDE_ABSENCE_CHOICES,
        day_type_choices=OVERRIDE_DAY_TYPE_CHOICES,
        leave=_leave_info(db, period) if period is not None and ctx["manual_mode"] else None,
        back=f"/kalender?periode={period.id}&monat={_ym(d)}" if period else "/kalender",
    )
    return ctx


@router.get("/kalender/tag/{day}", response_class=HTMLResponse)
def day_editor(day: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    d = _parse_date(day)
    if d is None:
        raise HTTPException(404, "Ungültiges Datum.")
    ctx = _day_ctx(db, d)
    if _is_htmx(request):
        return templates.TemplateResponse(request, "partials/calendar_day_form.html", ctx)
    ctx["page_title"] = f"Tag {d.strftime('%d.%m.%Y')}"
    return templates.TemplateResponse(request, "calendar_day.html", ctx)


@router.post("/kalender/tag/{day}", include_in_schema=False)
async def save_day(day: str, request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    d = _parse_date(day)
    if d is None:
        raise HTTPException(404, "Ungültiges Datum.")
    form = await request.form()
    period = cal.period_for_date(db, d)
    back = f"/kalender?periode={period.id}&monat={_ym(d)}" if period else "/kalender"
    try:
        if form.get("aktion") == "entfernen":
            if cal.get_override(db, d) is not None:
                cal.delete_override(db, d)
            flash(request, f"Eingaben für den {cal.fmt_date_de(d)} entfernt.", "success")
            return _redirect(back)
        cal.set_override(
            db, d,
            absence_type=str(form.get("absence_type") or "") or None,
            day_type=str(form.get("day_type") or "") or None,
            urlaub_manuell=form.get("urlaub_manuell") == "1",
            note=str(form.get("note") or ""),
        )
    except (cal.CalendarError, cal.NotFoundError) as exc:
        db.rollback()
        flash(request, str(exc), "danger")
        return _redirect(f"/kalender/tag/{d.isoformat()}")
    flash(request, f"{cal.fmt_date_de(d)} gespeichert.", "success")
    if str(form.get("absence_type") or "") == AbsenceType.FORTBILDUNG.value:
        flash(request, "Fortbildung: Das Soll bleibt. Bitte die Fortbildungszeit zusätzlich als Eintrag "
                       "„Fortbildung“ (F) erfassen – sonst zählt der Tag mit 0 Stunden.", "warn")
        return _redirect(f"/kalender/tag/{d.isoformat()}")
    if period is not None and settings_svc.get_settings(db).urlaubs_modus == LeaveMode.MANUELL.value:
        info = _leave_info(db, period)
        if info is not None and info.excess:
            flash(request, f"Achtung: {info.excess} Urlaubstag(e) zu viel ausgewählt.", "warn")
    return _redirect(back)


@router.post("/kalender/zeitraum", include_in_schema=False)
async def save_range(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = await request.form()
    start, end = _parse_date(form.get("start")), _parse_date(form.get("end"))
    target = _safe_next(form.get("next"))
    if start is None or end is None:
        flash(request, "Bitte Beginn und Ende des Zeitraums angeben.", "danger")
        return _redirect(target)
    try:
        rows = cal.set_override_range(
            db, start, end,
            absence_type=str(form.get("absence_type") or "") or None,
            day_type=str(form.get("day_type") or "") or None,
            note=str(form.get("note") or ""),
            weekdays_only=form.get("auch_wochenende") != "1",
        )
    except cal.CalendarError as exc:
        db.rollback()
        flash(request, str(exc), "danger")
        return _redirect(target)
    if rows:
        flash(request, f"{len(rows)} Tag(e) eingetragen ({cal.fmt_date_de(start)}–{cal.fmt_date_de(end)}).", "success")
    else:
        flash(request, f"Eingaben im Zeitraum {cal.fmt_date_de(start)}–{cal.fmt_date_de(end)} entfernt.", "success")
    return _redirect(target)


# --------------------------------------------------------------------------- settings shortcuts


@router.post("/kalender/feiertage", include_in_schema=False)
async def save_regional_holidays(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = await request.form()
    selected = [str(v) for v in form.getlist("regionale_feiertage")]
    target = _safe_next(form.get("next"))
    try:
        settings_svc.update_settings(db, regionale_feiertage=selected,
                                     reason=str(form.get("grund") or "").strip() or None)
    except settings_svc.SettingsValidationError as exc:
        db.rollback()
        flash(request, exc.message, "danger")
        return _redirect(target)
    except cal.CalendarLocked as exc:
        flash(request, f"{exc} Bitte unter Einstellungen → Regionale Feiertage mit Begründung ändern.", "danger")
        return _redirect(target)
    flash(request, "Regionale Feiertage gespeichert.", "success")
    return _redirect(target)


@router.post("/kalender/erfassung-ab", include_in_schema=False)
async def save_recording_start(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    """Wizard shortcut for ``settings.erfassung_ab`` (days before it are neutral in the account)."""
    form = await request.form()
    target = _safe_next(form.get("next"))
    raw = str(form.get("erfassung_ab") or "").strip()
    value = _parse_date(raw) if raw else None
    if raw and value is None:
        flash(request, "Bitte ein gültiges Datum angeben.", "danger")
        return _redirect(target)
    try:
        settings_svc.update_settings(db, erfassung_ab=value, reason=str(form.get("grund") or "").strip() or None)
    except settings_svc.SettingsValidationError as exc:
        db.rollback()
        flash(request, exc.message, "danger")
        return _redirect(target)
    except cal.CalendarLocked as exc:
        flash(request, f"{exc} Bitte unter Einstellungen → Arbeitszeitkonto mit Begründung ändern.", "danger")
        return _redirect(target)
    if value is None:
        flash(request, "Erfassung ab Periodenbeginn gespeichert.", "success")
    else:
        flash(request, f"Erfassung ab {cal.fmt_date_de(value)} gespeichert – frühere Tage zählen neutral.", "success")
    return _redirect(target)


@router.post("/kalender/urlaubsmodus", include_in_schema=False)
async def save_leave_mode(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = await request.form()
    target = _safe_next(form.get("next"))
    try:
        settings_svc.update_settings(db, urlaubs_modus=str(form.get("urlaubs_modus") or ""),
                                     reason=str(form.get("grund") or "").strip() or None)
    except settings_svc.SettingsValidationError as exc:
        db.rollback()
        flash(request, exc.message, "danger")
        return _redirect(target)
    except cal.CalendarLocked as exc:
        flash(request, f"{exc} Bitte unter Einstellungen → Arbeitszeitkonto mit Begründung ändern.", "danger")
        return _redirect(target)
    flash(request, "Urlaubsmodus gespeichert.", "success")
    return _redirect(target)


@router.post("/kalender/urlaub/vorlage", include_in_schema=False)
async def adopt_leave(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    form = await request.form()
    period = _period_or_none(db, str(form.get("periode") or "") or None)
    target = _safe_next(form.get("next"))
    if period is None:
        flash(request, cal.NO_PERIOD_HINT, "danger")
        return _redirect("/kalender")
    try:
        info = cal.adopt_leave_plan(db, period, str(form.get("vorlage") or ""))
    except (cal.CalendarError, ValueError) as exc:
        db.rollback()
        flash(request, str(exc) or "Ungültige Vorlage.", "danger")
        return _redirect(target)
    flash(request, f"{info.assigned} Urlaubstage als Vorlage übernommen – jetzt im Kalender anpassen.", "success")
    return _redirect(target)
