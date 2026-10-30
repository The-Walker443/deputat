"""HTML – Screen 'Konto' (Konzept §9.1, §6.4–§6.5, §1.2): saldo chart, tiles, Ferien-Guthaben-Rechner,
leave-mode comparison and Klassenfahrt variant switch.

Routes (mounted by app.main without prefix, no login):

- ``GET  /konto``                        full page (``?urlaub=<mode>&klassenfahrt=A|B`` = display switches)
- ``GET  /konto/daten.json``             chart data (saldo line, forecast, ferien bands, leave days,
                                          holidays, Geltendmachung marker), read by ``static/js/charts.js``
- ``GET  /konto/teil/kacheln``           tiles partial (HTMX, refreshed on ``deputat-konto-changed``)
- ``GET  /konto/teil/ferienrechner``     Ferien-Guthaben-Rechner partial
- ``POST /konto/ferienrechner``          save "geplante Ferienarbeit" (settings.ferienarbeit_schaetzung);
                                          HTMX -> partial + ``HX-Trigger: deputat-konto-changed``,
                                          plain form -> redirect to /konto
- ``GET  /konto/teil/urlaubsvergleich``  both leave modes side by side (lazy-loaded, display only)

The display switches never change the settings: the configured leave mode and Klassenfahrt variant
stay authoritative ("maßgeblich") for reports. All figures come from :mod:`app.services.ledger`.

Shared helpers for the screens Woche/Jahr live here too (:func:`load_ctx`, :func:`benchmarks`,
:func:`day_label`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.types import KlassenfahrtVariante, LeaveMode
from app.db.models import Document, DocumentType
from app.db.session import get_db
from app.util import fmt_dec, fmt_hm, to_local, today_local
from app.web.templating import MONTHS, flash, templates
from app.web.today import ABSENCE_LABELS, DAY_TYPE_LABELS

log = logging.getLogger("app.web.account")

router = APIRouter()

NO_PERIOD_TEXT = "Noch kein Schuljahr angelegt"
ZERO_LINE_LABEL = "Referenz 40h-Job"
BENCHMARK_LABEL = "Studienwert, nicht Norm"

LEAVE_MODE_SHORT = {
    LeaveMode.SOMMERFERIEN_ZUERST.value: "Sommerferien zuerst",
    LeaveMode.PROPORTIONAL.value: "Proportional",
    LeaveMode.MANUELL.value: "Manuell",
}
VARIANTE_LABELS = {
    KlassenfahrtVariante.A.value: "A – alle Stunden inkl. Nachtbereitschaft",
    KlassenfahrtVariante.B.value: "B – gekappt, ohne Nachtbereitschaft",
}
VARIANTE_SHORT = {KlassenfahrtVariante.A.value: "A · alle Stunden", KlassenfahrtVariante.B.value: "B · gekappt"}
AMPEL_LABELS = {"gruen": "grün", "gelb": "gelb", "rot": "rot"}


# =========================================================================== shared helpers


@dataclass
class CtxResult:
    """Outcome of :func:`load_ctx`: ``ctx`` or a state the template can render gracefully."""

    ctx: Any = None  # app.services.ledger.LedgerContext
    state: str = "ok"  # ok | no_period | error
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.ctx is not None


def load_ctx(
    db: Session,
    *,
    period: Any = None,
    today: date | None = None,
    leave_mode: str | None = None,
    klassenfahrt_variante: str | None = None,
) -> CtxResult:
    """``ledger.load_context`` that never raises: no period -> ``no_period``, other failures -> ``error``."""
    try:
        from app.services import calendar_service as cal
        from app.services import ledger as ledger_service
    except Exception:  # pragma: no cover - services are built concurrently
        log.exception("Ledger-Service nicht verfügbar")
        return CtxResult(state="error", message="Kennzahlen sind gerade nicht verfügbar.")
    try:
        ctx = ledger_service.load_context(
            db,
            period,
            today=today,
            leave_mode=leave_mode,
            klassenfahrt_variante=klassenfahrt_variante,
        )
        return CtxResult(ctx=ctx)
    except (cal.NoPeriodError, cal.NotFoundError):
        db.rollback()
        return CtxResult(state="no_period", message=NO_PERIOD_TEXT)
    except Exception:
        log.exception("Kontodaten konnten nicht berechnet werden")
        db.rollback()
        return CtxResult(state="error", message="Kennzahlen sind gerade nicht verfügbar.")


def view_params(request: Request) -> tuple[str | None, str | None]:
    """Display switches from the query string (invalid values are ignored)."""
    q = request.query_params
    mode = q.get("urlaub") or None
    if mode not in {m.value for m in LeaveMode}:
        mode = None
    variante = (q.get("klassenfahrt") or "").upper() or None
    if variante not in {v.value for v in KlassenfahrtVariante}:
        variante = None
    return mode, variante


def view_query(mode: str | None, variante: str | None) -> str:
    parts = []
    if mode:
        parts.append(f"urlaub={mode}")
    if variante:
        parts.append(f"klassenfahrt={variante}")
    return "&".join(parts)


def benchmarks() -> list[dict[str, Any]]:
    """Optional study reference values (Konzept §9.4) from ``app.services.benchmarks`` if present."""
    try:
        from app.services import benchmarks as bm  # type: ignore[attr-defined]
    except ImportError:
        return []
    try:
        items = bm.load_benchmarks() or []
    except Exception:
        log.exception("Benchmarks konnten nicht geladen werden")
        return []
    result = []
    for item in items:
        try:
            get = item.get if isinstance(item, dict) else (lambda k, _i=item: getattr(_i, k, None))
            hours = float(get("hours_per_week"))
            result.append(
                {
                    "label": str(get("label") or ""),
                    "hours_per_week": hours,
                    "hours_text": fmt_hm(hours * 3600),
                    "source": str(get("source") or ""),
                    "year": str(get("year") or ""),
                    "kind": str(get("kind") or ""),
                }
            )
        except (TypeError, ValueError):
            continue
    return result


def day_label(day: Any, ctx: Any) -> str:
    """'Ferientag · Herbstferien · Urlaub' for a core ``Day``."""
    label = DAY_TYPE_LABELS.get(str(day.day_type.value), str(day.day_type.value))
    if day.holiday_name:
        label = day.holiday_name
    elif day.ferien_id and ctx is not None:
        ferien = ctx.ferien_by_id(day.ferien_id)
        if ferien is not None:
            label = f"{label} · {ferien.name}"
    if day.absence:
        label = f"{label} · {ABSENCE_LABELS.get(day.absence.value, day.absence.value)}"
    return label


def _first_of_next_month(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def geltendmachung(db: Session) -> dict[str, Any] | None:
    """Earliest sent Geltendmachung: claims count from the following month (Konzept §1.1). Read-only."""
    try:
        doc = db.scalars(
            select(Document)
            .where(Document.type == DocumentType.GELTENDMACHUNG.value, Document.sent_at.is_not(None))
            .order_by(Document.sent_at)
        ).first()
    except Exception:  # documents feature is built concurrently
        log.exception("Dokumente konnten nicht gelesen werden")
        db.rollback()
        return None
    if doc is None or doc.sent_at is None:
        return None
    sent = to_local(doc.sent_at).date()
    return {"sent": sent, "wirksam_ab": _first_of_next_month(sent)}


def hours(seconds: int | float) -> float:
    return round(float(seconds) / 3600, 2)


# =========================================================================== view model


def _ampel(ctx: Any) -> str:
    value = getattr(ctx.eu.ampel, "value", ctx.eu.ampel)
    return str(value)


def tiles(ctx: Any) -> dict[str, Any]:
    week = ctx.week_of(ctx.today) if ctx.period.start <= ctx.today <= ctx.period.end else None
    week_ist = int(week.ist) if week else None
    pct = None
    if week_ist is not None and ctx.w_max:
        pct = int(round(100 * week_ist / ctx.w_max))
    ampel = _ampel(ctx)
    return {
        "saldo": ctx.saldo_heute,
        "saldo_tage": ctx.saldo_tage,
        "saldo_tage_text": f"{ctx.saldo_tage:+.1f}".replace(".", ",") if ctx.saldo_heute else "0,0",
        "prognose": ctx.prognose_ende,
        "period_end": ctx.period.end,
        "j_soll": ctx.j_soll,
        "soll_bisher": ctx.soll_bisher,
        "ist_bisher": ctx.ist_bisher,
        "ferienarbeit_bisher": ctx.ferienarbeit_bisher,
        "ferienarbeit_geplant": ctx.ferienarbeit_geplant,
        "w_max": ctx.w_max,
        "week_ist": week_ist,
        "week_pct": pct,
        "week_schulwoche": bool(week and week.is_schulwoche),
        "eu_hours_text": fmt_dec(ctx.eu.hours * 3600, places=1),
        "eu_ampel": ampel,
        "eu_ampel_label": AMPEL_LABELS.get(ampel, ampel),
        "eu_window_start": ctx.eu.window_start,
        "eu_since": getattr(ctx, "eu_daten_ab", None) or ctx.eu.window_start,
        "eu_since_datenbeginn": getattr(ctx, "eu_daten_ab", None) is not None,
        "uebertrag": getattr(ctx, "uebertrag", None),
        "uebertrag_label": getattr(ctx, "uebertrag_label", None),
        "uebertrag_verrechnet": bool(getattr(ctx, "uebertrag_verrechnet", False)),
        "erfassung_ab": getattr(ctx, "erfassung_ab", None),
        "eu_divisor": f"{ctx.eu.divisor_weeks:.1f}".replace(".", ","),
        "schulwochen_ueber_48": ctx.schulwochen_ueber_48,
    }


def _parse_hours(raw: str) -> float | None:
    text = (raw or "").strip().replace(" ", "").replace("h", "")
    if not text:
        return None
    if ":" in text:
        h, _, m = text.partition(":")
        value = int(h or 0) + int(m or 0) / 60
    else:
        value = float(text.replace(",", "."))
    if not 0 <= value <= 1000:  # also rejects nan/inf ("nan" parses as a float)
        raise ValueError
    return round(value, 2)


def _fmt_hours_input(value: float | None) -> str:
    if value is None:
        return ""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def ferien_rows(ctx: Any, schaetzung: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for f in ctx.holiday_forecast:
        planned = schaetzung.get(f.ferien_id)
        total_default = f.erwartete_ferienarbeit + f.ferienarbeit_bisher
        rows.append(
            {
                "id": f.ferien_id,
                "name": f.name,
                "start": f.start,
                "end": f.end,
                "laufend": f.start <= ctx.today <= f.end,
                "saldo_vor": f.saldo_vor,
                "abbau": f.abbau_kapazitaet,
                "erwartet": f.erwartete_ferienarbeit,
                "bisher": f.ferienarbeit_bisher,
                "saldo_nach": f.saldo_nach,
                "value": _fmt_hours_input(float(planned)) if planned is not None else "",
                "placeholder": fmt_dec(total_default, places=1) if planned is None else "",
                "is_default": planned is None,
                "text": _forecast_text(f),
            }
        )
    return rows


def _forecast_text(f: Any) -> str:
    """Konzept §6.4 sentence."""
    text = (
        f"Du gehst voraussichtlich mit {fmt_hm(f.saldo_vor, plus=True)} h in die {f.name}. "
        f"Die Ferien bauen maximal {fmt_hm(f.abbau_kapazitaet)} h ab."
    )
    if f.erwartete_ferienarbeit:
        text += (
            f" Wenn du {fmt_hm(f.erwartete_ferienarbeit)} h in den Ferien arbeitest, kommst du mit "
            f"{fmt_hm(f.saldo_nach, plus=True)} h zurück."
        )
    else:
        text += f" Ohne Ferienarbeit kommst du mit {fmt_hm(f.saldo_nach, plus=True)} h zurück."
    return text


def _schaetzung(db: Session) -> dict[str, Any]:
    try:
        from app.services.settings_service import get_settings

        return dict(get_settings(db).ferienarbeit_schaetzung or {})
    except Exception:
        log.exception("Einstellungen konnten nicht gelesen werden")
        return {}


def _configured(db: Session) -> tuple[str, str]:
    try:
        from app.services.settings_service import get_settings

        s = get_settings(db)
        return str(s.urlaubs_modus or LeaveMode.SOMMERFERIEN_ZUERST.value), str(s.klassenfahrt_variante or "B")
    except Exception:
        log.exception("Einstellungen konnten nicht gelesen werden")
        return LeaveMode.SOMMERFERIEN_ZUERST.value, KlassenfahrtVariante.B.value


def switches(db: Session, mode: str | None, variante: str | None) -> dict[str, Any]:
    conf_mode, conf_var = _configured(db)
    shown_mode = mode or conf_mode
    shown_var = variante or conf_var
    modes = [LeaveMode.SOMMERFERIEN_ZUERST.value, LeaveMode.PROPORTIONAL.value]
    if conf_mode not in modes:
        modes.append(conf_mode)
    mode_opts = [
        {
            "value": m,
            "label": LEAVE_MODE_SHORT.get(m, m),
            "active": m == shown_mode,
            "massgeblich": m == conf_mode,
            "href": "/konto" + ("?" + q if (q := view_query(None if m == conf_mode else m, variante)) else ""),
        }
        for m in modes
    ]
    var_opts = [
        {
            "value": v.value,
            "label": VARIANTE_SHORT[v.value],
            "active": v.value == shown_var,
            "massgeblich": v.value == conf_var,
            "href": "/konto" + ("?" + q if (q := view_query(mode, None if v.value == conf_var else v.value)) else ""),
        }
        for v in KlassenfahrtVariante
    ]
    return {
        "modes": mode_opts,
        "varianten": var_opts,
        "conf_mode": conf_mode,
        "conf_mode_label": LEAVE_MODE_SHORT.get(conf_mode, conf_mode),
        "conf_var": conf_var,
        "conf_var_label": VARIANTE_LABELS.get(conf_var, conf_var),
        "mode_differs": shown_mode != conf_mode,
        "var_differs": shown_var != conf_var,
        "shown_mode_label": LEAVE_MODE_SHORT.get(shown_mode, shown_mode),
        "shown_var_label": VARIANTE_LABELS.get(shown_var, shown_var),
    }


# =========================================================================== chart data


def _interp(points: list[tuple[int, float]], x: int) -> float | None:
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if x0 <= x <= x1:
            return y0 if x1 == x0 else round(y0 + (y1 - y0) * (x - x0) / (x1 - x0), 2)
    return None


def chart_data(ctx: Any, gm: dict[str, Any] | None) -> dict[str, Any]:
    """Day-indexed series for the saldo chart (x = days since period start, y = hours)."""
    start, end = ctx.period.start, ctx.period.end
    n_days = (end - start).days + 1

    def idx(d: date) -> int:
        return (d - start).days

    saldo = [[idx(d), hours(s)] for d, s in ctx.series if d <= ctx.today]

    prognose: list[list[float]] = []
    if ctx.today < end:
        anchor = max(ctx.today, start - timedelta(days=1))
        prognose.append([idx(anchor), hours(ctx.saldo_heute if ctx.today >= start else 0)])
        for f in ctx.holiday_forecast:
            before = f.start - timedelta(days=1)
            if before > anchor and idx(before) > prognose[-1][0]:
                prognose.append([idx(before), hours(f.saldo_vor)])
            if f.end <= end and idx(f.end) > prognose[-1][0]:
                prognose.append([idx(f.end), hours(f.saldo_nach)])
        if idx(end) > prognose[-1][0]:
            prognose.append([idx(end), hours(ctx.prognose_ende)])
        else:
            prognose[-1][1] = hours(ctx.prognose_ende)

    saldo_by_idx = {int(x): y for x, y in saldo}
    prog_pts = [(int(x), float(y)) for x, y in prognose]
    feiertage = []
    for d, (name, scope) in sorted(ctx.holidays.items()):
        if not (start <= d <= end) or scope not in ("gesetzlich", "regional"):
            continue
        i = idx(d)
        y = saldo_by_idx.get(i)
        if y is None:
            y = _interp(prog_pts, i)
        feiertage.append({"i": i, "name": name, "date": d.isoformat(), "y": y if y is not None else 0})

    months = []
    d = date(start.year, start.month, 1)
    while d <= end:
        if d >= start:
            months.append({"i": idx(d), "label": MONTHS[d.month - 1][:3]})
        d = _first_of_next_month(d)

    marker = None
    if gm is not None and start <= gm["wirksam_ab"] <= end:
        marker = {"i": idx(gm["wirksam_ab"]), "label": f"Geltendmachung wirksam ab {gm['wirksam_ab']:%d.%m.%Y}"}

    return {
        "period": {"label": ctx.period.label, "start": start.isoformat(), "end": end.isoformat()},
        "x0": start.isoformat(),
        "n_days": n_days,
        "today": idx(ctx.today) if start <= ctx.today <= end else None,
        "months": months,
        "saldo": saldo,
        "prognose": prognose,
        "ferien": [
            {"name": f.name, "from": max(0, idx(f.start)), "to": min(n_days - 1, idx(f.end))}
            for f in ctx.ferien
            if f.end >= start and f.start <= end
        ],
        "urlaub": sorted(idx(d) for d in ctx.plan.days if start <= d <= end),
        "feiertage": feiertage,
        "geltendmachung": marker,
        "labels": {
            "saldo": "Saldo",
            "prognose": "Prognose",
            "zero": ZERO_LINE_LABEL,
            "ferien": "Ferien",
            "urlaub": "Urlaub",
            "feiertag": "Feiertag",
        },
        "leave_mode": ctx.leave_mode.value,
        "klassenfahrt_variante": ctx.klassenfahrt_variante.value,
    }


# =========================================================================== routes


def _page_ctx(request: Request, db: Session) -> dict[str, Any]:
    mode, variante = view_params(request)
    today = today_local()
    res = load_ctx(db, today=today, leave_mode=mode, klassenfahrt_variante=variante)
    gm = geltendmachung(db)
    data: dict[str, Any] = {
        "page_title": "Konto",
        "res": res,
        "today": today,
        "view_query": view_query(mode, variante),
        "view_mode": mode or "",
        "view_variante": variante or "",
        "geltendmachung": gm,
        "zero_label": ZERO_LINE_LABEL,
    }
    if res.ok:
        ctx = res.ctx
        data.update(
            ctx=ctx,
            tiles=tiles(ctx),
            ferien=ferien_rows(ctx, _schaetzung(db)),
            switches=switches(db, mode, variante),
        )
    return data


@router.get("/konto", response_class=HTMLResponse)
def account_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(request, "account.html", _page_ctx(request, db))


@router.get("/konto/daten.json", include_in_schema=False)
def account_data(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    mode, variante = view_params(request)
    res = load_ctx(db, leave_mode=mode, klassenfahrt_variante=variante)
    headers = {"Cache-Control": "no-store"}
    if not res.ok:
        status = 404 if res.state == "no_period" else 503
        return JSONResponse({"detail": res.message, "state": res.state}, status_code=status, headers=headers)
    return JSONResponse(chart_data(res.ctx, geltendmachung(db)), headers=headers)


@router.get("/konto/teil/kacheln", response_class=HTMLResponse, include_in_schema=False)
def account_tiles(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    mode, variante = view_params(request)
    res = load_ctx(db, leave_mode=mode, klassenfahrt_variante=variante)
    return templates.TemplateResponse(
        request,
        "partials/account_tiles.html",
        {"res": res, "tiles": tiles(res.ctx) if res.ok else None, "ctx": res.ctx},
    )


def _render_rechner(
    request: Request, db: Session, mode: str | None, variante: str | None, *, error: str | None = None,
    status_code: int = 200, saved: bool = False, raw: dict[str, str] | None = None, invalid: str | None = None,
) -> HTMLResponse:
    res = load_ctx(db, leave_mode=mode, klassenfahrt_variante=variante)
    rows = ferien_rows(res.ctx, _schaetzung(db)) if res.ok else []
    if raw:
        # After a rejected input keep what the user typed (nothing was saved).
        for row in rows:
            if row["id"] in raw:
                row["value"] = raw[row["id"]]
            row["invalid"] = row["id"] == invalid
    return templates.TemplateResponse(
        request,
        "partials/account_ferienrechner.html",
        {
            "res": res,
            "ctx": res.ctx,
            "ferien": rows,
            "view_mode": mode or "",
            "view_variante": variante or "",
            "error": error,
            "saved": saved,
        },
        status_code=status_code,
    )


@router.get("/konto/teil/ferienrechner", response_class=HTMLResponse, include_in_schema=False)
def account_rechner(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    mode, variante = view_params(request)
    return _render_rechner(request, db, mode, variante)


@router.post("/konto/ferienrechner", include_in_schema=False)
async def account_rechner_save(request: Request, db: Session = Depends(get_db)) -> Response:
    form = await request.form()
    mode = str(form.get("urlaub") or "") or None
    if mode not in {m.value for m in LeaveMode}:
        mode = None
    variante = str(form.get("klassenfahrt") or "").upper() or None
    if variante not in {v.value for v in KlassenfahrtVariante}:
        variante = None
    is_htmx = bool(request.headers.get("hx-request"))

    planned: dict[str, float | None] = {}
    error = None
    known: set[str] = set()
    res = load_ctx(db)
    if res.ok:
        known = {f.id for f in res.ctx.ferien}
    typed: dict[str, str] = {}
    invalid: str | None = None
    for key, raw in form.multi_items():
        if not key.startswith("plan_"):
            continue
        ferien_id = key[5:]
        if ferien_id not in known:
            continue
        typed[ferien_id] = str(raw)[:8]
        if error is not None:
            continue
        try:
            planned[ferien_id] = _parse_hours(str(raw))
        except ValueError:
            error = f"„{str(raw)[:20]}“ ist keine gültige Stundenzahl (0–1000, z. B. 12 oder 12,5)."
            invalid = ferien_id

    if error is None and planned:
        try:
            from app.services.settings_service import set_ferienarbeit_schaetzung

            set_ferienarbeit_schaetzung(db, planned)
        except ValueError as exc:
            db.rollback()
            error = str(exc) or "Ungültige Eingabe."
        except Exception:
            log.exception("Geplante Ferienarbeit konnte nicht gespeichert werden")
            db.rollback()
            error = "Speichern fehlgeschlagen."

    if not is_htmx:
        if error:
            flash(request, error, "danger")
        else:
            flash(request, "Geplante Ferienarbeit gespeichert.", "success")
        q = view_query(mode, variante)
        return RedirectResponse("/konto" + ("?" + q if q else "") + "#ferienrechner", status_code=303)

    # Errors are answered with 200: htmx 2 does not swap 4xx responses, the message would never show.
    response = _render_rechner(
        request, db, mode, variante, error=error, saved=error is None,
        raw=typed if error else None, invalid=invalid,
    )
    if error is None:
        response.headers["HX-Trigger"] = "deputat-konto-changed"
    return response


@router.get("/konto/teil/urlaubsvergleich", response_class=HTMLResponse, include_in_schema=False)
def account_leave_compare(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    rows: list[dict[str, Any]] = []
    state = "ok"
    conf_mode, _ = _configured(db)
    try:
        from app.services import calendar_service as cal
        from app.services import ledger as ledger_service

        try:
            rows = ledger_service.compare_leave_modes(db, today=today_local())
        except (cal.NoPeriodError, cal.NotFoundError):
            db.rollback()
            state = "no_period"
    except Exception:
        log.exception("Urlaubsmodus-Vergleich fehlgeschlagen")
        db.rollback()
        state = "error"
    for r in rows:
        r["short"] = LEAVE_MODE_SHORT.get(r.get("mode"), r.get("label"))
        r["vergleich_label"] = (
            "Konto bei Urlaub = Sommerferien"
            if r.get("mode") == LeaveMode.SOMMERFERIEN_ZUERST.value
            else "Konto bei proportionaler Verteilung"
        )
    return templates.TemplateResponse(
        request,
        "partials/account_leave_compare.html",
        {
            "rows": rows,
            "state": state,
            "conf_mode": conf_mode,
            "conf_mode_label": LEAVE_MODE_SHORT.get(conf_mode, conf_mode),
            "conf_in_rows": any(r.get("massgeblich") for r in rows),
        },
    )


def iso_week_label(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"

