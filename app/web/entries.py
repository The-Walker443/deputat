"""HTML – Screen 'Einträge': list/filter entries, manual entry form, edit, delete/restore, revision history
(Konzept §4, §8.2, §8.3, §8.5).

Routes (mounted by app.main without prefix; no login, CSRF via the global dependency):

- ``GET  /eintraege``                       list grouped by day (filters: von, bis, kategorie, quelle,
                                             kennzeichen, geloescht; ``seite`` = number of day pages shown).
                                             HTMX requests (filter form) get only the results partial.
- ``GET  /eintraege/mehr``                  HTMX: the next page of day groups (+ new "mehr laden" trigger)
- ``GET  /eintraege/neu``                   manual entry form (prefill: datum, von, bis, kategorie, next)
- ``POST /eintraege/neu``                   create via ``services.entries.create_entry`` (PRG + flash)
- ``GET  /eintraege/{id}``                  edit form + lock notice + delete + revision history
- ``POST /eintraege/{id}``                  update via ``services.entries.update_entry`` (only changed fields)
- ``POST /eintraege/{id}/loeschen``         soft delete (reason required when locked)
- ``POST /eintraege/{id}/wiederherstellen`` restore a soft-deleted entry

All writes go through ``app.services.entries``; this module only reads the DB directly (categories,
settings, closed months) to build pickers.
"""

from __future__ import annotations

import calendar as _calendar
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timesplit import is_nachtraeglich
from app.db.models import Category, EntrySource, EntryStatus, MonthClose, Settings, TimeEntry
from app.db.session import get_db
from app.security import safe_redirect_target
from app.services import entries as svc
from app.util import fmt_hm, local_to_utc, now_utc, to_local, today_local
from app.web.templating import flash, templates

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]

PAGE_DAYS = 10  # day groups per page in the list

# --------------------------------------------------------------------------- labels

FLAG_LABELS: dict[str, str] = {
    "nachtraeglich": "nachträglich",
    "korrigiert": "korrigiert",
    "ohne_zeitgrenzen": "ohne Zeitgrenzen",
    "vertretung_angeordnet": "Vertretung angeordnet",
    "vertretung_genehmigt": "Vertretung genehmigt",
    "klassenfahrt": "Klassenfahrt",
    "nachtbereitschaft": "Nachtbereitschaft",
    "ferienarbeit": "Ferienarbeit",
    "wochenende": "Wochenende",
    "feiertag": "Feiertag",
}
# Badge colour per flag (app.css badge variants); missing -> neutral.
FLAG_BADGE: dict[str, str] = {
    "nachtraeglich": "badge-warn",
    "korrigiert": "badge-warn",
    "ohne_zeitgrenzen": "badge-outline",
    "vertretung_angeordnet": "badge-primary",
    "vertretung_genehmigt": "badge-primary",
    "klassenfahrt": "badge-success",
    "nachtbereitschaft": "badge-success",
}
# Flags shown as badges in the list (derived calendar flags stay quiet: the day header says it).
LIST_BADGE_FLAGS = ("nachtraeglich", "korrigiert", "ohne_zeitgrenzen", "vertretung_angeordnet",
                    "vertretung_genehmigt", "klassenfahrt", "nachtbereitschaft")

# Filter "Kennzeichen": real flags plus two pseudo values.
FILTER_FLAGS: tuple[tuple[str, str], ...] = (
    ("nachtraeglich", "nachträglich"),
    ("korrigiert", "korrigiert"),
    ("ohne_zeitgrenzen", "ohne Zeitgrenzen"),
    ("vertretung", "Vertretung (angeordnet/genehmigt)"),
    ("klassenfahrt", "Klassenfahrt"),
    ("nachtbereitschaft", "Nachtbereitschaft"),
    ("ferienarbeit", "Ferienarbeit"),
    ("wochenende", "Wochenende"),
    ("feiertag", "Feiertag"),
    ("gesperrt", "gesperrt (Monat abgeschlossen)"),
)
FILTER_FLAG_KEYS = frozenset(k for k, _ in FILTER_FLAGS)

SOURCE_LABELS: dict[str, str] = {
    EntrySource.TIMER.value: "Timer",
    EntrySource.MANUELL.value: "manuell",
    EntrySource.STUNDENPLAN.value: "Stundenplan",
    EntrySource.IMPORT.value: "Import",
}
LOCATION_LABELS: dict[str, str] = {
    "schule": "Schule",
    "zuhause": "Zuhause",
    "unterwegs": "Unterwegs",
    "extern": "Extern",
}
STATUS_LABELS: dict[str, str] = {"active": "aktiv", "vorschlag": "Vorschlag", "verworfen": "verworfen"}

# User-editable flags in the form (sticky/derived flags are set by the service).
FORM_FLAGS_U = ("vertretung_angeordnet", "vertretung_genehmigt")
FORM_FLAGS_ALL = ("klassenfahrt", "nachtbereitschaft")
FORM_FLAGS = FORM_FLAGS_U + FORM_FLAGS_ALL

