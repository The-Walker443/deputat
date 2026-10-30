"""Reports: month report (Konzept §10.2) and year report (§10.3) as self-contained, print-friendly HTML.

- :func:`render_month_report` – header (name, school, UPZ, Teilzeitfaktor, period, created, hash), key
  figures, categories, weeks (Ist/Soll/W_max), Mehrarbeit, the complete entry list of the month
  (incl. soft-deleted entries that are part of the hash, source, created/updated, import marker
  "Import vom DD.MM.YYYY aus Datei X"), the change log (revisions of the month) and a footer with the
  hash, APP_VERSION and RULES_VERSION.
- :func:`render_year_report` – the same for a period plus the saldo chart (inline SVG rendered on the
  server, no JS), the leave assignment, Ferienarbeit per Ferienabschnitt, both leave modes and the
  Klassenfahrt variants A/B.

The CSS (``app/static/css/report.css``) is inlined, the chart is inline SVG: a stored report file opens
anywhere without the app. Templates live in ``app/web/templates/reports/``.

Hashes: a closed month shows its stored ``month_close`` hash; an open month shows the hash of the
current data marked as preliminary. Both come from :mod:`app.services.entries` (canonical mapping) and
:mod:`app.core.hashing`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from html import escape
from typing import Any

from markupsafe import Markup
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import APP_VERSION, PROJECT_ROOT, RULES_VERSION
from app.core import account as core_account
from app.core.hashing import HASH_VERSION, entries_hash
from app.core.types import AbsenceType, DayType, KlassenfahrtVariante, LeaveMode
from app.db.models import (
    Category,
    EntryStatus,
    ImportBatch,
    MonthClose,
    Period,
    Settings,
    TimeEntry,
    TimeEntryRevision,
)
from app.services import calendar_service as cal
from app.services import entries as entries_service
from app.services import ledger as ledger_service
from app.services import months as months_service
from app.services.settings_service import get_settings, teilzeitfaktor
from app.util import fmt_dec, fmt_hm, now_utc, to_local, today_local

REPORT_CSS_PATH = PROJECT_ROOT / "app" / "static" / "css" / "report.css"

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
    "ohne_zeitgrenzen": "ohne Zeitgrenzen",
}
SOURCE_LABELS = {"timer": "Timer", "manuell": "Manuell", "stundenplan": "Stundenplan", "import": "Import"}
LOCATION_LABELS = {"schule": "Schule", "zuhause": "Zuhause", "unterwegs": "Unterwegs", "extern": "Extern"}
STATUS_LABELS = {"active": "aktiv", "vorschlag": "Vorschlag", "verworfen": "verworfen"}
REVISION_KIND_LABELS = {
    "erstellt": "Erstellt",
    "geaendert": "Geändert",
    "geloescht": "Gelöscht",
    "wiederhergestellt": "Wiederhergestellt",
}
DAY_TYPE_LABELS = {
    DayType.SCHULTAG: "Schultag",
    DayType.FERIENTAG: "Ferientag",
    DayType.UNTERRICHTSFREI: "unterrichtsfrei",
    DayType.FEIERTAG: "Feiertag",
    DayType.WOCHENENDE: "Wochenende",
}
DIFF_FIELDS: tuple[tuple[str, str], ...] = (
    ("day", "Tag"),
    ("start_at", "Von"),
    ("end_at", "Bis"),
    ("duration_seconds", "Dauer"),
    ("has_bounds", "Mit Uhrzeiten"),
    ("category_id", "Kategorie"),
    ("subcategory_id", "Unterkategorie"),
    ("klasse", "Klasse"),
    ("fach", "Fach"),
    ("location", "Ort"),
    ("note", "Notiz"),
    ("flags", "Flags"),
    ("status", "Status"),
    ("deleted_at", "Gelöscht"),
)
WEEKDAYS_SHORT = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
MONTH_SHORT = ("Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez")


@lru_cache(maxsize=1)
def report_css() -> str:
    """The report stylesheet (inlined into every report)."""
    return REPORT_CSS_PATH.read_text(encoding="utf-8")


def _templates():
    from app.web.templating import templates  # imported lazily: keeps services free of web imports at load

    return templates.env


# =========================================================================== formatting helpers


def num_de(value: float | int | None) -> str:
    """12.0 -> '12', 27.5 -> '27,5' (German decimal comma, no trailing zeros)."""
    if value is None:
        return ""
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _d(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


def _dt(dt: datetime | None) -> str:
    return to_local(dt).strftime("%d.%m.%Y %H:%M") if dt else ""


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _time_of(entry_day: date, value: datetime | None) -> str:
    if value is None:
        return "läuft"
    local = to_local(value)
    if local.date() > entry_day and local.hour == 0 and local.minute == 0 and local.second == 0:
        return "24:00"
    return local.strftime("%H:%M:%S") if local.second else local.strftime("%H:%M")


def flags_text(flags: Iterable[str] | None) -> str:
    return ", ".join(FLAG_LABELS.get(f, f) for f in (flags or []))


def import_marker(batch: ImportBatch | None) -> str | None:
    """Konzept §12: 'Import vom DD.MM.YYYY aus Datei X'."""
    if batch is None:
        return None
    return f"Import vom {to_local(batch.created_at).strftime('%d.%m.%Y')} aus Datei {batch.filename}"


def _categories(db: Session) -> dict[int, Category]:
    return {c.id: c for c in db.scalars(select(Category))}


def _batches(db: Session, ids: Iterable[str | None]) -> dict[str, ImportBatch]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return {b.id: b for b in db.scalars(select(ImportBatch).where(ImportBatch.id.in_(wanted)))}


# =========================================================================== entries and revisions


def entry_rows(db: Session, entries: Sequence[TimeEntry]) -> list[dict[str, Any]]:
    """Pre-formatted rows of the complete entry list (Konzept §10.2)."""
    cats = _categories(db)
    batches = _batches(db, (e.import_batch_id for e in entries))
    rows = []
    for e in entries:
        cat = cats.get(e.category_id)
        sub = cats.get(e.subcategory_id) if e.subcategory_id else None
        seconds = int(e.duration_seconds or 0)
        rows.append(
            {
                "id": e.id,
                "date": e.day,
                "weekday": WEEKDAYS_SHORT[e.day.weekday()],
                "von": _time_of(e.day, e.start_at) if e.has_bounds else "–",
                "bis": _time_of(e.day, e.end_at) if e.has_bounds else "–",
                "seconds": seconds,
                "dauer": fmt_hm(seconds),
                "dauer_dec": fmt_dec(seconds),
                "category": f"{cat.code} {cat.name}" if cat else str(e.category_id),
                "category_color": cat.color if cat else "#888888",
                "is_pseudo": bool(cat.is_pseudo) if cat else False,
                "subcategory": sub.name if sub else "",
                "klasse_fach": " / ".join(x for x in (e.klasse, e.fach) if x),
                "ort": LOCATION_LABELS.get(e.location or "", e.location or ""),
                "flags": flags_text(e.flags),
                "quelle": SOURCE_LABELS.get(e.source, e.source),
                "import": import_marker(batches.get(e.import_batch_id or "")),
                "note": e.note or "",
                "created": _dt(e.created_at),
                "updated": _dt(e.updated_at) if e.updated_at and e.updated_at != e.created_at else "",
                "deleted": _dt(e.deleted_at) if e.deleted_at else "",
                "locked": bool(e.locked),
            }
        )
    return rows


def _fmt_field(field: str, value: Any, cats: dict[int, Category], day: date | None) -> str:
    if value is None or value == "" or value == []:
        return "–"
    if field in ("start_at", "end_at"):
        dt = _parse_iso(value)
        return to_local(dt).strftime("%d.%m.%Y %H:%M:%S") if dt else str(value)
    if field == "deleted_at":
        dt = _parse_iso(value)
        return f"ja ({_dt(dt)})" if dt else "ja"
    if field == "day":
        try:
            return _d(date.fromisoformat(str(value)))
        except ValueError:
            return str(value)
    if field == "duration_seconds":
        return fmt_hm(int(value))
    if field in ("category_id", "subcategory_id"):
        cat = cats.get(int(value))
        return f"{cat.code} {cat.name}" if cat else str(value)
    if field == "has_bounds":
        return "ja" if value else "nein"
    if field == "flags":
        return flags_text(value) or "–"
    if field == "location":
        return LOCATION_LABELS.get(str(value), str(value))
    if field == "status":
        return STATUS_LABELS.get(str(value), str(value))
    return str(value)


def _norm_for_diff(field: str, value: Any) -> Any:
    if field == "flags":
        return sorted(value or [])
    if value == "":
        return None
    return value


def revision_rows(db: Session, start: date, end: date) -> list[dict[str, Any]]:
    """Change log of the entries of ``start..end`` (also entries moved into/out of the range).

    Each revision row lists what changed: the snapshot is the state *before* the change, the state
    after it is the next revision's snapshot or the entry's current state.
    """
    in_range = select(TimeEntry.id).where(TimeEntry.day.between(start, end))
    snap_day = func.json_extract(TimeEntryRevision.snapshot_json, "$.day")
    touched = db.scalars(
        select(TimeEntryRevision.entry_id).where(
            or_(
                TimeEntryRevision.entry_id.in_(in_range),
                snap_day.between(start.isoformat(), end.isoformat()),
            )
        )
    ).all()
    entry_ids = sorted(set(touched))
    if not entry_ids:
        return []
    cats = _categories(db)
    entries = {e.id: e for e in db.scalars(select(TimeEntry).where(TimeEntry.id.in_(entry_ids)))}
    revs_by_entry: dict[str, list[TimeEntryRevision]] = defaultdict(list)
    for rev in db.scalars(
        select(TimeEntryRevision)
        .where(TimeEntryRevision.entry_id.in_(entry_ids))
        .order_by(TimeEntryRevision.entry_id, TimeEntryRevision.revision_no)
    ):
        revs_by_entry[rev.entry_id].append(rev)

    rows: list[dict[str, Any]] = []
    for entry_id, revs in revs_by_entry.items():
        entry = entries.get(entry_id)
        current = entries_service.entry_snapshot(entry) if entry is not None else {}
        state_revs = [r for r in revs if entries_service.revision_kind(r) != "erstellt"]
        for rev in revs:
            snap = rev.snapshot_json or {}
            kind = entries_service.revision_kind(rev)
            day_value = snap.get("day") or current.get("day")
            try:
                rev_day = date.fromisoformat(str(day_value)) if day_value else None
            except ValueError:
                rev_day = None
            if kind != "erstellt" and rev_day is not None and not (start <= rev_day <= end):
                after_day = None
                nxt = next((r for r in state_revs if r.revision_no > rev.revision_no), None)
                after = (nxt.snapshot_json if nxt is not None else current) or {}
                try:
                    after_day = date.fromisoformat(str(after.get("day"))) if after.get("day") else None
                except ValueError:
                    after_day = None
                if after_day is None or not (start <= after_day <= end):
                    continue  # neither before nor after in the range
            changes: list[dict[str, str]] = []
            detail = ""
            if kind == "erstellt":
                split = snap.get("split")
                if split:
                    a, b = _parse_iso(split.get("original_start_at")), _parse_iso(split.get("original_end_at"))
                    detail = (
                        f"An Mitternacht geteilt (Teil {split.get('part_no')} von {len(split.get('parts') or [])}); "
                        f"ursprünglich {_dt(a)} – {_dt(b)}, "
                        f"{fmt_hm(int(split.get('original_duration_seconds') or 0))}"
                    )
                else:
                    detail = "Angelegt"
            else:
                nxt = next((r for r in state_revs if r.revision_no > rev.revision_no), None)
                after = (nxt.snapshot_json if nxt is not None else current) or {}
                ref_day = rev_day
                for field, label in DIFF_FIELDS:
                    before_v, after_v = snap.get(field), after.get(field)
                    if _norm_for_diff(field, before_v) == _norm_for_diff(field, after_v):
                        continue
                    changes.append(
                        {
                            "field": label,
                            "before": _fmt_field(field, before_v, cats, ref_day),
                            "after": _fmt_field(field, after_v, cats, ref_day),
                        }
                    )
            cat_id = (current or snap).get("category_id")
            cat = cats.get(int(cat_id)) if cat_id is not None else None
            start_dt = _parse_iso((current or snap).get("start_at"))
            rows.append(
                {
                    "changed_at_raw": rev.changed_at,
                    "changed_at": _dt(rev.changed_at),
                    "entry_id": entry_id,
                    "entry": (
                        f"{_d(rev_day)} {to_local(start_dt).strftime('%H:%M') if start_dt else ''} "
                        f"{cat.code if cat else ''}"
                    ).strip(),
                    "revision_no": rev.revision_no,
                    "kind": kind,
                    "kind_label": REVISION_KIND_LABELS.get(kind, kind),
                    "reason": rev.change_reason or "",
                    "changes": changes,
                    "detail": detail,
                }
            )
    rows.sort(key=lambda r: (r["changed_at_raw"], r["entry_id"], r["revision_no"]))
    return rows


def hashed_entries(db: Session, start: date, end: date) -> list[TimeEntry]:
    """Entries that make up a hash for ``start..end`` (same selection as ``entries.month_hash_entries``)."""
    return list(
        db.scalars(
            select(TimeEntry)
            .where(TimeEntry.day.between(start, end), TimeEntry.status == EntryStatus.ACTIVE.value)
            .order_by(TimeEntry.start_at, TimeEntry.id)
        )
    )


def range_hash(entries: Iterable[TimeEntry]) -> str:
    return entries_hash(entries_service.entry_hash_dict(e) for e in entries)


# =========================================================================== header


def _head(db: Session, period: Period, now: datetime) -> dict[str, Any]:
    # Read-only on purpose: get_settings() may commit (Altersermäßigung refresh), which must not happen
    # inside the month-close transaction that renders this report.
    settings = db.get(Settings, 1) or get_settings(db)
    leave_mode = LeaveMode(settings.urlaubs_modus)
    return {
        "name": settings.name or "",
        "schule": settings.schule or "",
        "upz_voll": settings.upz_voll,
        "upz_ist": settings.upz_ist,
        "altersermaessigung": settings.altersermaessigung or 0,
        "anrechnungsstunden": settings.anrechnungsstunden or 0,
        "anrechnungs_grund": settings.anrechnungs_grund or "",
        "teilzeitfaktor": teilzeitfaktor(settings),
        "teilzeitfaktor_text": f"{teilzeitfaktor(settings):.3f}".replace(".", ","),
        "wochen_soll_voll_h": settings.wochen_soll_voll_h,
        "upz_ist_text": num_de(settings.upz_ist),
        "upz_voll_text": num_de(settings.upz_voll),
        "altersermaessigung_text": num_de(settings.altersermaessigung or 0),
        "anrechnungsstunden_text": num_de(settings.anrechnungsstunden or 0),
        "wochen_soll_text": num_de(settings.wochen_soll_voll_h),
        "period_label": period.label,
        "period_range": f"{_d(period.start)} – {_d(period.end)}",
        "leave_mode": cal.LEAVE_MODE_LABELS.get(leave_mode, leave_mode.value),
        "klassenfahrt_variante": settings.klassenfahrt_variante or "B",
        "krank_neutralisiert": bool(settings.krank_neutralisiert),
        "created": _dt(now),
    }


def _footer(hash_value: str, preliminary: bool) -> dict[str, Any]:
    return {
        "hash": hash_value,
        "preliminary": preliminary,
        "app_version": APP_VERSION,
        "rules_version": RULES_VERSION,
        "hash_version": HASH_VERSION,
    }


# =========================================================================== month report


def month_report_data(
    db: Session,
    ym: str,
    *,
    today: date | None = None,
    now: datetime | None = None,
    close_note: str | None = None,
    forced_items: Sequence[months_service.OpenItem] | None = None,
) -> dict[str, Any]:
    today = today or today_local()
    now = now or now_utc()
    ov = months_service.month_overview(db, ym, today=today, sync=False)
    period = db.get(Period, ov.period_id)
    assert period is not None

    entries = entries_service.month_hash_entries(db, ov.year_month)
    mc = db.get(MonthClose, ov.year_month)
    if mc is not None:
        hash_value, preliminary = mc.entries_hash_sha256, False
    else:
        hash_value, preliminary = range_hash(entries), True

    if mc is not None and close_note is None and forced_items is None:
        # Live render of a closed month: show the stored reason / overridden items of a forced close.
        close_note = mc.note
        forced_items = [months_service.OpenItem(
            kind=str(i.get("kind") or ""),
            message=str(i.get("message") or ""),
            date=date.fromisoformat(i["date"]) if i.get("date") else None,
        ) for i in (mc.forced_items or [])]

    rows = entry_rows(db, entries)
    teaching_flagged = [
        r for r, e in zip(rows, entries, strict=True)
        if e.deleted_at is None and {"vertretung_angeordnet", "vertretung_genehmigt"} & set(e.flags or [])
    ]
    return {
        "kind": "month",
        "title": f"Monatsbericht {ov.label}",
        "css": Markup(report_css()),
        "head": _head(db, period, now),
        "ov": ov,
        "hash": hash_value,
        "preliminary": preliminary,
        "close": mc,
        "close_status_label": months_service.STATUS_LABELS.get(mc.status, mc.status) if mc else "offen",
        "hash_history": [
            {**h, "closed_at_text": _dt(_parse_iso(h.get("closed_at"))),
             "replaced_at_text": _dt(_parse_iso(h.get("replaced_at")))}
            for h in (mc.hash_history or [] if mc else [])
        ],
        "close_note": close_note,
        "forced_items": list(forced_items or []),
        "entries": rows,
        "entries_total": sum(r["seconds"] for r in rows if not r["deleted"] and not r["is_pseudo"]),
        "entries_deleted": sum(1 for r in rows if r["deleted"]),
        "mehrarbeit_entries": teaching_flagged,
        "revisions": revision_rows(db, ov.start, ov.end),
        "footer": _footer(hash_value, preliminary),
        "eu_hours_text": fmt_dec(ov.eu.hours * 3600),
    }


def render_month_report(
    db: Session,
    ym: str,
    *,
    today: date | None = None,
    now: datetime | None = None,
    close_note: str | None = None,
    forced_items: Sequence[months_service.OpenItem] | None = None,
) -> str:
    """The month report as a self-contained HTML document (Konzept §10.2)."""
    data = month_report_data(db, ym, today=today, now=now, close_note=close_note, forced_items=forced_items)
    return _templates().get_template("reports/month.html").render(**data)


# =========================================================================== chart


@dataclass(frozen=True)
class _Scale:
    x0: float
    x1: float
    d0: date
    d1: date
    y0: float
    y1: float
    v0: float
    v1: float

    def x(self, d: date) -> float:
        span = max(1, (self.d1 - self.d0).days)
        return self.x0 + (self.x1 - self.x0) * (d - self.d0).days / span

    def y(self, v: float) -> float:
        span = (self.v1 - self.v0) or 1.0
        return self.y1 - (self.y1 - self.y0) * (v - self.v0) / span


def _nice_step(span_h: float) -> int:
    for step in (5, 10, 20, 25, 50, 100, 200, 250, 500):
        if span_h / step <= 6:
            return step
    return 1000


def saldo_chart_svg(ctx: ledger_service.LedgerContext, *, width: int = 820, height: int = 280) -> Markup:
    """Saldo over the period as inline SVG (Konzept §9.1/§10.3): line up to the evaluation date,
    dashed forecast to the period end, ferien bands, leave days darker, zero line = 40-h reference."""
    period = ctx.period
    as_of = ctx.as_of
    points = [(d, s) for d, s in ctx.series if d <= as_of] if ctx.today >= period.start else []
    values_h = [s / 3600 for _, s in points] + [0.0, ctx.prognose_ende / 3600]
    lo, hi = min(values_h), max(values_h)
    pad = max(5.0, (hi - lo) * 0.08)
    step = _nice_step((hi - lo) + 2 * pad)
    v0 = (int((lo - pad) // step)) * step
    v1 = (int((hi + pad) // step) + 1) * step
    sc = _Scale(52, width - 12, period.start, period.end, 14, height - 30, v0, v1)

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Saldo-Verlauf {escape(period.label)}" font-family="system-ui, sans-serif" font-size="10">'
    ]
    # ferien bands and leave days
    for f in ctx.ferien:
        a, b = max(f.start, period.start), min(f.end, period.end)
        if a > b:
            continue
        x_a, x_b = sc.x(a), sc.x(b + timedelta(days=1)) if b < period.end else sc.x(b)
        out.append(
            f'<rect x="{x_a:.1f}" y="{sc.y0}" width="{max(1.0, x_b - x_a):.1f}" height="{sc.y1 - sc.y0}" '
            f'fill="#dfe7f5"><title>{escape(f.name)}</title></rect>'
        )
    day_w = max(1.0, (sc.x1 - sc.x0) / max(1, (period.end - period.start).days + 1))
    for d in sorted(ctx.plan.days):
        out.append(
            f'<rect x="{sc.x(d):.1f}" y="{sc.y0}" width="{day_w:.2f}" height="{sc.y1 - sc.y0}" fill="#aebfdc"/>'
        )
    # grid + y labels
    v = v0
    while v <= v1 + 1e-9:
        y = sc.y(v)
        stroke = "#15191f" if v == 0 else "#e3e7ec"
        width_line = 1.2 if v == 0 else 0.6
        out.append(f'<line x1="{sc.x0}" x2="{sc.x1}" y1="{y:.1f}" y2="{y:.1f}" stroke="{stroke}" '
                   f'stroke-width="{width_line}"/>')
        label = f"{v:+d} h" if v else "0 h"
        out.append(f'<text x="{sc.x0 - 6}" y="{y + 3:.1f}" text-anchor="end" fill="#555e6b">{label}</text>')
        v += step
    # month ticks
    m = date(period.start.year, period.start.month, 1)
    while m <= period.end:
        if m >= period.start:
            x = sc.x(m)
            out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{sc.y1}" y2="{sc.y1 + 4}" stroke="#8a93a0"/>')
            out.append(f'<text x="{x + 2:.1f}" y="{sc.y1 + 15}" fill="#555e6b">{MONTH_SHORT[m.month - 1]}</text>')
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    # saldo line
    if points:
        pts = " ".join(f"{sc.x(d):.1f},{sc.y(s / 3600):.1f}" for d, s in points)
        out.append(f'<polyline points="{pts}" fill="none" stroke="#2453c9" stroke-width="1.8" '
                   'stroke-linejoin="round"/>')
        last_d, last_s = points[-1]
        if last_d < period.end:
            out.append(
                f'<line x1="{sc.x(last_d):.1f}" y1="{sc.y(last_s / 3600):.1f}" x2="{sc.x(period.end):.1f}" '
                f'y2="{sc.y(ctx.prognose_ende / 3600):.1f}" stroke="#2453c9" stroke-width="1.4" '
                'stroke-dasharray="5 4"/>'
            )
        out.append(f'<circle cx="{sc.x(last_d):.1f}" cy="{sc.y(last_s / 3600):.1f}" r="3" fill="#2453c9"/>')
    out.append(f'<rect x="{sc.x0}" y="{sc.y0}" width="{sc.x1 - sc.x0}" height="{sc.y1 - sc.y0}" '
               'fill="none" stroke="#c9cfd8"/>')
    out.append("</svg>")
    return Markup("".join(out))


# =========================================================================== year report


def _ferien_rows(ctx: ledger_service.LedgerContext) -> list[dict[str, Any]]:
    result = []
    for f in ctx.ferien:
        block = [r for r in ctx.ledger if r.ferien_id == f.id and core_account.is_ferien_row(r)]
        werktage = [r for r in block if r.date.isoweekday() <= 5 and not r.is_holiday]
        urlaub = sum(1 for r in block if r.absence == AbsenceType.URLAUB)
        if f.end < ctx.today:
            state = "vergangen"
        elif f.start <= ctx.today:
            state = "laufend"
        else:
            state = "kommend"
        planned = ctx.planned_work.get(f.id)
        result.append(
            {
                "name": f.name,
                "range": f"{_d(f.start)} – {_d(f.end)}",
                "werktage": len(werktage),
                "urlaubstage": urlaub,
                "abbau": sum(r.soll for r in block),
                "arbeit": sum(r.ist_erfasst for r in block if r.date <= ctx.today),
                "geplant": planned,
                "state": state,
            }
        )
    return result


def _leave_list(ctx: ledger_service.LedgerContext) -> list[dict[str, Any]]:
    by_block: dict[str, list[date]] = defaultdict(list)
    names = {f.id: f.name for f in ctx.ferien}
    for d in sorted(ctx.plan.days):
        block = next((f for f in ctx.ferien if f.contains(d)), None)
        by_block[block.id if block else ""].append(d)
    result = []
    for f in ctx.ferien:
        days = by_block.get(f.id)
        if days:
            result.append({"name": names[f.id], "count": len(days), "days": ", ".join(d.strftime("%d.%m.") for d in days)})
    if by_block.get(""):
        days = by_block[""]
        result.append({"name": "außerhalb der Ferien", "count": len(days),
                       "days": ", ".join(d.strftime("%d.%m.") for d in days)})
    return result


def _klassenfahrt(db: Session, period: Period, as_of: date, ctx: ledger_service.LedgerContext) -> dict[str, Any]:
    trip_entries = [
        e for e in hashed_entries(db, period.start, period.end)
        if e.deleted_at is None and {"klassenfahrt", "nachtbereitschaft"} & set(e.flags or [])
    ]
    variants = []
    for variante in (KlassenfahrtVariante.A, KlassenfahrtVariante.B):
        vctx = ctx if ctx.klassenfahrt_variante == variante else ledger_service.load_context(
            db, period, today=as_of, klassenfahrt_variante=variante
        )
        variants.append(
            {
                "variante": variante.value,
                "label": "A – alle Stunden inkl. Nachtbereitschaft" if variante is KlassenfahrtVariante.A
                else "B – gekappt je Tag, ohne Nachtbereitschaft",
                "massgeblich": variante == ctx.klassenfahrt_variante,
                "ist_bisher": vctx.ist_bisher,
                "saldo_heute": vctx.saldo_heute,
                "prognose_ende": vctx.prognose_ende,
            }
        )
    return {
        "tage": len({e.day for e in trip_entries if "klassenfahrt" in (e.flags or [])}),
        "nachtbereitschaft": sum(int(e.duration_seconds or 0) for e in trip_entries
                                 if "nachtbereitschaft" in (e.flags or [])),
        "variants": variants,
    }


def year_report_data(
    db: Session,
    period: Period | str | None = None,
    *,
    today: date | None = None,
    now: datetime | None = None,
    include_entries: bool = True,
) -> dict[str, Any]:
    today = today or today_local()
    now = now or now_utc()
    if not isinstance(period, Period):
        period = cal.resolve_period(db, period, today)
    as_of = max(period.start, min(today, period.end))
    ctx = ledger_service.load_context(db, period, today=as_of)

    entries = hashed_entries(db, period.start, period.end)
    hash_value = range_hash(entries)
    month_rows = months_service.list_months(db, period, today=today)
    compare = ledger_service.compare_leave_modes(db, period, as_of)
    for item in compare:
        item["saldo_periodenende_hm"] = fmt_hm(item["saldo_periodenende"], plus=True)
    weeks = [w for w in ctx.weeks if w.monday <= as_of]
    return {
        "kind": "year",
        "title": f"Jahresbericht {period.label}",
        "css": Markup(report_css()),
        "head": _head(db, period, now),
        "period": period,
        "ctx": ctx,
        "as_of": as_of,
        "chart": saldo_chart_svg(ctx),
        "months": month_rows,
        "closed_months": sum(1 for m in month_rows if m.hash),
        "weeks": weeks,
        "categories": ctx.categories,
        "categories_total": sum(c.seconds for c in ctx.categories),
        "ferien": _ferien_rows(ctx),
        "leave": _leave_list(ctx),
        "leave_info": cal.leave_plan_info_from(
            cal.PeriodDays(period.id, period.start, period.end, ctx.days, ctx.plan, ctx.leave_mode,
                           ctx.ferien, ctx.holidays)
        ),
        "compare": compare,
        "klassenfahrt": _klassenfahrt(db, period, as_of, ctx),
        "mehrarbeit": [
            {"label": months_service.month_label(f"{y:04d}-{m:02d}"), "mm": mm}
            for (y, m), mm in sorted(ctx.mehrarbeit.items())
            if mm.total > 0
        ],
        "eu_hours_text": fmt_dec(ctx.eu.hours * 3600),
        "include_entries": include_entries,
        "entries": entry_rows(db, entries) if include_entries else [],
        "revisions": revision_rows(db, period.start, period.end) if include_entries else [],
        "hash": hash_value,
        "footer": _footer(hash_value, False),
        "leave_mode_manuell": ctx.leave_mode is LeaveMode.MANUELL,
    }


def render_year_report(
    db: Session,
    period: Period | str | None = None,
    *,
    today: date | None = None,
    now: datetime | None = None,
    include_entries: bool = True,
) -> str:
    """The year report of ``period`` (default: current) as self-contained HTML (Konzept §10.3)."""
    data = year_report_data(db, period, today=today, now=now, include_entries=include_entries)
    return _templates().get_template("reports/year.html").render(**data)
