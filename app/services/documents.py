"""Letter generator (Konzept §11, L7, Anhang A/B): Markdown templates filled from the account data.

Templates live in ``app/templates_docs/<type>.md`` and use ``{{placeholder}}`` markers (no logic).
Every rendered text starts with the non-removable notice :data:`NOTICE`; it is enforced here in
:func:`ensure_notice` (render, save and HTML conversion), not only in the template files.

Public API
----------
- :func:`prepare` – fill a template, returns :class:`Rendered` (markdown, values, warnings, range)
- :func:`render` – ``render(db, type, period_from, period_to, overrides) -> markdown``
- :func:`save` – store a :class:`~app.db.models.Document` (``rendered_md`` with enforced notice)
- :func:`mark_sent` / :func:`list_documents` / :func:`get_document`
- :func:`geltendmachung_wirksam_ab` – first day of the month after the first *sent* Geltendmachung
  (for the account screen: "Geltendmachung wirksam ab …"), or None
- :func:`to_html` – Markdown -> safe HTML (raw HTML in the source is neutralised)

Placeholders (all values are strings; numbers in German format, hours as decimal with 2 places):

=========================  ==================================================================
``name``, ``schule``       from settings (fallback "[Name]" / "[Schule]")
``ort``                    override only (fallback "[Ort]")
``datum``                  today, ``dd.mm.yyyy``
``adressat``               suggested recipient per type (override)
``dienstweg``              e.g. "auf dem Dienstweg über die Schulleitung der …" (override)
``abdruck``                e.g. "Personalrat" (override)
``periode_label``          account period label, e.g. "2026/27"
``zeitraum_von/_bis``      evaluated range, ``dd.mm.yyyy``
``soll_h``                 reference soll in the range (h)
``ist_h``                  account ist in the range (recorded + sick-day neutralisation)
``erfasst_h``              recorded time in the range (Klassenfahrt variant applied)
``saldo_periode_h``        ist − soll in the range, signed ("+83,25")
``saldo_periode_hm``       same as ``h:mm`` ("+83:15")
``saldo_kumuliert_h``      period saldo at the end of the range, signed
``schulwochen_avg_h``      Ø recorded hours per *full* school week in the range (ISO week inside
                           the range with 5 school days and no full-day absence except Fortbildung)
``schulwochen_anzahl``     number of those weeks
``schulwochen_ueber_48``   school weeks (≥ 1 school day) in the range with > 48 h recorded
``wochen_soll_h``          regular weekly hours × part-time factor
``upz``                    Unterrichtspflichtzeit (settings)
``eu_mittel_h``            12-month average (EU 48 h rule) at the end of the range
``eu_fenster_von/_bis``    window of that average
``kategorien_tabelle``     Markdown table: Tätigkeitsbereich | Stunden | Anteil
``belastungen_liste``      Markdown list: category with hours and share
``aufgaben_liste``         Markdown list of FS/KV/B tasks (subcategories) with hours
``monatsabschluesse``      closed months in the range with short hash, or "keine"
``monat``                  (antrag) month, e.g. "Oktober 2026"
``ustd_angeordnet``, ``ustd_genehmigt``, ``ustd_gesamt``, ``ustd_ausgleichsfaehig``, ``ustd_minuten``
``mehrarbeit_tabelle``     table rows Datum | Dauer | UStd | Grundlage
``freizeitausgleich_frist`` last day for Freizeitausgleich (end of the 3rd following month)
=========================  ==================================================================

Unknown placeholders render as ``[name?]`` so gaps are visible. Overrides may set any placeholder;
``monat`` (``YYYY-MM``) selects the month for the Mehrarbeit application.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import markdown as md_lib
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import PROJECT_ROOT
from app.core import account as core_account
from app.core.mehrarbeit import BAGATELLGRENZE_USTD, month_mehrarbeit, seconds_to_ustd
from app.core.types import FLAG_VERTRETUNG_ANGEORDNET, FLAG_VERTRETUNG_GENEHMIGT, AbsenceType, DayType
from app.db.models import Category, Document, DocumentType, EntryStatus, MonthClose, Period, TimeEntry
from app.services import calendar_service as cal
from app.services import ledger as ledger_service
from app.services.settings_service import get_settings, teilzeitfaktor
from app.util import fmt_hm, local_to_utc, now_utc, to_local, today_local

TEMPLATE_DIR = PROJECT_ROOT / "app" / "templates_docs"

NOTICE = (
    "Vorlage, keine Rechtsberatung. Vor Versand prüfen, idealerweise mit Personalrat oder "
    "Verbands-Rechtsschutz."
)
NOTICE_BLOCK = f"> **Hinweis:** {NOTICE}"

MONTH_NAMES = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)

TYPE_INFO: dict[str, dict[str, Any]] = {
    DocumentType.GELTENDMACHUNG.value: {
        "label": "Geltendmachung",
        "zweck": "Ausgleich ab dem Folgemonat sichern (schriftliche Geltendmachung)",
        "adressat": "Staatliches Schulamt [Landkreis/Stadt]",
        "dienstweg": "auf dem Dienstweg über die Schulleitung",
        "abdruck": "Personalrat",
        "uses_month": False,
    },
    DocumentType.UEBERLASTUNGSANZEIGE.value: {
        "label": "Überlastungsanzeige",
        "zweck": "Belastung anzeigen und um Priorisierung bitten",
        "adressat": "Staatliches Schulamt [Landkreis/Stadt]",
        "dienstweg": "auf dem Dienstweg über die Schulleitung",
        "abdruck": "Personalrat",
        "uses_month": False,
    },
    DocumentType.PRIORITAETSBITTE.value: {
        "label": "Bitte um Priorisierung",
        "zweck": "Nachfragen, welche Aufgaben zurückgestellt werden dürfen",
        "adressat": "Schulleitung",
        "dienstweg": "",
        "abdruck": "",
        "uses_month": False,
    },
    DocumentType.ANTRAG_FREIZEITAUSGLEICH.value: {
        "label": "Antrag Freizeitausgleich / Mehrarbeitsvergütung",
        "zweck": "Mehrarbeit im engen Sinn (angeordnete/genehmigte Unterrichtsstunden) ausgleichen",
        "adressat": "Schulleitung",
        "dienstweg": "zur Weiterleitung an das Staatliche Schulamt",
        "abdruck": "",
        "uses_month": True,
    },
}
TYPES = tuple(TYPE_INFO)

# Placeholder documentation (shown in the UI).
PLACEHOLDERS: dict[str, str] = {
    "name": "Name (Einstellungen)",
    "schule": "Schule (Einstellungen)",
    "ort": "Ort (Eingabe)",
    "datum": "Datum des Schreibens",
    "adressat": "Adressat (Vorschlag, änderbar)",
    "dienstweg": "Dienstweg (Vorschlag, änderbar)",
    "abdruck": "Abdruck an (Vorschlag, änderbar)",
    "periode_label": "Kontoperiode",
    "zeitraum_von": "Beginn des Zeitraums",
    "zeitraum_bis": "Ende des Zeitraums",
    "soll_h": "Referenz-Soll im Zeitraum (h)",
    "ist_h": "Angerechnete Ist-Zeit im Zeitraum (h)",
    "erfasst_h": "Erfasste Zeit im Zeitraum (h)",
    "saldo_periode_h": "Differenz Ist − Soll im Zeitraum (h, mit Vorzeichen)",
    "saldo_periode_hm": "Differenz im Zeitraum (h:mm)",
    "saldo_kumuliert_h": "Saldo der Kontoperiode am Ende des Zeitraums",
    "schulwochen_avg_h": "Ø Wochen-Ist in vollen Schulwochen (h)",
    "schulwochen_anzahl": "Anzahl voller Schulwochen",
    "schulwochen_ueber_48": "Schulwochen über 48 h",
    "wochen_soll_h": "Regelmäßige Wochenarbeitszeit (h)",
    "upz": "Unterrichtspflichtzeit",
    "eu_mittel_h": "12-Monats-Mittel (h/Woche)",
    "eu_fenster_von": "Beginn des 12-Monats-Fensters",
    "eu_fenster_bis": "Ende des 12-Monats-Fensters",
    "kategorien_tabelle": "Tabelle Tätigkeitsbereiche mit Stunden und Anteil",
    "belastungen_liste": "Liste der Belastungen (Kategorien mit Stunden)",
    "aufgaben_liste": "Aufgabenliste aus FS/KV/B",
    "monatsabschluesse": "Abgeschlossene Monate mit Prüf-Hash",
    "monat": "Monat der Mehrarbeit",
    "ustd_angeordnet": "UStd angeordnet",
    "ustd_genehmigt": "UStd genehmigt",
    "ustd_gesamt": "UStd insgesamt",
    "ustd_ausgleichsfaehig": "UStd ausgleichsfähig",
    "ustd_minuten": "Minuten je UStd",
    "mehrarbeit_tabelle": "Tabelle der Mehrarbeits-Einträge",
    "freizeitausgleich_frist": "Frist für Freizeitausgleich",
}
OVERRIDABLE = ("adressat", "dienstweg", "abdruck", "ort", "name", "schule", "datum")

AUFGABEN_CODES = ("FS", "KV", "B")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
MAX_MD_LENGTH = 100_000


class DocumentError(ValueError):
    """Invalid input (unknown type, bad range, no period); message is German UI text."""


class DocumentNotFound(LookupError):
    pass


@dataclass
class Rendered:
    type: str
    markdown: str
    values: dict[str, str]
    period_from: date | None
    period_to: date | None
    warnings: list[str] = field(default_factory=list)


# =========================================================================== formatting


def num_de(value: float, places: int = 2, *, plus: bool = False) -> str:
    value = round(float(value), places)
    if value == 0:
        value = 0.0
    text = f"{value:+,.{places}f}" if plus and value > 0 else f"{value:,.{places}f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def hours_de(seconds: int | float, *, plus: bool = False) -> str:
    return num_de(seconds / 3600, plus=plus)


def ustd_de(value: float) -> str:
    return num_de(value, 1)


def date_de(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


def month_label(ym: str) -> str:
    return f"{MONTH_NAMES[int(ym[5:7]) - 1]} {ym[:4]}"


def _md_cell(text: str) -> str:
    return str(text).replace("|", "/").replace("\n", " ").strip()


# =========================================================================== notice


def ensure_notice(text: str) -> str:
    """Return ``text`` starting with :data:`NOTICE_BLOCK` (exactly once, as the first block)."""
    text = (text or "").replace("\r\n", "\n")
    lines = [ln for ln in text.split("\n") if NOTICE not in ln and ln.strip() != "> **Hinweis:**"]
    body = "\n".join(lines).strip("\n")
    return f"{NOTICE_BLOCK}\n\n{body}\n" if body else f"{NOTICE_BLOCK}\n"


# =========================================================================== templates


def template_path(doc_type: str) -> Path:
    return TEMPLATE_DIR / f"{check_type(doc_type)}.md"


def check_type(doc_type: str) -> str:
    if doc_type not in TYPE_INFO:
        raise DocumentError(f"Unbekannter Schreiben-Typ „{doc_type}“.")
    return doc_type


def load_template(doc_type: str) -> str:
    return template_path(doc_type).read_text(encoding="utf-8")


def fill(template: str, values: dict[str, str]) -> str:
    def repl(m: re.Match[str]) -> str:
        key = m.group(1)
        return values[key] if key in values else f"[{key}?]"

    return PLACEHOLDER_RE.sub(repl, template)


# =========================================================================== data


def _period_and_range(
    db: Session, period_from: date | None, period_to: date | None, today: date
) -> tuple[Period, date, date, list[str]]:
    warnings: list[str] = []
    ref = period_to or period_from or today
    period = cal.period_for_date(db, ref) or cal.get_current_period(db, today)
    if period is None:
        raise DocumentError("Es ist noch kein Schuljahr angelegt (Kalender), daher fehlen die Kontodaten.")
    default_to = max(period.start, min(today - timedelta(days=1), period.end))
    start = period_from or period.start
    end = period_to or default_to
    if end < start:
        raise DocumentError("Das Ende des Zeitraums liegt vor dem Beginn.")
    if start < period.start:
        warnings.append(f"Der Zeitraum wurde auf den Beginn der Kontoperiode ({date_de(period.start)}) gekürzt.")
        start = period.start
    if end > period.end:
        warnings.append(f"Der Zeitraum wurde auf das Ende der Kontoperiode ({date_de(period.end)}) gekürzt.")
        end = period.end
    if end < start:  # the whole range lies outside the period (e.g. 2025 dates, period 2026/27)
        raise DocumentError(
            f"Der Zeitraum liegt außerhalb der Kontoperiode {period.label} "
            f"({date_de(period.start)} – {date_de(period.end)})."
        )
    if end > today:
        warnings.append("Der Zeitraum reicht in die Zukunft; Werte enthalten nur bisher erfasste Zeiten.")
    return period, start, end, warnings


def _full_school_week_rows(rows: list[Any]) -> list[list[Any]]:
    """Ledger rows of each ISO week with 5 school days and no full-day absence (except Fortbildung)."""
    weeks: dict[tuple[int, int], list[Any]] = defaultdict(list)
    for r in rows:
        weeks[r.date.isocalendar()[:2]].append(r)
    result = []
    for items in weeks.values():
        school = [r for r in items if r.day_type == DayType.SCHULTAG]
        if len(school) < 5:
            continue
        if any(r.absence is not None and r.absence != AbsenceType.FORTBILDUNG for r in school):
            continue
        result.append(items)
    return result


def _full_school_weeks(rows: list[Any]) -> list[int]:
    """Recorded seconds of each full school week (see :func:`_full_school_week_rows`)."""
    return [sum(r.ist_erfasst for r in items) for items in _full_school_week_rows(rows)]


def _category_week_seconds(db: Session, ctx: Any, days: set[date]) -> dict[int, int]:
    """Recorded seconds per main category on ``days`` - on the same basis as ``ist_erfasst`` (Klassenfahrt
    variant applied, pseudo categories dropped), so the per-category means add up to the weekly mean."""
    if not days:
        return {}
    cats = {c.id: c for c in db.scalars(select(Category))}
    entries = [e for e in ledger_service.load_entries(db, min(days), max(days), cats) if e.day in days]
    per_cat: dict[int, int] = defaultdict(int)
    for row in ledger_service.evaluated_rows(entries, cats, ctx.ist_by_day, ctx.klassenfahrt_variante):
        per_cat[row.category_id] += row.seconds
    return dict(per_cat)


def _category_hours(db: Session, start: date, end: date) -> list[tuple[Category, int]]:
    cats = {c.id: c for c in db.scalars(select(Category))}
    per_cat: dict[int, int] = defaultdict(int)
    for row in ledger_service.load_entries(db, start, end, cats):
        cat = cats.get(row.category_id)
        if cat is None or cat.is_pseudo:
            continue
        per_cat[row.category_id] += row.seconds
    return sorted(((cats[cid], s) for cid, s in per_cat.items() if s > 0), key=lambda x: (x[0].sort, x[0].code))


def _category_table(items: list[tuple[Category, int]]) -> str:
    total = sum(s for _, s in items)
    if not total:
        return "_Im Zeitraum sind keine Einträge erfasst._"
    lines = ["| Tätigkeitsbereich | Stunden | Anteil |", "|---|---:|---:|"]
    for cat, secs in items:
        lines.append(f"| {_md_cell(cat.name)} | {hours_de(secs)} | {num_de(100 * secs / total, 1)} % |")
    lines.append(f"| **Summe** | **{hours_de(total)}** | 100 % |")
    return "\n".join(lines)


def _burden_list(items: list[tuple[Category, int]], week_seconds: dict[int, int], n_weeks: int) -> str:
    """Categories with hours and share. The mean per *full* school week uses the same weeks as
    ``schulwochen_avg_h``, so the per-category means add up to the stated weekly mean (audit fix)."""
    total = sum(s for _, s in items)
    if not total:
        return "- (im Zeitraum sind keine Einträge erfasst)"
    lines = []
    for cat, secs in sorted(items, key=lambda x: -x[1]):
        per_week = (f", Ø {hours_de(week_seconds.get(cat.id, 0) / n_weeks)} h je voller Schulwoche"
                    if n_weeks else "")
        lines.append(f"- {_md_cell(cat.name)}: {hours_de(secs)} h ({num_de(100 * secs / total, 1)} %{per_week})")
    if n_weeks:
        lines.append(f"- _Die Werte „je voller Schulwoche“ beziehen sich auf dieselben {n_weeks} vollen Schulwochen "
                     "wie die durchschnittliche Wochenarbeitszeit oben und ergeben zusammen (gerundet) diesen Wert; "
                     "Stunden und Anteile gelten für den ganzen Zeitraum einschließlich Ferien._")
    return "\n".join(lines)


def _task_list(db: Session, start: date, end: date) -> str:
    cats = {c.id: c for c in db.scalars(select(Category))}
    mains = {c.code: c for c in cats.values() if c.parent_id is None and c.code in AUFGABEN_CODES}
    if not mains:
        return "- [Aufgaben ergänzen]"
    stmt = select(TimeEntry.category_id, TimeEntry.subcategory_id, TimeEntry.duration_seconds).where(
        TimeEntry.day.between(start, end),
        TimeEntry.status == EntryStatus.ACTIVE.value,
        TimeEntry.deleted_at.is_(None),
        TimeEntry.end_at.is_not(None),
    )
    main_ids = {c.id: c for c in mains.values()}
    per_main: dict[int, int] = defaultdict(int)
    per_sub: dict[int, dict[int | None, int]] = defaultdict(lambda: defaultdict(int))
    for cid, sid, secs in db.execute(stmt):
        cat = cats.get(cid)
        main_id = cid
        if cat is not None and cat.parent_id is not None:  # subcategory stored as category
            sid, main_id = cid, cat.parent_id
        if main_id not in main_ids:
            continue
        per_main[main_id] += int(secs or 0)
        per_sub[main_id][sid] += int(secs or 0)
    lines = []
    for code in AUFGABEN_CODES:
        main = mains.get(code)
        if main is None:
            continue
        if per_main.get(main.id, 0) <= 0:
            continue  # nothing recorded in this area: not a task to prioritise
        lines.append(f"- {_md_cell(main.name)}: {hours_de(per_main.get(main.id, 0))} h")
        subs = per_sub.get(main.id, {})
        for sid, secs in sorted(subs.items(), key=lambda x: -x[1]):
            if sid is None or secs <= 0 or sid not in cats:
                continue
            lines.append(f"    - {_md_cell(cats[sid].name)}: {hours_de(secs)} h")
    if not lines:  # nothing recorded: offer the areas as a starting point
        lines = [f"- {_md_cell(mains[c].name)}: [Aufgaben ergänzen]" for c in AUFGABEN_CODES if c in mains]
    lines.append("- [weitere Aufgaben ergänzen]")
    return "\n".join(lines)


def _closed_months(db: Session, start: date, end: date) -> str:
    rows = db.scalars(select(MonthClose).order_by(MonthClose.year_month)).all()
    lo, hi = f"{start:%Y-%m}", f"{end:%Y-%m}"
    items = [
        f"{month_label(r.year_month)} (Prüf-Hash {r.entries_hash_sha256[:12]}…)"
        for r in rows
        if lo <= r.year_month <= hi
    ]
    return "; ".join(items) if items else "keine"


def _base_values(db: Session, doc_type: str, today: date) -> dict[str, str]:
    settings = get_settings(db)
    info = TYPE_INFO[doc_type]
    schule = (settings.schule or "").strip()
    dienstweg = info["dienstweg"]
    adressat = info["adressat"]
    if schule and doc_type != DocumentType.ANTRAG_FREIZEITAUSGLEICH.value and dienstweg:
        dienstweg = f"{dienstweg} der {schule}"
    if adressat == "Schulleitung" and schule:
        adressat = f"Schulleitung der {schule}"
    f = teilzeitfaktor(settings)
    upz = float(settings.upz_ist or 0)
    return {
        "name": (settings.name or "").strip() or "[Name]",
        "schule": schule or "[Schule]",
        "ort": "[Ort]",
        "datum": date_de(today),
        "adressat": adressat,
        "dienstweg": dienstweg,
        "abdruck": info["abdruck"],
        "upz": num_de(upz, 1).removesuffix(",0"),
        "wochen_soll_h": num_de(float(settings.wochen_soll_voll_h or 40.0) * f, 2).removesuffix(",00"),
        "ustd_minuten": str(int(settings.unterrichtsstunde_min or 45)),
    }


def _account_values(db: Session, period: Period, start: date, end: date) -> dict[str, str]:
    ctx = ledger_service.load_context(db, period, today=end)
    rows = [r for r in ctx.ledger if start <= r.date <= end]
    soll = sum(r.soll for r in rows)
    ist = sum(r.ist for r in rows)
    erfasst = sum(r.ist_erfasst for r in rows)
    week_rows = _full_school_week_rows(rows)
    weeks = [sum(r.ist_erfasst for r in items) for items in week_rows]
    avg = (sum(weeks) / len(weeks)) if weeks else None
    week_days = {r.date for items in week_rows for r in items}
    cats = _category_hours(db, start, end)
    return {
        "periode_label": period.label,
        "zeitraum_von": date_de(start),
        "zeitraum_bis": date_de(end),
        "soll_h": hours_de(soll),
        "ist_h": hours_de(ist),
        "erfasst_h": hours_de(erfasst),
        "saldo_periode_h": hours_de(ist - soll, plus=True),
        "saldo_periode_hm": fmt_hm(ist - soll, plus=True),
        "saldo_kumuliert_h": hours_de(ctx.saldo_at(end), plus=True),
        "schulwochen_avg_h": hours_de(avg) if avg is not None else "–",
        "schulwochen_anzahl": str(len(weeks)),
        "schulwochen_ueber_48": str(core_account.schulwochen_ueber(rows, 48)),
        "eu_mittel_h": num_de(ctx.eu.hours),
        "eu_fenster_von": date_de(ctx.eu.window_start),
        "eu_fenster_bis": date_de(ctx.eu.window_end),
        "kategorien_tabelle": _category_table(cats),
        "belastungen_liste": _burden_list(cats, _category_week_seconds(db, ctx, week_days), len(weeks)),
        "aufgaben_liste": _task_list(db, start, end),
        "monatsabschluesse": _closed_months(db, start, end),
    }


def _parse_ym(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-\d{2}", value or "") or not 1 <= int(value[5:7]) <= 12:
        raise DocumentError("Monat bitte als JJJJ-MM angeben.")
    start = date(int(value[:4]), int(value[5:7]), 1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return start, end


def default_month(today: date) -> str:
    prev = today.replace(day=1) - timedelta(days=1)
    return f"{prev:%Y-%m}"


def _mehrarbeit_values(db: Session, ym: str) -> tuple[dict[str, str], list[str], date, date]:
    from app.services.months import freizeitausgleich_frist

    start, end = _parse_ym(ym)
    settings = get_settings(db)
    unit = int(settings.unterrichtsstunde_min or 45)
    cats = {c.id: c for c in db.scalars(select(Category))}

    def is_teaching(cid: int) -> bool:
        cat = cats.get(cid)
        depth = 0
        while cat is not None and cat.parent_id is not None and depth < 5:
            cat, depth = cats.get(cat.parent_id), depth + 1
        return bool(cat is not None and cat.is_unterricht)

    entries = db.scalars(
        select(TimeEntry)
        .where(
            TimeEntry.day.between(start, end),
            TimeEntry.status == EntryStatus.ACTIVE.value,
            TimeEntry.deleted_at.is_(None),
            TimeEntry.end_at.is_not(None),
        )
        .order_by(TimeEntry.start_at)
    ).all()
    tuples, table = [], []
    for e in entries:
        flags = set(e.flags or [])
        if not is_teaching(e.category_id) or not flags & {FLAG_VERTRETUNG_ANGEORDNET, FLAG_VERTRETUNG_GENEHMIGT}:
            continue
        secs = int(e.duration_seconds or 0)
        tuples.append((e.day, secs, tuple(flags)))
        basis = "angeordnet" if FLAG_VERTRETUNG_ANGEORDNET in flags else "genehmigt"
        zeit = (
            f"{to_local(e.start_at):%H:%M}–{to_local(e.end_at):%H:%M}" if e.has_bounds and e.end_at else fmt_hm(secs)
        )
        table.append(f"| {date_de(e.day)} | {zeit} | {ustd_de(seconds_to_ustd(secs, unit))} | {basis} |")
    mm = month_mehrarbeit(tuples, unit)
    warnings = []
    if not tuples:
        warnings.append("Für diesen Monat sind keine Unterrichtseinträge mit „Vertretung angeordnet/genehmigt“ erfasst.")
    elif mm.total <= BAGATELLGRENZE_USTD:
        warnings.append(
            f"Im Monat sind {ustd_de(mm.total)} UStd erfasst – nicht mehr als drei Unterrichtsstunden "
            "(Bagatellgrenze). Bitte prüfen, ob ein Antrag sinnvoll ist."
        )
    values = {
        "monat": month_label(ym),
        "zeitraum_von": date_de(start),
        "zeitraum_bis": date_de(end),
        "ustd_angeordnet": ustd_de(mm.ustd_angeordnet),
        "ustd_genehmigt": ustd_de(mm.ustd_genehmigt),
        "ustd_gesamt": ustd_de(mm.total),
        "ustd_ausgleichsfaehig": ustd_de(mm.ausgleichsfaehig),
        "mehrarbeit_tabelle": "\n".join(table) or "| – | – | 0 | – |",
        "freizeitausgleich_frist": date_de(freizeitausgleich_frist(ym)),
    }
    return values, warnings, start, end


# =========================================================================== render / save


def prepare(
    db: Session,
    doc_type: str,
    period_from: date | None = None,
    period_to: date | None = None,
    overrides: dict[str, Any] | None = None,
    *,
    today: date | None = None,
) -> Rendered:
    """Fill the template of ``doc_type`` from the account data (see module docstring)."""
    check_type(doc_type)
    today = today or today_local()
    overrides = {k: str(v).strip() for k, v in (overrides or {}).items() if v is not None}
    values = _base_values(db, doc_type, today)
    warnings: list[str] = []

    if TYPE_INFO[doc_type]["uses_month"]:
        ym = overrides.get("monat") or (f"{period_from:%Y-%m}" if period_from else default_month(today))
        mvalues, mwarn, start, end = _mehrarbeit_values(db, ym)
        values.update(mvalues)
        warnings.extend(mwarn)
    else:
        period, start, end, rwarn = _period_and_range(db, period_from, period_to, today)
        warnings.extend(rwarn)
        values.update(_account_values(db, period, start, end))
        if values["saldo_periode_h"].startswith("-"):
            warnings.append(
                "Die Differenz zum Referenz-Soll ist im gewählten Zeitraum negativ. Bitte Zeitraum und "
                "Wortlaut prüfen – Ferienwerktage bauen das Konto planmäßig ab."
            )

    for key, value in overrides.items():
        if key == "monat":
            continue
        if key in OVERRIDABLE or key in PLACEHOLDERS:
            values[key] = value
    for key in ("dienstweg", "abdruck"):
        if values.get(key, "").strip() == "-":  # "-" = leave out (the web form treats "" as "use suggestion")
            values[key] = ""
        values.setdefault(key, "")
    text = fill(load_template(doc_type), values)
    if not values.get("abdruck"):
        text = re.sub(r"\n+Abdruck: *\n", "\n", text)
    if values.get("ort") == "[Ort]":
        warnings.append("Ort fehlt noch (Feld „Ort“).")
    if "[Name]" in values.get("name", ""):
        warnings.append("Name ist in den Einstellungen nicht hinterlegt.")
    return Rendered(doc_type, ensure_notice(text), values, start, end, warnings)


def render(
    db: Session,
    doc_type: str,
    period_from: date | None = None,
    period_to: date | None = None,
    overrides: dict[str, Any] | None = None,
    *,
    today: date | None = None,
) -> str:
    """Rendered Markdown of ``doc_type`` (always starting with the notice)."""
    return prepare(db, doc_type, period_from, period_to, overrides, today=today).markdown


def save(
    db: Session,
    doc_type: str,
    rendered_md: str,
    *,
    period_from: date | None = None,
    period_to: date | None = None,
    params: dict[str, Any] | None = None,
    note: str | None = None,
    commit: bool = True,
) -> Document:
    """Store a generated letter. The notice is enforced; ``rendered_md`` may be user-edited."""
    check_type(doc_type)
    if len(rendered_md or "") > MAX_MD_LENGTH:
        raise DocumentError("Der Text ist zu lang.")
    if not (rendered_md or "").strip():
        raise DocumentError("Der Text ist leer.")
    doc = Document(
        type=doc_type,
        period_from=period_from,
        period_to=period_to,
        params_json={k: v for k, v in (params or {}).items() if isinstance(v, str | int | float)},
        rendered_md=ensure_notice(rendered_md),
        note=(note or "").strip()[:2000] or None,
    )
    db.add(doc)
    if commit:
        db.commit()
        db.refresh(doc)
    return doc


def create(
    db: Session,
    doc_type: str,
    period_from: date | None = None,
    period_to: date | None = None,
    overrides: dict[str, Any] | None = None,
    *,
    text: str | None = None,
    note: str | None = None,
    today: date | None = None,
) -> tuple[Document, Rendered]:
    """Render and save in one step (``text`` replaces the rendered text, e.g. after editing)."""
    result = prepare(db, doc_type, period_from, period_to, overrides, today=today)
    doc = save(
        db,
        doc_type,
        text if text and text.strip() else result.markdown,
        period_from=result.period_from,
        period_to=result.period_to,
        params=result.values,
        note=note,
    )
    return doc, result


def list_documents(db: Session, doc_type: str | None = None) -> list[Document]:
    stmt = select(Document).order_by(Document.created_at.desc(), Document.id.desc())
    if doc_type:
        stmt = stmt.where(Document.type == check_type(doc_type))
    return list(db.scalars(stmt))


def get_document(db: Session, doc_id: str) -> Document:
    doc = db.get(Document, doc_id)
    if doc is None:
        raise DocumentNotFound("Schreiben nicht gefunden.")
    return doc


def mark_sent(db: Session, doc_id: str, sent_on: date | datetime | None = None, *, commit: bool = True) -> Document:
    """Set ``sent_at`` (now, or local noon of ``sent_on``)."""
    doc = get_document(db, doc_id)
    if isinstance(sent_on, datetime):
        doc.sent_at = sent_on
    elif isinstance(sent_on, date):
        if sent_on > today_local():
            raise DocumentError("Das Versanddatum liegt in der Zukunft.")
        doc.sent_at = local_to_utc(sent_on, time(12, 0))
    else:
        doc.sent_at = now_utc().replace(microsecond=0)
    if commit:
        db.commit()
        db.refresh(doc)
    return doc


def sent_date(doc: Document) -> date | None:
    return to_local(doc.sent_at).date() if doc.sent_at else None


def first_of_next_month(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def geltendmachung_wirksam_ab(db: Session) -> date | None:
    """First day of the month after the earliest *sent* Geltendmachung (Konzept §11), else None."""
    first = db.scalar(
        select(Document.sent_at)
        .where(Document.type == DocumentType.GELTENDMACHUNG.value, Document.sent_at.is_not(None))
        .order_by(Document.sent_at)
        .limit(1)
    )
    return first_of_next_month(to_local(first).date()) if first else None


def document_dict(doc: Document) -> dict[str, Any]:
    return {
        "id": doc.id,
        "type": doc.type,
        "type_label": TYPE_INFO.get(doc.type, {}).get("label", doc.type),
        "created_at": doc.created_at,
        "period_from": doc.period_from,
        "period_to": doc.period_to,
        "sent_at": doc.sent_at,
        "sent_on": sent_date(doc),
        "note": doc.note,
        "rendered_md": doc.rendered_md,
    }


def download_name(doc: Document) -> str:
    created = to_local(doc.created_at).date() if doc.created_at else today_local()
    return f"{doc.type}_{created:%Y-%m-%d}.md"


# =========================================================================== HTML


def to_html(text: str) -> Markup:
    """Markdown -> HTML for preview/print. Raw HTML is neutralised (``<`` escaped); notice enforced."""
    source = ensure_notice(text).replace("&", "&amp;").replace("<", "&lt;")
    html = md_lib.markdown(source, extensions=["tables", "sane_lists", "nl2br"], output_format="html")
    return Markup(html)