NOTE_PLACEHOLDER = "Keine Namen von Kindern oder Eltern – z. B. ‚Gespräch Übertritt, 4a‘"

TIME_FIELDS = ("datum", "modus", "von", "bis", "folgetag", "dauer")


# --------------------------------------------------------------------------- small read helpers


def _settings(db: Session) -> Settings | None:
    return db.get(Settings, 1)


def _categories(db: Session) -> list[Category]:
    return list(db.scalars(select(Category).order_by(Category.sort, Category.id)))


@dataclass
class CategoryTree:
    by_id: dict[int, Category]
    mains: list[Category]
    children: dict[int, list[Category]]

    def label(self, cat_id: int | None, sub_id: int | None = None) -> str:
        cat = self.by_id.get(cat_id) if cat_id is not None else None
        sub = self.by_id.get(sub_id) if sub_id is not None else None
        if cat is None:
            return "?"
        return f"{cat.code}/{sub.name}" if sub is not None else cat.code


def _category_tree(db: Session, *, keep: Iterable[int | None] = ()) -> CategoryTree:
    """All categories; for pickers inactive ones are hidden except ids in ``keep`` (the entry's own)."""
    cats = _categories(db)
    keep_ids = {k for k in keep if k is not None}
    by_id = {c.id: c for c in cats}
    mains = [c for c in cats if c.parent_id is None and (c.active or c.id in keep_ids)]
    children: dict[int, list[Category]] = {}
    for c in cats:
        if c.parent_id is not None and (c.active or c.id in keep_ids):
            children.setdefault(c.parent_id, []).append(c)
    return CategoryTree(by_id=by_id, mains=mains, children=children)


def _closed_months(db: Session) -> list[str]:
    return list(db.scalars(select(MonthClose.year_month).order_by(MonthClose.year_month)))


def _month_range(d: date) -> tuple[date, date]:
    last = _calendar.monthrange(d.year, d.month)[1]
    return d.replace(day=1), d.replace(day=last)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _parse_time(value: str | None) -> time | None:
    if not value:
        return None
    text = value.strip()
    try:
        return time.fromisoformat(text if len(text) != 4 else "0" + text)
    except ValueError:
        return None


def _safe_next(value: str | None) -> str | None:
    """Only same-site relative paths are accepted as redirect target (see ``safe_redirect_target``)."""
    return safe_redirect_target(value)


def _wants_partial(request: Request) -> bool:
    return bool(request.headers.get("hx-request")) and not request.headers.get("hx-history-restore-request")


def _dt_local_str(iso_or_dt: Any, fmt: str) -> str:
    if iso_or_dt is None or iso_or_dt == "":
        return ""
    dt = datetime.fromisoformat(iso_or_dt) if isinstance(iso_or_dt, str) else iso_or_dt
    return to_local(dt).strftime(fmt)


# --------------------------------------------------------------------------- list


@dataclass
class ListFilters:
    von: date
    bis: date
    kategorie: int | None = None
    quelle: str | None = None
    kennzeichen: str | None = None
    geloescht: bool = False
    seite: int = 1

    def query(self, **override: Any) -> str:
        params: dict[str, Any] = {"von": self.von.isoformat(), "bis": self.bis.isoformat()}
        if self.kategorie is not None:
            params["kategorie"] = self.kategorie
        if self.quelle:
            params["quelle"] = self.quelle
        if self.kennzeichen:
            params["kennzeichen"] = self.kennzeichen
        if self.geloescht:
            params["geloescht"] = "1"
        params.update({k: v for k, v in override.items() if v is not None})
        return urlencode(params)

    @property
    def is_full_month(self) -> bool:
        first, last = _month_range(self.von)
        return self.von == first and self.bis == last

    def month_shift(self, months: int) -> str:
        y, m = self.von.year, self.von.month + months
        y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
        first, last = _month_range(date(y, m, 1))
        return self.query(von=first.isoformat(), bis=last.isoformat())

    @property
    def active_count(self) -> int:
        return sum(1 for v in (self.kategorie, self.quelle, self.kennzeichen) if v) + int(self.geloescht)


def _filters_from_query(request: Request) -> ListFilters:
    q = request.query_params
    default_from, default_to = _month_range(today_local())
    von = _parse_date(q.get("von")) or default_from
    bis = _parse_date(q.get("bis")) or (default_to if not q.get("von") else _month_range(von)[1])
    if bis < von:
        von, bis = bis, von
    try:
        kategorie = int(q["kategorie"]) if q.get("kategorie") else None
    except ValueError:
        kategorie = None
    quelle = q.get("quelle") if q.get("quelle") in SOURCE_LABELS else None
    kennzeichen = q.get("kennzeichen") if q.get("kennzeichen") in FILTER_FLAG_KEYS else None
    try:
        seite = max(1, min(int(q.get("seite", "1")), 500))
    except ValueError:
        seite = 1
    return ListFilters(von=von, bis=bis, kategorie=kategorie, quelle=quelle, kennzeichen=kennzeichen,
                       geloescht=q.get("geloescht") in ("1", "true", "on"), seite=seite)


