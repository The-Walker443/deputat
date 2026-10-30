"""HTML – Screens 'Monate & Berichte' and 'Export & Backup' (Konzept §10, §6.6, §14.4).

Routes (mounted by app.main without prefix; no login, CSRF via the global dependency):

- ``GET  /monate?periode=``               month list of a period (Soll, Ist, Saldo, Mehrarbeit, status, hash)
- ``GET  /monate/{ym}``                   month overview: figures, open items (with fix links), Mehrarbeit,
                                           close state (hash, report link, export hint)
- ``GET  /monate/{ym}/abschliessen``      confirmation page (the same form is shown in a <dialog> by months.js)
- ``POST /monate/{ym}/abschliessen``      close (``bestaetigt``; ``trotzdem`` + ``begruendung`` to force)
- ``POST /monate/{ym}/mehrarbeit``        status / note / "Vertretung erhalten" (HTMX: returns the partial)
- ``GET  /monate/{ym}/bericht``           report: stored close file if closed (``?aktuell=1`` live render),
                                           ``?download=1`` as attachment; a print toolbar is injected
- ``GET  /berichte/jahr?periode=&eintraege=0`` year report (same toolbar)
- ``GET  /export``                        CSV/JSON/ICS downloads, JSON restore, backups list
- ``POST /export/backup``                 "Backup jetzt"
- ``POST /export/wiederherstellen``       JSON restore (multipart ``datei``; ``verstanden`` + word ERSETZEN)

Figures come from :mod:`app.services.months` / :mod:`app.services.reports` (ledger-based); downloads use the
JSON API endpoints in :mod:`app.api.export` and :mod:`app.api.backup`.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.config import get_config
from app.db.models import MonthClose
from app.db.session import get_db
from app.services import backup as backup_service
from app.services import calendar_service as cal
from app.services import export as export_service
from app.services import months as svc
from app.services import reports
from app.util import fmt_dec, today_local
from app.web.assets import static_url
from app.web.templating import flash, templates

log = logging.getLogger("app.web.months")

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]

TRUE_VALUES = ("1", "true", "on", "yes", "ja")
RESTORE_WORD = "ERSETZEN"
OPEN_ITEMS_VISIBLE = 8  # further open items are folded into <details>

STATUS_BADGES = {
    svc.STATUS_OFFEN: "badge-outline",
    "abgeschlossen": "badge-success",
    "nachtraeglich_korrigiert": "badge-warn",
}
MEHRARBEIT_STATUS = (("offen", "offen"), ("beantragt", "beantragt"), ("ausgeglichen", "ausgeglichen"))
AMPEL_LABELS = {"gruen": "unter 44 h", "gelb": "44–48 h", "rot": "über 48 h"}
BACKUP_KIND_LABELS = {"manuell": "manuell", "auto": "nächtlich", "automatisch": "automatisch",
                      "restore": "vor Wiederherstellung"}


# --------------------------------------------------------------------------- helpers


def _truthy(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in TRUE_VALUES


def _wants_partial(request: Request) -> bool:
    return bool(request.headers.get("hx-request")) and not request.headers.get("hx-history-restore-request")


def _ym_or_404(ym: str) -> str:
    try:
        start, _ = svc.parse_ym(ym)
    except svc.MonthValidationError as exc:
        raise HTTPException(404, exc.message) from None
    return svc.ym_of(start)


def _shift_ym(ym: str, months: int) -> str:
    year, month = int(ym[:4]), int(ym[5:])
    index = year * 12 + month - 1 + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _ustd(value: float | None) -> str:
    """UStd with German decimal comma, at most one decimal ('3', '4,5')."""
    value = float(value or 0.0)
    text = f"{value:.1f}".replace(".", ",")
    return text[:-2] if text.endswith(",0") else text


def _size(num: int | None) -> str:
    num = int(num or 0)
    if num >= 1024 * 1024:
        return f"{num / (1024 * 1024):.1f} MB".replace(".", ",")
    if num >= 1024:
        return f"{num / 1024:.0f} KB"
    return f"{num} B"


def _parse_decimal(text: str | None) -> float | None:
    text = (text or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        raise svc.MonthValidationError("„Vertretung erhalten“ bitte als Zahl angeben (z. B. 1,5).") from None


def _periods(db: Session) -> list[Any]:
    try:
        return list(cal.list_periods(db))
    except Exception:  # pragma: no cover - listing must never break the page
        log.exception("Perioden konnten nicht geladen werden")
        return []


def _item_fix(item: svc.OpenItem, ym: str) -> dict[str, str] | None:
    """Link that resolves an open item (Konzept §10.1 step 1)."""
    back = f"/monate/{ym}"
    if item.kind == svc.ITEM_TIMER_LAEUFT:
        return {"href": "/heute", "label": "Zum Timer"}
    if item.kind == svc.ITEM_VORSCHLAG and item.date:
        return {"href": f"/tag/{item.date.isoformat()}?next={back}", "label": "Tag bestätigen"}
    if item.kind == svc.ITEM_OHNE_EINTRAG and item.date:
        return {"href": f"/eintraege/neu?datum={item.date.isoformat()}&next={back}", "label": "Nachtragen"}
    return None


def _open_items(ov: svc.MonthOverview) -> list[dict[str, Any]]:
    return [{"item": i, "fix": _item_fix(i, ov.year_month)} for i in ov.open_items]


def _chart_data(rows: list[svc.MonthRow]) -> list[dict[str, Any]]:
    short = ("Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez")
    result = []
    for r in rows:
        if r.zukunft:
            continue
        result.append({
            "label": f"{short[r.start.month - 1]} {str(r.start.year)[2:]}",
            "soll": round(r.soll / 3600, 2),
            "ist": round(r.ist / 3600, 2),
            "saldo": round(r.saldo / 3600, 2),
            "kumuliert": round(r.saldo_kumuliert / 3600, 2),
        })
    return result


# --------------------------------------------------------------------------- month list


@router.get("/monate", response_class=HTMLResponse)
def months_list(request: Request, db: DbSession) -> HTMLResponse:
    ref = (request.query_params.get("periode") or "").strip() or None
    today = today_local()
    periods = _periods(db)
    ctx: dict[str, Any] = {
        "page_title": "Monate & Berichte",
        "periods": periods,
        "period": None,
        "rows": [],
        "error": None,
        "today": today,
        "status_badges": STATUS_BADGES,
        "ustd": _ustd,
    }
    try:
        period = cal.resolve_period(db, ref, today)
        rows = svc.list_months(db, period, today=today)
    except (cal.NoPeriodError, cal.NotFoundError, svc.MonthError) as exc:
        db.rollback()
        message = getattr(exc, "message", None) or str(exc)
        ctx["error"] = message
        ctx["no_period"] = not periods
        return templates.TemplateResponse(request, "months.html", ctx, status_code=200 if not ref else 404)
    current_ym = svc.ym_of(today)
    totals = {
        "soll": sum(r.soll for r in rows),
        "ist": sum(r.ist for r in rows),
        "saldo": sum(r.saldo for r in rows),
        "ustd": sum(r.ustd_total for r in rows),
        "ausgleich": sum(r.ustd_ausgleichsfaehig for r in rows),
    }
    closable = [r for r in rows if r.status == svc.STATUS_OFFEN and not r.laufend and not r.zukunft
                and r.year_month < current_ym]
    ctx.update(
        period=period,
        rows=rows,
        totals=totals,
        current_ym=current_ym,
        closable=closable,
        chart_json=json.dumps(_chart_data(rows)),
    )
    return templates.TemplateResponse(request, "months.html", ctx)


# --------------------------------------------------------------------------- month detail


def _overview_or_error(db: Session, ym: str) -> svc.MonthOverview:
    try:
        return svc.month_overview(db, ym)
    except svc.MonthNotFound as exc:
        db.rollback()
        raise HTTPException(404, exc.message) from None
    except svc.MonthError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, exc.message) from None


def _detail_context(ov: svc.MonthOverview, **extra: Any) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "page_title": ov.label,
        "ov": ov,
        "ym": ov.year_month,
        "prev_ym": _shift_ym(ov.year_month, -1),
        "next_ym": _shift_ym(ov.year_month, 1),
        "items": _open_items(ov),
        "items_visible": OPEN_ITEMS_VISIBLE,
        "ma": ov.mehrarbeit,
        "ma_status": MEHRARBEIT_STATUS,
        "status_badges": STATUS_BADGES,
        "ampel_labels": AMPEL_LABELS,
        "eu_hours": fmt_dec(ov.eu.hours * 3600),
        "ustd": _ustd,
        "export_hint": svc.EXPORT_HINT,
        "close_error": None,
        "close_form": {"begruendung": "", "trotzdem": False},
        "ma_message": None,
        "ma_error": None,
    }
    ctx.update(extra)
    return ctx


@router.get("/monate/{ym}", response_class=HTMLResponse)
def month_detail(ym: str, request: Request, db: DbSession) -> HTMLResponse:
    ym = _ym_or_404(ym)
    ov = _overview_or_error(db, ym)
    ctx = _detail_context(ov, just_closed=_truthy(request.query_params.get("abgeschlossen")))
    return templates.TemplateResponse(request, "months_detail.html", ctx)


@router.get("/monate/{ym}/abschliessen", response_class=HTMLResponse)
def month_close_page(ym: str, request: Request, db: DbSession) -> Response:
    ym = _ym_or_404(ym)
    ov = _overview_or_error(db, ym)
    if ov.is_closed:
        flash(request, f"{ov.label} ist bereits abgeschlossen.", "info")
        return RedirectResponse(f"/monate/{ym}", status_code=303)
    return templates.TemplateResponse(request, "months_close.html", _detail_context(ov))


@router.post("/monate/{ym}/abschliessen", response_class=HTMLResponse)
async def month_close(ym: str, request: Request, db: DbSession) -> Response:
    ym = _ym_or_404(ym)
    form = await request.form()
    confirmed = _truthy(form.get("bestaetigt"))
    force = _truthy(form.get("trotzdem"))
    note = str(form.get("begruendung") or "").strip()
    form_state = {"begruendung": note, "trotzdem": force}

    def refuse(message: str, status_code: int = 422) -> HTMLResponse:
        db.rollback()
        ov = _overview_or_error(db, ym)
        if ov.is_closed:
            raise HTTPException(409, message)
        ctx = _detail_context(ov, close_error=message, close_form=form_state)
        return templates.TemplateResponse(request, "months_close.html", ctx, status_code=status_code)

    if not confirmed:
        return refuse("Bitte bestätigen, dass die Einträge des Monats nach dem Abschluss gesperrt werden.")
    if force and not note:
        return refuse("Bitte begründen, warum der Monat trotz offener Punkte abgeschlossen wird.")
    try:
        result = svc.close_month(db, ym, force=force, note=note or None)
    except svc.MonthOpenItems as exc:
        return refuse(exc.message, exc.status_code)
    except svc.MonthConflict as exc:
        db.rollback()
        flash(request, exc.message, "warn")
        return RedirectResponse(f"/monate/{ym}", status_code=303)
    except svc.MonthError as exc:
        return refuse(exc.message, exc.status_code)
    mc = result.month_close
    message = f"{svc.month_label(ym)} abgeschlossen. Prüf-Hash {mc.entries_hash_sha256[:12]}…"
    if result.forced_items:
        message += f" ({len(result.forced_items)} offene Punkte mit Begründung übergangen.)"
    flash(request, message, "success")
    return RedirectResponse(f"/monate/{ym}?abgeschlossen=1", status_code=303)


@router.post("/monate/{ym}/mehrarbeit", response_class=HTMLResponse)
async def month_mehrarbeit(ym: str, request: Request, db: DbSession) -> Response:
    ym = _ym_or_404(ym)
    form = await request.form()
    status = str(form.get("status") or "").strip() or None
    note = str(form.get("notiz") or "")
    message = error = None
    try:
        erhalten = _parse_decimal(str(form.get("erhalten") or ""))
        svc.update_mehrarbeit(db, ym, status=status, note=note, ustd_vertretung_erhalten=erhalten)
        message = "Mehrarbeit gespeichert."
    except svc.MonthNotFound as exc:
        db.rollback()
        raise HTTPException(404, exc.message) from None
    except svc.MonthError as exc:
        db.rollback()
        error = exc.message
    if _wants_partial(request):
        ov = _overview_or_error(db, ym)
        ctx = _detail_context(ov, ma_message=message, ma_error=error)
        return templates.TemplateResponse(request, "partials/months_mehrarbeit.html", ctx)
    flash(request, message or error or "", "success" if message else "danger")
    return RedirectResponse(f"/monate/{ym}#mehrarbeit", status_code=303)


# --------------------------------------------------------------------------- reports


_BODY_OPEN_RE = re.compile(r"<body[^>]*>", re.IGNORECASE)


def _with_toolbar(request: Request, html: str, toolbar: dict[str, Any]) -> str:
    """Inject the screen toolbar (back, print, download) into a self-contained report document.

    The stored file itself is never changed; the toolbar is ``no-print`` and the script is external (CSP).
    """
    bar = templates.env.get_template("partials/months_report_toolbar.html").render(request=request, **toolbar)
    head = f'<link rel="stylesheet" href="{static_url("css/months.css")}">\n'
    script = f'<script src="{static_url("js/months.js")}" defer></script>\n'
    if "</head>" in html:
        html = html.replace("</head>", head + "</head>", 1)
    match = _BODY_OPEN_RE.search(html)
    if match:
        html = html[: match.end()] + "\n" + bar + html[match.end():]
    else:
        html = bar + html
    if "</body>" in html:
        html = html.replace("</body>", script + "</body>", 1)
    else:
        html += script
    return html


def _report_response(html: str, filename: str, download: bool) -> HTMLResponse:
    kind = "attachment" if download else "inline"
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store",
        "Content-Disposition": f'{kind}; filename="{filename}"',
    })


@router.get("/monate/{ym}/bericht", response_class=HTMLResponse)
def month_report(ym: str, request: Request, db: DbSession) -> HTMLResponse:
    ym = _ym_or_404(ym)
    q = request.query_params
    live = _truthy(q.get("aktuell"))
    download = _truthy(q.get("download"))
    mc = db.get(MonthClose, ym)
    html: str | None = None
    stored = False
    if mc is not None and not live:
        path = svc.report_path(mc)
        if path is not None and path.is_file():
            html = path.read_text(encoding="utf-8")
            stored = True
    if html is None:
        try:
            html = reports.render_month_report(db, ym)
        except svc.MonthNotFound as exc:
            db.rollback()
            raise HTTPException(404, exc.message) from None
        except svc.MonthError as exc:
            db.rollback()
            raise HTTPException(exc.status_code, exc.message) from None
    suffix = "" if stored or mc is None else "_aktuell"
    filename = f"Monatsbericht_{ym}{suffix}.html"
    if download:
        return _report_response(html, filename, True)
    base = f"/monate/{ym}/bericht"
    toolbar = {
        "back_href": f"/monate/{ym}",
        "back_label": f"Zurück zu {svc.month_label(ym)}",
        "download_href": f"{base}?download=1" + ("&aktuell=1" if live and mc is not None else ""),
        "mode_label": (
            f"Gespeicherte Abschluss-Fassung vom {mc.closed_at:%d.%m.%Y}" if stored and mc is not None
            else ("Aktueller Stand (nach dem Abschluss neu berechnet)" if mc is not None
                  else "Vorläufiger Bericht – Monat nicht abgeschlossen")
        ),
        "toggles": (
            [{"href": f"{base}?aktuell=1", "label": "Aktuellen Stand zeigen"}] if stored
            else ([{"href": base, "label": "Abschluss-Fassung zeigen"}] if mc is not None else [])
        ),
        "warn": mc is None or (not stored and mc is not None),
    }
    return _report_response(_with_toolbar(request, html, toolbar), filename, False)


@router.get("/berichte/jahr", response_class=HTMLResponse)
def year_report(request: Request, db: DbSession) -> HTMLResponse:
    q = request.query_params
    ref = (q.get("periode") or "").strip() or None
    with_entries = q.get("eintraege", "1").strip().lower() not in ("0", "false", "nein", "off")
    download = _truthy(q.get("download"))
    try:
        period = cal.resolve_period(db, ref)
        html = reports.render_year_report(db, period, include_entries=with_entries)
    except (cal.NoPeriodError, cal.NotFoundError) as exc:
        db.rollback()
        raise HTTPException(404, str(exc)) from None
    except svc.MonthError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, exc.message) from None
    label = period.label.replace("/", "-")
    filename = f"Jahresbericht_{label}{'' if with_entries else '_kurz'}.html"
    if download:
        return _report_response(html, filename, True)
    params = f"periode={period.id}"
    toolbar = {
        "back_href": f"/monate?{params}",
        "back_label": "Zurück zu Monate",
        "download_href": f"/berichte/jahr?{params}&download=1" + ("" if with_entries else "&eintraege=0"),
        "mode_label": f"Jahresbericht {period.label} – aktueller Stand",
        "toggles": [
            {"href": f"/berichte/jahr?{params}&eintraege=0", "label": "Ohne Eintragsliste"} if with_entries
            else {"href": f"/berichte/jahr?{params}", "label": "Mit Eintragsliste"}
        ],
        "warn": False,
    }
    return _report_response(_with_toolbar(request, html, toolbar), filename, False)


# --------------------------------------------------------------------------- export / backup


def _export_context(db: Session, **extra: Any) -> dict[str, Any]:
    cfg = get_config()
    try:
        backups = backup_service.list_backups(db, limit=30)
    except Exception:  # pragma: no cover - the page must still offer the downloads
        log.exception("Backup-Liste konnte nicht geladen werden")
        db.rollback()
        backups = {"log": [], "files": [], "retention_days": cfg.backup_retention_days, "cron": cfg.backup_cron,
                   "directory": str(cfg.backups_dir)}
    period = None
    try:
        period = cal.get_current_period(db)
    except Exception:  # pragma: no cover
        db.rollback()
    try:
        has_data = export_service.has_user_data(db)
    except Exception:  # pragma: no cover
        db.rollback()
        has_data = True
    ctx: dict[str, Any] = {
        "page_title": "Export & Backup",
        "backups": backups,
        "period": period,
        "today": today_local(),
        "has_data": has_data,
        "size": _size,
        "kind_labels": BACKUP_KIND_LABELS,
        "restore_word": RESTORE_WORD,
        "restore_error": None,
        "max_restore_mb": export_service.MAX_RESTORE_BYTES // (1024 * 1024),
        "data_dir": str(cfg.data_dir),
    }
    ctx.update(extra)
    return ctx


@router.get("/export", response_class=HTMLResponse)
def export_page(request: Request, db: DbSession) -> HTMLResponse:
    return templates.TemplateResponse(request, "export.html", _export_context(db))


@router.post("/export/backup", response_class=HTMLResponse)
def export_backup_now(request: Request, db: DbSession) -> Response:
    result = backup_service.create_backup(db, kind="manuell")
    if result.ok:
        message = f"Backup erstellt: {result.file} ({_size(result.size_bytes)})."
        if result.deleted:
            message += f" {len(result.deleted)} alte Sicherung(en) nach Aufbewahrungsfrist entfernt."
        flash(request, message, "success")
    else:
        flash(request, result.message or "Backup fehlgeschlagen.", "danger")
    return RedirectResponse("/export#backups", status_code=303)


@router.post("/export/wiederherstellen", response_class=HTMLResponse)
async def export_restore(request: Request, db: DbSession) -> Response:
    form = await request.form()
    upload = form.get("datei")

    def refuse(message: str, status_code: int = 422) -> HTMLResponse:
        db.rollback()
        ctx = _export_context(db, restore_error=message)
        return templates.TemplateResponse(request, "export.html", ctx, status_code=status_code)

    if upload is None or isinstance(upload, str) or not getattr(upload, "filename", ""):
        return refuse("Bitte eine JSON-Sicherung auswählen.")
    content = await upload.read(export_service.MAX_RESTORE_BYTES + 1)
    if len(content) > export_service.MAX_RESTORE_BYTES:
        return refuse("Die Datei ist zu groß.", 413)
    word_ok = str(form.get("bestaetigung") or "").strip().upper() == RESTORE_WORD
    if not (_truthy(form.get("verstanden")) and word_ok):
        return refuse(f"Bitte bestätigen: Häkchen setzen und „{RESTORE_WORD}“ eintippen. Es wurde nichts geändert.")
    try:
        # Parsing, validation, the safety backup and the restore are blocking work: never on the event
        # loop (it would stall every request including /healthz) – same as the JSON API.
        result = await run_in_threadpool(_restore_sync, db, content)
    except export_service.ExportError as exc:
        return refuse(str(exc), getattr(exc, "status_code", 422))
    total = sum(result.counts.values())
    message = f"Sicherung wiederhergestellt ({total} Datensätze)."
    if result.safety_backup:
        message += f" Der vorherige Stand liegt als Sicherheitskopie in {result.safety_backup}."
    flash(request, message, "success")
    return RedirectResponse("/export", status_code=303)


def _restore_sync(db: Session, content: bytes) -> export_service.RestoreResult:
    data = export_service.parse_backup(content)
    export_service.validate_backup(db, data)
    return export_service.restore_json(db, data, replace=True)


# --------------------------------------------------------------------------- misc


def month_link(d: date) -> str:  # used by other screens if they want to link a month
    return f"/monate/{svc.ym_of(d)}"
