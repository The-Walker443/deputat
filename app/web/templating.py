"""Shared Jinja2 environment for all HTML routers.

Usage in a router::

    from app.web.templating import templates, flash

    @router.get("/woche")
    def week(request: Request, ...):
        return templates.TemplateResponse(request, "week.html", {"rows": rows})

Filters: ``hm`` (seconds -> h:mm, ``|hm(plus=True)`` for signed saldo), ``dec`` (decimal hours),
``date_de`` (29.10.2026), ``date_short`` (29.10.), ``datetime_de`` (29.10.2026 14:12, local time),
``time_de`` (14:12, local time), ``weekday_de`` (Do / ``weekday_de(long=True)`` -> Donnerstag),
``month_de`` (Oktober 2026 from date or 'YYYY-MM').
Globals: ``csrf_token(request)``, ``app_version``, ``rules_version``, ``nav_items``, ``more_items``,
``pop_flashes(request)``, ``is_active(request, item)``, ``static_url(path)`` (``/static/<path>?v=<content
hash>``, see :mod:`app.web.assets`), ``base_url()`` (BASE_URL without trailing slash).
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import APP_VERSION, PROJECT_ROOT, RULES_VERSION, get_config
from app.security import get_csrf_token
from app.util import fmt_dec, fmt_hm, to_local
from app.web.assets import static_url

TEMPLATE_DIR = PROJECT_ROOT / "app" / "web" / "templates"

WEEKDAYS_SHORT = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
WEEKDAYS_LONG = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
MONTHS = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)

# Bottom tab bar. ``match`` lists path prefixes that mark the tab active.
NAV_ITEMS = (
    {"href": "/heute", "label": "Heute", "icon": "clock", "match": ("/heute",)},
    {"href": "/woche", "label": "Woche", "icon": "week", "match": ("/woche",)},
    {"href": "/konto", "label": "Konto", "icon": "chart", "match": ("/konto",)},
    {"href": "/jahr", "label": "Jahr", "icon": "calendar", "match": ("/jahr",)},
    {
        "href": "/mehr",
        "label": "Mehr",
        "icon": "menu",
        "match": ("/mehr", "/eintraege", "/kalender", "/stundenplan", "/monate", "/berichte", "/import",
                  "/export", "/schreiben", "/einstellungen"),
    },
)

# Secondary screens listed on /mehr.
MORE_ITEMS = (
    {"href": "/eintraege", "label": "Einträge", "icon": "list", "hint": "Alle Zeiteinträge, Historie, Bearbeiten"},
    {"href": "/kalender", "label": "Kalender", "icon": "calendar", "hint": "Ferien, Feiertage, Urlaub, Abwesenheiten"},
    {"href": "/stundenplan", "label": "Stundenplan", "icon": "grid", "hint": "Wochenraster und Vorschläge"},
    {"href": "/monate", "label": "Monate & Berichte", "icon": "file", "hint": "Monatsabschluss, Berichte, Exporte"},
    {"href": "/import", "label": "Import", "icon": "upload", "hint": "Bestehende Aufzeichnungen übernehmen"},
    {"href": "/schreiben", "label": "Schreiben", "icon": "pen", "hint": "Vorlagen für Geltendmachung & Co."},
    {"href": "/einstellungen", "label": "Einstellungen", "icon": "settings", "hint": "UPZ, Kategorien, Backup, Zugang"},
)


def _as_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return to_local(value).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def date_de(value) -> str:
    d = _as_date(value)
    return d.strftime("%d.%m.%Y") if d else ""


def date_short(value) -> str:
    d = _as_date(value)
    return d.strftime("%d.%m.") if d else ""


def datetime_de(value: datetime | None) -> str:
    return to_local(value).strftime("%d.%m.%Y %H:%M") if value else ""


def time_de(value: datetime | None) -> str:
    return to_local(value).strftime("%H:%M") if value else ""


def weekday_de(value, long: bool = False) -> str:
    d = _as_date(value)
    if d is None:
        return ""
    return (WEEKDAYS_LONG if long else WEEKDAYS_SHORT)[d.weekday()]


def month_de(value) -> str:
    if isinstance(value, str) and len(value) == 7:  # 'YYYY-MM'
        year, month = int(value[:4]), int(value[5:])
    else:
        d = _as_date(value)
        if d is None:
            return ""
        year, month = d.year, d.month
    return f"{MONTHS[month - 1]} {year}"


def is_active(request: Request, item: dict) -> bool:
    path = request.url.path
    return any(path == prefix or path.startswith(prefix + "/") for prefix in item["match"])


# --------------------------------------------------------------------------- flash messages


def flash(request: Request, message: str, kind: str = "info") -> None:
    """Queue a message for the next rendered page. kind: info | success | warn | danger."""
    request.session.setdefault("_flashes", [])
    request.session["_flashes"] = [*request.session["_flashes"], {"message": message, "kind": kind}]


def pop_flashes(request: Request) -> list[dict]:
    if "session" not in request.scope:
        return []
    return request.session.pop("_flashes", [])


def _csrf_token(request: Request) -> str:
    if "session" not in request.scope:
        return ""
    return get_csrf_token(request)


AMPEL_DE = {"gruen": "grün", "gelb": "gelb", "rot": "rot"}


def ampel_de(value) -> str:
    """'gruen' -> 'grün' (enum values are ASCII, the UI and reports are German)."""
    raw = str(getattr(value, "value", value) or "")
    return AMPEL_DE.get(raw, raw)


def cron_de(expr: str | None) -> str:
    """A daily cron expression ("30 0 * * *") as plain German ("jede Nacht um 00:30 Uhr")."""
    parts = str(expr or "").split()
    if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit() and parts[2:] == ["*", "*", "*"]:
        hour, minute = int(parts[1]), int(parts[0])
        when = "jede Nacht" if hour < 6 else "jeden Tag"
        return f"{when} um {hour:02d}:{minute:02d} Uhr"
    return "nach Zeitplan" if parts else ""


def _base_url() -> str:
    return get_config().base_url


def _build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        hm=fmt_hm,
        dec=fmt_dec,
        date_de=date_de,
        date_short=date_short,
        datetime_de=datetime_de,
        time_de=time_de,
        weekday_de=weekday_de,
        month_de=month_de,
        ampel_de=ampel_de,
        cron_de=cron_de,
    )
    env.globals.update(
        csrf_token=_csrf_token,
        pop_flashes=pop_flashes,
        is_active=is_active,
        app_version=APP_VERSION,
        rules_version=RULES_VERSION,
        nav_items=NAV_ITEMS,
        more_items=MORE_ITEMS,
        static_url=static_url,
        base_url=_base_url,
    )
    return env


templates = Jinja2Templates(env=_build_env())