def _counts(entry: TimeEntry) -> bool:
    """Whether the entry counts towards the day sum shown in the list."""
    return (entry.deleted_at is None and entry.status == EntryStatus.ACTIVE.value
            and not (entry.category is not None and entry.category.is_pseudo))


@dataclass
class EntryRow:
    entry: TimeEntry
    time_label: str
    duration: str
    code: str
    color: str
    title: str
    meta: list[str]
    badges: list[tuple[str, str]]  # (label, css class)
    counts: bool
    deleted: bool


@dataclass
class DayGroup:
    day: date
    rows: list[EntryRow] = field(default_factory=list)
    total: int = 0


def entry_badges(entry: TimeEntry, *, flags: Iterable[str] = LIST_BADGE_FLAGS) -> list[tuple[str, str]]:
    present = set(entry.flags or [])
    badges = [(FLAG_LABELS[f], FLAG_BADGE.get(f, "")) for f in flags if f in present]
    if entry.source == EntrySource.IMPORT.value:
        badges.append(("Import", "badge-outline"))
    if entry.locked:
        badges.append(("gesperrt", "badge-danger"))
    if entry.status == EntryStatus.VORSCHLAG.value:
        badges.append(("Vorschlag", "badge-outline"))
    elif entry.status == EntryStatus.VERWORFEN.value:
        badges.append(("verworfen", "badge-outline"))
    if entry.category is not None and entry.category.is_pseudo:
        badges.append(("zählt nicht", "badge-outline"))
    if entry.deleted_at is not None:
        badges.append(("gelöscht", "badge-danger"))
    return badges


def _time_label(entry: TimeEntry) -> str:
    if not entry.has_bounds:
        return "ohne Uhrzeit"
    start = to_local(entry.start_at).strftime("%H:%M")
    if entry.end_at is None:
        return f"{start}–läuft"
    return f"{start}–{to_local(entry.end_at).strftime('%H:%M')}"


def _row(entry: TimeEntry) -> EntryRow:
    cat, sub = entry.category, entry.subcategory
    meta = [m for m in (entry.klasse, entry.fach, LOCATION_LABELS.get(entry.location or "")) if m]
    if entry.source not in (EntrySource.MANUELL.value, EntrySource.IMPORT.value):  # import has a badge
        meta.append(SOURCE_LABELS.get(entry.source, entry.source))
    return EntryRow(
        entry=entry,
        time_label=_time_label(entry),
        duration="läuft" if entry.end_at is None else fmt_hm(entry.duration_seconds),
        code=cat.code if cat else "?",
        color=cat.color if cat else "#64748B",
        title=(sub.name if sub else (cat.name if cat else "Unbekannte Kategorie")),
        meta=meta,
        badges=entry_badges(entry),
        counts=_counts(entry),
        deleted=entry.deleted_at is not None,
    )


def _query_entries(db: Session, f: ListFilters) -> list[TimeEntry]:
    real_flag = f.kennzeichen if f.kennzeichen not in (None, "vertretung", "gesperrt") else None
    rows = svc.list_entries(
        db, date_from=f.von, date_to=f.bis, category_id=f.kategorie, source=f.quelle,
        flags=[real_flag] if real_flag else None, include_deleted=f.geloescht,
    )
    if not f.geloescht:
        rows = [e for e in rows if e.status != EntryStatus.VERWORFEN.value]
    if f.kennzeichen == "vertretung":
        rows = [e for e in rows if {"vertretung_angeordnet", "vertretung_genehmigt"} & set(e.flags or [])]
    elif f.kennzeichen == "gesperrt":
        rows = [e for e in rows if e.locked]
    return rows


def _group_by_day(entries: list[TimeEntry]) -> list[DayGroup]:
    groups: dict[date, DayGroup] = {}
    for entry in entries:
        group = groups.setdefault(entry.day, DayGroup(day=entry.day))
        row = _row(entry)
        group.rows.append(row)
        if row.counts:
            group.total += entry.duration_seconds or 0
    return [groups[d] for d in sorted(groups, reverse=True)]  # newest day first, entries by start


def _list_context(db: Session, f: ListFilters, *, page_from: int, page_to: int) -> dict[str, Any]:
    entries = _query_entries(db, f)
    groups = _group_by_day(entries)
    shown = groups[(page_from - 1) * PAGE_DAYS: page_to * PAGE_DAYS]
    has_more = len(groups) > page_to * PAGE_DAYS
    return {
        "filters": f,
        "groups": shown,
        "has_more": has_more,
        "next_page": page_to + 1,
        "total_seconds": sum(g.total for g in groups),
        "entry_count": sum(1 for e in entries if e.deleted_at is None),
        "deleted_count": sum(1 for e in entries if e.deleted_at is not None),
        "day_count": len(groups),
        "today": today_local(),
    }


