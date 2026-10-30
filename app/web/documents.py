"""HTML – Screen 'Schreiben' (Konzept §11, L7) and the in-app reminder slot (Konzept §3.2 Nr. 15).

Routes (mounted by app.main without prefix, no login):

- ``GET  /schreiben``                   saved letters + the four letter types
- ``GET  /schreiben/neu?typ=…``         parameters (Zeitraum/Monat, Ort, Adressat …), rendered preview,
                                        editable Markdown text; GET form, works without JS
- ``POST /schreiben/vorschau``          HTMX: Markdown text -> preview HTML (partial)
- ``POST /schreiben/neu``               save (``text`` = edited Markdown) -> redirect to the detail page
- ``GET  /schreiben/{id}``              print-friendly view (browser print = PDF), download, mark as sent
- ``GET  /schreiben/{id}/download``     Markdown file
- ``POST /schreiben/{id}/versendet``    set ``sent_at`` (field ``versendet_am``, default today)
- ``GET  /hinweise``                    reminder banners partial, loaded by the slot in base.html
                                        (``partials/reminders_slot.html``; ``?seite=`` = current path)

Rendering, the non-removable notice and the placeholder values live in ``app.services.documents``;
reminders in ``app.services.reminders``.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db.models import DocumentType
from app.db.session import get_db
from app.services import documents as docs
from app.services import reminders as reminders_service
from app.util import today_local
from app.web.templating import flash, templates

log = logging.getLogger("app.web.documents")

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]

OVERRIDE_FIELDS: tuple[tuple[str, str], ...] = (
    ("ort", "Ort"),
    ("datum", "Datum des Schreibens"),
    ("adressat", "Adressat"),
    ("dienstweg", "Dienstweg"),
    ("abdruck", "Abdruck an"),
)
PAGE_FIELDS = ("typ", "von", "bis", "monat", *(k for k, _ in OVERRIDE_FIELDS))
MAX_FIELD = 300


# --------------------------------------------------------------------------- helpers


def _parse_date(value: str | None, label: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise docs.DocumentError(f"{label}: bitte ein gültiges Datum angeben.") from None


def _params(source: Any) -> dict[str, str]:
    """The page parameters from query params or a form (strings, trimmed, length-limited)."""
    out: dict[str, str] = {}
    for key in PAGE_FIELDS:
        value = source.get(key)
        if isinstance(value, str):
            out[key] = value.strip()[:MAX_FIELD]
    return out


def _overrides(params: dict[str, str]) -> dict[str, str]:
    over = {k: params[k] for k, _ in OVERRIDE_FIELDS if params.get(k)}
    if params.get("monat"):
        over["monat"] = params["monat"]
    return over


def _type_or_404(doc_type: str | None) -> str:
    if not doc_type or doc_type not in docs.TYPE_INFO:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unbekannter Schreiben-Typ.")
    return doc_type


def _type_cards() -> list[dict[str, Any]]:
    return [{"type": t, **info} for t, info in docs.TYPE_INFO.items()]


def _new_url(params: dict[str, str]) -> str:
    return "/schreiben/neu?" + urlencode({k: v for k, v in params.items() if v})


def _render_new(
    request: Request,
    db: Session,
    params: dict[str, str],
    *,
    text: str | None = None,
    note: str = "",
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    doc_type = params["typ"]
    info = docs.TYPE_INFO[doc_type]
    result: docs.Rendered | None = None
    try:
        result = docs.prepare(
            db,
            doc_type,
            _parse_date(params.get("von"), "Beginn"),
            _parse_date(params.get("bis"), "Ende"),
            _overrides(params),
        )
    except docs.DocumentError as exc:
        error = error or str(exc)
    markdown = text if text is not None else (result.markdown if result else "")
    values = result.values if result else {}
    if result is not None and not info["uses_month"]:  # show the (possibly clamped) range used
        params["von"] = result.period_from.isoformat() if result.period_from else ""
        params["bis"] = result.period_to.isoformat() if result.period_to else ""
    if info["uses_month"] and not params.get("monat"):
        params["monat"] = docs.default_month(today_local())
    defaults = {k: values.get(k, "") for k, _ in OVERRIDE_FIELDS}
    if defaults.get("ort") == "[Ort]":
        defaults["ort"] = ""
    placeholders = [
        {"key": key, "label": label, "value": values.get(key, "")}
        for key, label in docs.PLACEHOLDERS.items()
        if key in values
    ]
    ctx = {
        "page_title": info["label"],
        "doc_type": doc_type,
        "info": info,
        "types": _type_cards(),
        "params": params,
        "defaults": defaults,
        "override_fields": [
            (k, label) for k, label in OVERRIDE_FIELDS
            if not (k in ("dienstweg", "abdruck") and doc_type == DocumentType.PRIORITAETSBITTE.value)
        ],
        "markdown": markdown,
        "preview": docs.to_html(markdown) if markdown else None,
        "warnings": result.warnings if result else [],
        "placeholders": placeholders,
        "notice": docs.NOTICE,
        "note": note,
        "error": error,
        "can_save": result is not None,
    }
    return templates.TemplateResponse(request, "documents_new.html", ctx, status_code=status_code)


def _doc_or_404(db: Session, doc_id: str):
    try:
        return docs.get_document(db, doc_id)
    except docs.DocumentNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schreiben nicht gefunden.") from None


# --------------------------------------------------------------------------- routes: letters


@router.get("/schreiben", response_class=HTMLResponse)
def documents_page(request: Request, db: DbSession) -> HTMLResponse:
    items = [docs.document_dict(d) for d in docs.list_documents(db)]
    ctx = {
        "page_title": "Schreiben",
        "types": _type_cards(),
        "documents": items,
        "wirksam_ab": docs.geltendmachung_wirksam_ab(db),
        "notice": docs.NOTICE,
    }
    return templates.TemplateResponse(request, "documents.html", ctx)


@router.get("/schreiben/neu", response_class=HTMLResponse)
def document_new(request: Request, db: DbSession) -> HTMLResponse:
    params = _params(request.query_params)
    params["typ"] = _type_or_404(params.get("typ"))
    return _render_new(request, db, params)


@router.post("/schreiben/vorschau", response_class=HTMLResponse, include_in_schema=False)
async def document_preview(request: Request) -> HTMLResponse:
    form = await request.form()
    text = form.get("text")
    text = text if isinstance(text, str) else ""
    if len(text) > docs.MAX_MD_LENGTH:
        text = text[: docs.MAX_MD_LENGTH]
    return templates.TemplateResponse(
        request, "partials/documents_preview.html", {"preview": docs.to_html(text) if text.strip() else None}
    )


@router.post("/schreiben/neu", response_class=HTMLResponse)
async def document_create(request: Request, db: DbSession) -> Response:
    form = await request.form()
    params = _params(form)
    params["typ"] = _type_or_404(params.get("typ"))
    raw_text = form.get("text")
    text = raw_text if isinstance(raw_text, str) else ""
    raw_note = form.get("notiz")
    note = raw_note.strip()[:2000] if isinstance(raw_note, str) else ""
    try:
        doc, _result = docs.create(
            db,
            params["typ"],
            _parse_date(params.get("von"), "Beginn"),
            _parse_date(params.get("bis"), "Ende"),
            _overrides(params),
            text=text,
            note=note,
        )
    except docs.DocumentError as exc:
        db.rollback()
        return _render_new(request, db, params, text=text or None, note=note, error=str(exc),
                           status_code=422)
    flash(request, "Schreiben gespeichert. Vor dem Versand bitte prüfen.", "success")
    return RedirectResponse(f"/schreiben/{doc.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/schreiben/{doc_id}", response_class=HTMLResponse)
def document_detail(doc_id: str, request: Request, db: DbSession) -> HTMLResponse:
    doc = _doc_or_404(db, doc_id)
    data = docs.document_dict(doc)
    info = docs.TYPE_INFO.get(doc.type, {})
    again: dict[str, str] = {"typ": doc.type}
    if info.get("uses_month"):
        if doc.period_from:
            again["monat"] = f"{doc.period_from:%Y-%m}"
    else:
        if doc.period_from:
            again["von"] = doc.period_from.isoformat()
        if doc.period_to:
            again["bis"] = doc.period_to.isoformat()
    sent_on = data["sent_on"]
    ctx = {
        "page_title": data["type_label"],
        "doc": data,
        "info": info,
        "html": docs.to_html(doc.rendered_md),
        "again_url": _new_url(again),
        "today": today_local(),
        "wirksam_ab": (
            docs.first_of_next_month(sent_on)
            if sent_on and doc.type == DocumentType.GELTENDMACHUNG.value
            else None
        ),
    }
    return templates.TemplateResponse(request, "documents_detail.html", ctx)


@router.get("/schreiben/{doc_id}/download")
def document_download(doc_id: str, db: DbSession) -> Response:
    doc = _doc_or_404(db, doc_id)
    name = docs.download_name(doc)
    return Response(
        docs.ensure_notice(doc.rendered_md),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
                 "Cache-Control": "no-store"},
    )


@router.post("/schreiben/{doc_id}/versendet", include_in_schema=False)
async def document_mark_sent(doc_id: str, request: Request, db: DbSession) -> Response:
    _doc_or_404(db, doc_id)
    form = await request.form()
    raw = form.get("versendet_am")
    try:
        sent_on = _parse_date(raw if isinstance(raw, str) else None, "Versanddatum") or today_local()
        doc = docs.mark_sent(db, doc_id, sent_on)
    except docs.DocumentError as exc:
        db.rollback()
        flash(request, str(exc), "danger")
        return RedirectResponse(f"/schreiben/{doc_id}", status_code=status.HTTP_303_SEE_OTHER)
    message = f"Als versendet markiert ({docs.sent_date(doc):%d.%m.%Y})."
    if doc.type == DocumentType.GELTENDMACHUNG.value:
        wirksam = docs.geltendmachung_wirksam_ab(db)
        if wirksam:
            message += f" Das Konto zeigt „Geltendmachung wirksam ab {wirksam:%d.%m.%Y}“."
    flash(request, message, "success")
    return RedirectResponse(f"/schreiben/{doc_id}", status_code=status.HTTP_303_SEE_OTHER)


# --------------------------------------------------------------------------- routes: reminders


@router.get("/hinweise", response_class=HTMLResponse, include_in_schema=False)
def reminders(request: Request, db: DbSession) -> HTMLResponse:
    page = request.query_params.get("seite") or ""
    try:
        hints = reminders_service.collect(db)
    except Exception:  # pragma: no cover - collect() already guards each rule
        log.exception("Reminders failed")
        hints = []
    if page == "/heute":  # the timer screen shows its own long-running banner
        hints = [h for h in hints if not h["key"].startswith("timer-lang:")]
    hints = [h for h in hints if not (h.get("link") and h["link"].split("?")[0] == page)]
    return templates.TemplateResponse(
        request,
        "partials/reminders_list.html",
        {"hints": hints},
        headers={"Cache-Control": "no-store"},
    )