@router.get("/eintraege", response_class=HTMLResponse)
def entries_list(request: Request, db: DbSession) -> HTMLResponse:
    f = _filters_from_query(request)
    ctx = _list_context(db, f, page_from=1, page_to=f.seite)
    ctx.update(
        page_title="Einträge",
        tree=_category_tree(db),
        source_labels=SOURCE_LABELS,
        filter_flags=FILTER_FLAGS,
    )
    if _wants_partial(request):
        return templates.TemplateResponse(request, "partials/entries_results.html", ctx)
    return templates.TemplateResponse(request, "entries.html", ctx)


@router.get("/eintraege/mehr", response_class=HTMLResponse)
def entries_more(request: Request, db: DbSession) -> HTMLResponse:
    f = _filters_from_query(request)
    ctx = _list_context(db, f, page_from=f.seite, page_to=f.seite)
    return templates.TemplateResponse(request, "partials/entries_page.html", ctx)


# --------------------------------------------------------------------------- form state


@dataclass
class EntryForm:
    """Raw form values (strings) as shown in the inputs."""

    datum: str = ""
    modus: str = "zeit"  # 'zeit' (von/bis) | 'dauer'
    von: str = ""
    bis: str = ""
    folgetag: bool = False
    dauer: str = ""
    kategorie: str = ""
    unterkategorie: str = ""
    klasse: str = ""
    fach: str = ""
    ort: str = ""
    notiz: str = ""
    flags: list[str] = field(default_factory=list)
    begruendung: str = ""
    next: str = ""

    @classmethod
    def from_form(cls, form: Mapping[str, Any], flags: list[str]) -> EntryForm:
        def g(key: str) -> str:
            value = form.get(key)
            return value.strip() if isinstance(value, str) else ""

        return cls(
            datum=g("datum"), modus="dauer" if g("modus") == "dauer" else "zeit", von=g("von"), bis=g("bis"),
            folgetag=g("folgetag") in ("1", "on", "true"), dauer=g("dauer"), kategorie=g("kategorie"),
            unterkategorie=g("unterkategorie"), klasse=g("klasse"), fach=g("fach"), ort=g("ort"),
            notiz=g("notiz"), flags=[f for f in flags if f in FORM_FLAGS], begruendung=g("begruendung"),
            next=g("next"),
        )

    @classmethod
    def from_entry(cls, entry: TimeEntry) -> EntryForm:
        if entry.has_bounds:
            start = to_local(entry.start_at)
            end = to_local(entry.end_at) if entry.end_at is not None else None
            modus, von = "zeit", start.strftime("%H:%M")
            bis = end.strftime("%H:%M") if end is not None else ""
            folgetag = end is not None and end.date() > start.date()
            dauer = fmt_hm(entry.duration_seconds) if end is not None else ""
        else:
            modus, von, bis, folgetag, dauer = "dauer", "", "", False, fmt_hm(entry.duration_seconds)
        return cls(
            datum=entry.day.isoformat(), modus=modus, von=von, bis=bis, folgetag=folgetag, dauer=dauer,
            kategorie=str(entry.category_id), unterkategorie=str(entry.subcategory_id or ""),
            klasse=entry.klasse or "", fach=entry.fach or "", ort=entry.location or "", notiz=entry.note or "",
            flags=[f for f in (entry.flags or []) if f in FORM_FLAGS],
        )


class FormError(Exception):
    def __init__(self, message: str, field_name: str | None = None):
        super().__init__(message)
        self.message = message
        self.field = field_name


def _error_field(message: str, modus: str = "zeit") -> str | None:
    """Map a German service message to the form field it concerns (None = general banner only)."""
    m = message.lower()
    if "begründung" in m or "abgeschlossen" in m:
        return "begruendung"
    if "überschneidung" in m or "timer" in m:
        return "von" if modus == "zeit" else None
    if "notiz" in m:
        return "notiz"
    if "unterkategorie" in m:
        return "unterkategorie"
    if "kategorie" in m:
        return "kategorie"
    if "klasse" in m:
        return "klasse"
    if "ungültiger ort" in m:
        return "ort"
    if "datum" in m:
        return "datum"
    if "dauer" in m or "stunden" in m:
        return "dauer" if modus == "dauer" else "bis"
    if "endzeit" in m or "ende" in m:
        return "bis"
    if "start" in m:
        return "von"
    return None


def _times_from_form(form: EntryForm) -> dict[str, Any]:
    """Service data for the time fields; raises FormError for unparsable input."""
    day = _parse_date(form.datum)
    if day is None:
        raise FormError("Bitte ein gültiges Datum angeben.", "datum")
    if form.modus == "dauer":
        if not form.dauer:
            raise FormError("Bitte eine Dauer angeben (z. B. 1:30 oder 1,5).", "dauer")
        return {"has_bounds": False, "day": day, "duration_seconds": form.dauer}
    start_t, end_t = _parse_time(form.von), _parse_time(form.bis)
    if start_t is None:
        raise FormError("Bitte eine Startzeit (Von) angeben.", "von")
    if end_t is None:
        raise FormError("Bitte eine Endzeit (Bis) angeben – oder auf „Dauer“ umschalten.", "bis")
    end_day = day + timedelta(days=1) if form.folgetag else day
    if not form.folgetag and end_t <= start_t:
        raise FormError("„Bis“ muss nach „Von“ liegen. Endet der Eintrag nach Mitternacht, "
                        "„endet am Folgetag“ ankreuzen.", "bis")
    return {"has_bounds": True, "start_at": local_to_utc(day, start_t), "end_at": local_to_utc(end_day, end_t)}


def _attrs_from_form(form: EntryForm) -> dict[str, Any]:
    if not form.kategorie:
        raise FormError("Bitte eine Kategorie wählen.", "kategorie")
    return {
        "category_id": form.kategorie,
        "subcategory_id": form.unterkategorie or None,
        "klasse": form.klasse or None,
        "fach": form.fach or None,
        "location": form.ort or None,
        "note": form.notiz or None,
    }


def _flags_for_category(form: EntryForm, tree: CategoryTree) -> list[str]:
    """Vertretung flags only make sense for Unterricht; drop them for other categories."""
    try:
        cat = tree.by_id.get(int(form.kategorie))
    except ValueError:
        cat = None
    if cat is not None and cat.parent_id is not None:
        cat = tree.by_id.get(cat.parent_id)
    is_u = cat is not None and cat.is_unterricht
    return [f for f in form.flags if f in FORM_FLAGS_ALL or (is_u and f in FORM_FLAGS_U)]


def _create_data(form: EntryForm, tree: CategoryTree) -> dict[str, Any]:
    data = _times_from_form(form)
    data.update(_attrs_from_form(form))
    data["flags"] = _flags_for_category(form, tree)
    return data


def _update_changes(form: EntryForm, entry: TimeEntry, tree: CategoryTree) -> dict[str, Any]:
    """Only the fields the user actually changed (keeps seconds of timer entries untouched)."""
    original = EntryForm.from_entry(entry)
    changes: dict[str, Any] = {}

    if form.modus == "dauer":
        time_changed = (original.modus != "dauer" or form.datum != original.datum
                        or _norm_duration(form.dauer) != _norm_duration(original.dauer))
    else:
        time_changed = original.modus != "zeit" or any(
            getattr(form, k) != getattr(original, k) for k in ("datum", "von", "bis", "folgetag"))
    if time_changed:
        if form.modus == "zeit" and original.modus == "zeit" and entry.end_at is None and not form.bis:
            # running timer: only the start can be moved here
            start_t = _parse_time(form.von)
            day = _parse_date(form.datum)
            if start_t is None or day is None:
                raise FormError("Bitte Datum und Startzeit angeben.", "von")
            changes["start_at"] = local_to_utc(day, start_t)
        else:
            times = _times_from_form(form)
            if times["has_bounds"] and original.modus == "zeit":
                # keep exact (second-precise) original bounds where the minute value did not change
                if form.datum == original.datum and form.von == original.von:
                    times["start_at"] = entry.start_at
                if (form.datum == original.datum and form.bis == original.bis
                        and form.folgetag == original.folgetag and entry.end_at is not None):
                    times["end_at"] = entry.end_at
            changes.update(times)

    attrs = _attrs_from_form(form)
    if form.kategorie != original.kategorie or form.unterkategorie != original.unterkategorie:
        changes["category_id"] = attrs["category_id"]
        changes["subcategory_id"] = attrs["subcategory_id"]
    for key, attr in (("klasse", "klasse"), ("fach", "fach"), ("ort", "location"), ("notiz", "note")):
        if getattr(form, key) != getattr(original, key):
            changes[attr] = attrs[attr]
    new_flags = _flags_for_category(form, tree)
    if set(new_flags) != set(original.flags):
        changes["flags"] = new_flags
    return changes


def _norm_duration(value: str) -> int | None:
    from app.core.parsing import parse_duration

    try:
        return parse_duration(value) if value else None
    except ValueError:
        return -1


# --------------------------------------------------------------------------- form rendering


def _deadline_note(day: date | None) -> bool:
    """True if an entry for ``day`` created now would be flagged 'nachträglich'."""
    return day is not None and is_nachtraeglich(day, now_utc())


def _form_context(db: Session, form: EntryForm, *, entry: TimeEntry | None = None,
                  error: str | None = None, error_field: str | None = None,
                  delete_error: str | None = None) -> dict[str, Any]:
    settings = _settings(db)
    tree = _category_tree(db, keep=(entry.category_id, entry.subcategory_id) if entry else ())
    klassen = list(settings.klassen or []) if settings else []
    if form.klasse and form.klasse not in klassen:
        klassen.append(form.klasse)
    faecher = list(settings.faecher or []) if settings else []
    closed = _closed_months(db)
    day = _parse_date(form.datum)
    month_closed = day is not None and f"{day.year:04d}-{day.month:02d}" in closed
    locked = bool(entry is not None and (entry.locked or (entry.day and
                  f"{entry.day.year:04d}-{entry.day.month:02d}" in closed)))
    return {
        "form": form,
        "entry": entry,
        "tree": tree,
        "klassen": klassen,
        "faecher": faecher,
        "location_labels": LOCATION_LABELS,
        "flag_labels": FLAG_LABELS,
        "form_flags_u": FORM_FLAGS_U,
        "form_flags_all": FORM_FLAGS_ALL,
        "note_placeholder": NOTE_PLACEHOLDER,
        "closed_months": closed,
        "month_closed": month_closed,
        "locked": locked,
        "reason_required": locked or month_closed or error_field == "begruendung",
        "late": _deadline_note(day) if entry is None else False,
        "location_rule": (settings.ui_default_location_rule or {}) if settings else {},
        "error": error,
        "error_field": error_field,
        "delete_error": delete_error,
        "back_url": _safe_next(form.next) or (
            f"/eintraege?{ListFilters(*_month_range(entry.day)).query()}" if entry is not None else "/eintraege"),
    }


def _render_form(request: Request, db: Session, form: EntryForm, *, status_code: int = 200,
                 **kwargs: Any) -> HTMLResponse:
    entry = kwargs.get("entry")
    ctx = _form_context(db, form, **kwargs)
    if entry is None:
        ctx["page_title"] = "Neuer Eintrag"
        return templates.TemplateResponse(request, "entries_form.html", ctx, status_code=status_code)
    ctx["page_title"] = "Eintrag bearbeiten" if entry.deleted_at is None else "Gelöschter Eintrag"
    ctx.update(_detail_context(db, entry, ctx["tree"]))
    return templates.TemplateResponse(request, "entries_edit.html", ctx, status_code=status_code)


def _list_url_for(day: date) -> str:
    return f"/eintraege?{ListFilters(*_month_range(day)).query()}"


async def _read_form(request: Request) -> tuple[Mapping[str, Any], list[str]]:
    form = await request.form()
    flags = [v for v in form.getlist("flags") if isinstance(v, str)]
    return form, flags


# --------------------------------------------------------------------------- create


@router.get("/eintraege/neu", response_class=HTMLResponse)
def entry_new(request: Request, db: DbSession) -> HTMLResponse:
    q = request.query_params
    day = _parse_date(q.get("datum")) or today_local()
    form = EntryForm(datum=day.isoformat(), next=_safe_next(q.get("next")) or "")
    if _parse_time(q.get("von")):
        form.von = q["von"]
    if _parse_time(q.get("bis")):
        form.bis = q["bis"]
    if q.get("modus") == "dauer":
        form.modus = "dauer"
    if q.get("kategorie", "").isdigit():
        form.kategorie = q["kategorie"]
    return _render_form(request, db, form)


@router.post("/eintraege/neu", response_class=HTMLResponse)
async def entry_create(request: Request, db: DbSession) -> Response:
    raw, flags = await _read_form(request)
    form = EntryForm.from_form(raw, flags)
    tree = _category_tree(db)
    try:
        data = _create_data(form, tree)
        created = svc.create_entry(db, data, source=EntrySource.MANUELL.value, reason=form.begruendung or None)
    except FormError as exc:
        return _render_form(request, db, form, status_code=422, error=exc.message, error_field=exc.field)
    except svc.DomainError as exc:
        db.rollback()
        return _render_form(request, db, form, status_code=422, error=exc.message,
                            error_field=_error_field(exc.message, form.modus))
    total = sum(e.duration_seconds for e in created)
    message = f"Eintrag gespeichert ({fmt_hm(total)} h)."
    if len(created) > 1:
        message += f" Über Mitternacht – automatisch in {len(created)} Einträge geteilt."
    if any("nachtraeglich" in (e.flags or []) for e in created):
        message += " Kennzeichen „nachträglich“ gesetzt."
    flash(request, message, "success")
    target = _safe_next(form.next) or _list_url_for(created[0].day)
    return RedirectResponse(target, status_code=303)


# --------------------------------------------------------------------------- detail / edit


def _get_or_404(db: Session, entry_id: str) -> TimeEntry:
    try:
        return svc.get_entry(db, entry_id)
    except svc.EntryNotFound as exc:
        raise HTTPException(404, "Eintrag nicht gefunden.") from exc


@router.get("/eintraege/{entry_id}", response_class=HTMLResponse)
def entry_detail(request: Request, entry_id: str, db: DbSession) -> HTMLResponse:
    entry = _get_or_404(db, entry_id)
    form = EntryForm.from_entry(entry)
    form.next = _safe_next(request.query_params.get("next")) or ""
    return _render_form(request, db, form, entry=entry)


@router.post("/eintraege/{entry_id}", response_class=HTMLResponse)
async def entry_update(request: Request, entry_id: str, db: DbSession) -> Response:
    entry = _get_or_404(db, entry_id)
    raw, flags = await _read_form(request)
    form = EntryForm.from_form(raw, flags)
    tree = _category_tree(db, keep=(entry.category_id, entry.subcategory_id))
    try:
        if entry.deleted_at is not None:
            raise FormError("Gelöschte Einträge können nicht bearbeitet werden – zuerst wiederherstellen.")
        changes = _update_changes(form, entry, tree)
        if not changes:
            flash(request, "Keine Änderung.", "info")
            return RedirectResponse(f"/eintraege/{entry.id}", status_code=303)
        before = len(svc.revisions(db, entry.id))
        result = svc.update_entry(db, entry.id, changes, reason=form.begruendung or None)
    except FormError as exc:
        return _render_form(request, db, form, entry=entry, status_code=422, error=exc.message,
                            error_field=exc.field)
    except svc.DomainError as exc:
        db.rollback()
        db.refresh(entry)
        return _render_form(request, db, form, entry=entry, status_code=422, error=exc.message,
                            error_field=_error_field(exc.message, form.modus))
    after = len(svc.revisions(db, entry.id))
    if after == before:
        flash(request, "Keine Änderung.", "info")
    else:
        message = "Änderung gespeichert – Version im Verlauf festgehalten."
        if len(result) > 1:
            message += f" Über Mitternacht – in {len(result)} Einträge geteilt."
        flash(request, message, "success")
    target = f"/eintraege/{entry.id}"
    if _safe_next(form.next):
        target += "?" + urlencode({"next": form.next})
    return RedirectResponse(target, status_code=303)


@router.post("/eintraege/{entry_id}/loeschen", response_class=HTMLResponse)
async def entry_delete(request: Request, entry_id: str, db: DbSession) -> Response:
    entry = _get_or_404(db, entry_id)
    raw = await request.form()
    reason = raw.get("grund")
    reason = reason.strip() if isinstance(reason, str) else ""
    try:
        svc.delete_entry(db, entry.id, reason=reason or None)
    except svc.DomainError as exc:
        db.rollback()
        db.refresh(entry)
        form = EntryForm.from_entry(entry)
        return _render_form(request, db, form, entry=entry, status_code=422, delete_error=exc.message)
    flash(request, "Eintrag gelöscht. Er bleibt im Verlauf sichtbar und kann wiederhergestellt werden.", "success")
    next_url = raw.get("next")
    return RedirectResponse(_safe_next(next_url if isinstance(next_url, str) else None)
                            or _list_url_for(entry.day), status_code=303)


@router.post("/eintraege/{entry_id}/wiederherstellen", response_class=HTMLResponse)
async def entry_restore(request: Request, entry_id: str, db: DbSession) -> Response:
    entry = _get_or_404(db, entry_id)
    raw = await request.form()
    reason = raw.get("grund")
    reason = reason.strip() if isinstance(reason, str) else ""
    try:
        svc.restore_entry(db, entry.id, reason=reason or None)
    except svc.DomainError as exc:
        db.rollback()
        db.refresh(entry)
        form = EntryForm.from_entry(entry)
        return _render_form(request, db, form, entry=entry, status_code=422, delete_error=exc.message)
    flash(request, "Eintrag wiederhergestellt.", "success")
    return RedirectResponse(f"/eintraege/{entry.id}", status_code=303)


# --------------------------------------------------------------------------- revision history

DIFF_FIELDS = ("day", "times", "duration_seconds", "category", "klasse", "fach", "location", "note", "flags",
               "status", "deleted_at")


def _snap_category(snap: Mapping[str, Any], tree: CategoryTree) -> str:
    cat = tree.by_id.get(snap.get("category_id"))
    sub = tree.by_id.get(snap.get("subcategory_id"))
    if cat is None:
        return "?"
    return f"{cat.code} {cat.name}" + (f" / {sub.name}" if sub else "")


def _snap_times(snap: Mapping[str, Any]) -> str:
    if not snap.get("has_bounds", True):
        return "ohne Uhrzeit"
    start = _dt_local_str(snap.get("start_at"), "%H:%M")
    end = _dt_local_str(snap.get("end_at"), "%H:%M") if snap.get("end_at") else "läuft"
    return f"{start}–{end}"


def snapshot_summary(snap: Mapping[str, Any], tree: CategoryTree) -> str:
    """'13:30–15:10 K/Proben 3b' (Konzept §8.3)."""
    if snap.get("has_bounds", True):
        head = _snap_times(snap)
    else:
        head = f"{fmt_hm(snap.get('duration_seconds') or 0)} h ohne Uhrzeit"
    parts = [head, tree.label(snap.get("category_id"), snap.get("subcategory_id"))]
    if snap.get("klasse"):
        parts.append(snap["klasse"])
    return " ".join(parts)


def _diff_value(key: str, snap: Mapping[str, Any], tree: CategoryTree) -> str:
    if key == "day":
        d = _parse_date(snap.get("day"))
        return d.strftime("%d.%m.%Y") if d else "–"
    if key == "times":
        return _snap_times(snap)
    if key == "duration_seconds":
        return fmt_hm(snap.get("duration_seconds") or 0)
    if key == "category":
        return _snap_category(snap, tree)
    if key == "location":
        return LOCATION_LABELS.get(snap.get("location") or "", "–")
    if key == "flags":
        flags = [FLAG_LABELS.get(f, f) for f in (snap.get("flags") or [])]
        return ", ".join(flags) if flags else "–"
    if key == "status":
        return STATUS_LABELS.get(snap.get("status") or "", snap.get("status") or "–")
    if key == "deleted_at":
        return "gelöscht" if snap.get("deleted_at") else "aktiv"
    value = snap.get(key)
    return str(value) if value not in (None, "") else "–"


DIFF_LABELS = {
    "day": "Datum", "times": "Zeit", "duration_seconds": "Dauer", "category": "Kategorie", "klasse": "Klasse",
    "fach": "Fach", "location": "Ort", "note": "Notiz", "flags": "Kennzeichen", "status": "Status",
    "deleted_at": "Zustand",
}


def snapshot_diff(before: Mapping[str, Any], after: Mapping[str, Any], tree: CategoryTree
                  ) -> list[tuple[str, str, str]]:
    """[(label, old, new)] for every displayed field that differs."""
    result = []
    for key in DIFF_FIELDS:
        old, new = _diff_value(key, before, tree), _diff_value(key, after, tree)
        if old != new:
            result.append((DIFF_LABELS[key], old, new))
    return result


@dataclass
class HistoryItem:
    kind: str  # 'version' | 'erstellt'
    title: str
    summary: str
    changed_at: str = ""
    change_label: str = ""
    reason: str | None = None
    diff: list[tuple[str, str, str]] = field(default_factory=list)
    current: bool = False


def _history(db: Session, entry: TimeEntry, tree: CategoryTree) -> list[HistoryItem]:
    revs = svc.revisions(db, entry.id)
    states = [r for r in revs if r.revision_no > 0]
    notes = [r for r in revs if r.revision_no == 0]
    current = svc.entry_snapshot(entry)
    items: list[HistoryItem] = []
    version_no = len(states) + 1
    items.append(HistoryItem(
        kind="version", current=True,
        title=f"Version {version_no} (aktuell) vom {_dt_local_str(entry.updated_at, '%d.%m. %H:%M')}",
        summary=snapshot_summary(current, tree),
    ))
    for idx in range(len(states) - 1, -1, -1):
        rev = states[idx]
        snap = rev.snapshot_json or {}
        nxt = states[idx + 1].snapshot_json if idx + 1 < len(states) else current
        kind = svc.revision_kind(rev)
        change_label = {"geloescht": "Gelöscht", "wiederhergestellt": "Wiederhergestellt"}.get(kind, "Geändert")
        items.append(HistoryItem(
            kind="version",
            title=f"Version {rev.revision_no} vom {_dt_local_str(snap.get('updated_at'), '%d.%m. %H:%M')}",
            summary=snapshot_summary(snap, tree),
            changed_at=_dt_local_str(rev.changed_at, "%d.%m.%Y %H:%M"),
            change_label=change_label,
            reason=rev.change_reason,
            diff=snapshot_diff(snap, nxt or {}, tree),
        ))
    for rev in notes:
        snap = rev.snapshot_json or {}
        split = snap.get("split") or None
        summary = "Angelegt"
        if split:
            summary = (f"Automatisch an Mitternacht geteilt – Teil {split.get('part_no')} von "
                       f"{len(split.get('parts') or [])} (ursprünglich "
                       f"{_dt_local_str(split.get('original_start_at'), '%d.%m. %H:%M')}–"
                       f"{_dt_local_str(split.get('original_end_at'), '%d.%m. %H:%M')})")
        elif snap.get("reason"):
            summary = "Angelegt im abgeschlossenen Monat"
        items.append(HistoryItem(
            kind="erstellt", title=f"Erstellt am {_dt_local_str(rev.changed_at, '%d.%m.%Y %H:%M')}",
            summary=summary, reason=rev.change_reason,
        ))
    return items


def _detail_context(db: Session, entry: TimeEntry, tree: CategoryTree) -> dict[str, Any]:
    return {
        "history": _history(db, entry, tree),
        "badges": entry_badges(entry, flags=tuple(FLAG_LABELS)),
        "source_label": SOURCE_LABELS.get(entry.source, entry.source),
        "running": entry.end_at is None and entry.deleted_at is None,
        "created_label": _dt_local_str(entry.created_at, "%d.%m.%Y %H:%M"),
        "deleted_label": _dt_local_str(entry.deleted_at, "%d.%m.%Y %H:%M") if entry.deleted_at else "",
    }
