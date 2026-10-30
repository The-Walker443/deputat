"""HTML – Screen 'Import': four-step wizard for existing records (Konzept §12).

Mounted by app.main without prefix (no login; CSRF via the global dependency). All logic lives in
:mod:`app.services.import_service`; this module only translates forms ↔ service calls.

Routes
------
- ``GET  /import``                          step 1 upload form, batch list (undo), remembered mappings
- ``POST /import``                          upload (multipart ``datei``) → redirect to the preview
- ``GET  /import/{id}``                     step 1 preview: first 50 rows, sheet selection, header row
- ``GET  /import/{id}/zuordnung``           step 2 column mapping (``?blatt=…&blatt=…&kopfzeile=``)
- ``POST /import/{id}/pruefen``             step 3 check (mapping + options from the form)
- ``GET  /import/{id}/pruefen``             step 3 again from the state stored on the batch
- ``POST /import/{id}/uebernehmen``         step 4 commit → redirect to the result
- ``GET  /import/{id}/ergebnis``            step 4 result summary
- ``GET  /import/{id}/rueckgaengig``        undo confirmation (reason field when closed months are touched)
- ``POST /import/{id}/rueckgaengig``        undo a committed batch / discard an unfinished upload
- ``POST /import/zuordnungen/loeschen``     forget a remembered category mapping (``source_text``)

Form fields of steps 2/3: ``map_<feld>`` (column index, empty = not mapped) for the fields in
``import_service.FIELDS``, ``blatt`` (repeatable), ``kopfzeile`` (empty = automatic, 0 = no header row),
``standard_kategorie``, ``kat_text``/``kat_code``/``kat_orig`` (parallel lists, category mapping table),
``duplikate_ueberspringen``, ``merken``, ``begruendung``, ``einschliessen``/``ausschliessen`` (row refs).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData, UploadFile

from app.db.models import TimeEntry
from app.db.session import get_db
from app.services import calendar_service as cal
from app.services import import_service as svc
from app.util import fmt_hm
from app.web.templating import flash, templates

log = logging.getLogger("app.web.import")

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]

TRUE_VALUES = ("1", "true", "on", "yes", "ja")
PROBLEM_ROWS_MAX = 300
ALL_ROWS_MAX = 200
STEPS = ("Datei", "Spalten", "Prüfen", "Übernehmen")
STAGE_LABELS = {
    svc.STAGE_UPLOADED: ("hochgeladen", "badge-outline"),
    svc.STAGE_VALIDATED: ("geprüft", "badge-primary"),
    svc.STAGE_COMMITTED: ("importiert", "badge-success"),
    svc.STAGE_UNDONE: ("rückgängig", "badge-warn"),
}
ROW_STATUS = {
    svc.STATUS_OK: ("ok", "badge-success"),
    svc.STATUS_ERROR: ("Fehler", "badge-danger"),
    svc.STATUS_DUPLICATE: ("Duplikat", "badge-warn"),
    svc.STATUS_EMPTY: ("leer", "badge-outline"),
    svc.STATUS_IGNORED: ("Summe", "badge-outline"),
}
CATEGORY_SOURCES = {"manuell": "von dir zugeordnet", "gemerkt": "gemerkt", "katalog": "Katalog",
                    "standard": "Standard"}


# --------------------------------------------------------------------------- helpers


def _truthy(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in TRUE_VALUES


def _batch_or_404(db: Session, batch_id: str):
    try:
        return svc.get_batch(db, batch_id)
    except svc.ImportNotFound as exc:
        raise HTTPException(404, exc.message) from None


def _stage(batch) -> str | None:
    return (batch.mapping_json or {}).get("stage")


def _int_or_none(text: Any) -> int | None:
    text = str(text or "").strip()
    if not text:
        return None
    try:
        return max(0, int(text))
    except ValueError:
        return None


def _base_ctx(step: int, **extra: Any) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "page_title": "Import",
        "step": step,
        "steps": STEPS,
        "stage_labels": STAGE_LABELS,
        "row_status": ROW_STATUS,
        "category_sources": CATEGORY_SOURCES,
        "error": None,
    }
    ctx.update(extra)
    return ctx


def _sheet_query(sheets: list[str], header_row: int | None) -> str:
    params: list[tuple[str, str]] = [("blatt", s) for s in sheets]
    if header_row is not None:
        params.append(("kopfzeile", str(header_row)))
    return urlencode(params)


def _guard_open(request: Request, batch) -> Response | None:
    """Batches that are committed/undone cannot run steps 1–3 again."""
    stage = _stage(batch)
    if stage == svc.STAGE_COMMITTED:
        flash(request, "Dieser Import wurde bereits übernommen.", "info")
        return RedirectResponse(f"/import/{batch.id}/ergebnis", status_code=303)
    if stage == svc.STAGE_UNDONE or batch.undone_at is not None:
        flash(request, "Dieser Import wurde rückgängig gemacht. Bitte die Datei erneut hochladen.", "warn")
        return RedirectResponse("/import", status_code=303)
    return None


# --------------------------------------------------------------------------- step 1: upload


def _overview_ctx(db: Session, **extra: Any) -> dict[str, Any]:
    try:
        batches = svc.list_batches(db)
        mappings = svc.list_mappings(db)
    except Exception:  # pragma: no cover - the upload must stay usable
        log.exception("Import-Liste konnte nicht geladen werden")
        db.rollback()
        batches, mappings = [], []
    return _base_ctx(1, batches=batches, mappings=mappings,
                     max_mb=svc.MAX_UPLOAD_BYTES // (1024 * 1024), **extra)


@router.get("/import", response_class=HTMLResponse)
def import_start(request: Request, db: DbSession) -> HTMLResponse:
    return templates.TemplateResponse(request, "import.html", _overview_ctx(db))


@router.post("/import", response_class=HTMLResponse)
async def import_upload(request: Request, db: DbSession) -> Response:
    form = await request.form()
    upload = form.get("datei")
    if not isinstance(upload, UploadFile) or not upload.filename:
        ctx = _overview_ctx(db, error="Bitte eine CSV- oder XLSX-Datei auswählen.")
        return templates.TemplateResponse(request, "import.html", ctx, status_code=422)
    content = await upload.read(svc.MAX_UPLOAD_BYTES + 1)
    try:
        preview = svc.upload(db, content, upload.filename)
    except svc.ImportServiceError as exc:
        db.rollback()
        ctx = _overview_ctx(db, error=exc.message)
        return templates.TemplateResponse(request, "import.html", ctx, status_code=exc.status_code)
    return RedirectResponse(f"/import/{preview['batch_id']}", status_code=303)


@router.get("/import/{batch_id}", response_class=HTMLResponse)
def import_preview(batch_id: str, request: Request, db: DbSession) -> Response:
    batch = _batch_or_404(db, batch_id)
    guard = _guard_open(request, batch)
    if guard is not None:
        return guard
    q = request.query_params
    state = batch.mapping_json or {}
    selected = [s for s in q.getlist("blatt") if s] or list(state.get("selected_sheets") or [])
    header_row = _int_or_none(q.get("kopfzeile")) if "kopfzeile" in q else state.get("header_row")
    view_sheet = q.get("ansicht") or (selected[0] if selected else None)
    error = None
    try:
        preview = svc.preview(db, batch.id, sheet=view_sheet, header_row=header_row)
    except svc.ImportValidationError as exc:
        error = exc.message
        header_row = None
        preview = svc.preview(db, batch.id, sheet=view_sheet if view_sheet in _sheets(db, batch) else None)
    except svc.ImportServiceError as exc:
        raise HTTPException(exc.status_code, exc.message) from None
    if not selected and preview["sheet"]:
        selected = [preview["sheet"]]
    ctx = _base_ctx(
        1,
        batch=batch,
        preview=preview,
        selected_sheets=selected,
        header_row=header_row,
        error=error,
        next_query=_sheet_query(selected, header_row),
    )
    return templates.TemplateResponse(request, "import_preview.html", ctx)


def _sheets(db: Session, batch) -> list[str]:
    return list((batch.mapping_json or {}).get("sheets") or [])


# --------------------------------------------------------------------------- step 2: mapping


def _column_options(preview: dict[str, Any]) -> list[dict[str, Any]]:
    options = []
    for col in preview["columns"]:
        sample = ", ".join(s for s in col["samples"][:2] if s)
        options.append({
            "value": str(col["index"]),
            "label": f"{col['letter']} · {col['header']}" + (f" (z. B. {sample})" if sample else ""),
            "header": col["header"],
        })
    return options


def _mapping_ctx(db: Session, batch, sheets: list[str], header_row: int | None,
                 mapping: dict[str, Any] | None, options: dict[str, Any] | None, error: str | None = None
                 ) -> dict[str, Any]:
    preview = svc.preview(db, batch.id, sheet=sheets[0] if sheets else None, header_row=header_row)
    columns = _column_options(preview)
    current = dict(preview["mapping"])
    if mapping:
        headers = [c["header"] for c in columns]
        for field in svc.FIELDS:
            value = mapping.get(field)
            if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
                current[field] = int(value)
            elif isinstance(value, str) and value in headers:
                current[field] = headers.index(value)
            elif value in (None, ""):
                current[field] = None
    options = options or {}
    return _base_ctx(
        2,
        batch=batch,
        preview=preview,
        sheets=sheets,
        header_row=header_row,
        columns=columns,
        mapping={k: ("" if v is None else str(v)) for k, v in current.items()},
        fields=[{"key": f, "label": svc.FIELD_LABELS[f]} for f in svc.FIELDS],
        catalogue=svc.catalogue(db),
        default_category=options.get("default_category") or "",
        error=error,
        back_query=_sheet_query(sheets, header_row),
    )


@router.get("/import/{batch_id}/zuordnung", response_class=HTMLResponse)
def import_mapping(batch_id: str, request: Request, db: DbSession) -> Response:
    batch = _batch_or_404(db, batch_id)
    guard = _guard_open(request, batch)
    if guard is not None:
        return guard
    q = request.query_params
    state = batch.mapping_json or {}
    sheets = [s for s in q.getlist("blatt") if s] or list(state.get("selected_sheets") or [])
    header_row = _int_or_none(q.get("kopfzeile")) if "kopfzeile" in q else state.get("header_row")
    try:
        ctx = _mapping_ctx(db, batch, sheets, header_row, state.get("mapping"), state.get("options"))
    except svc.ImportValidationError as exc:
        flash(request, exc.message, "danger")
        return RedirectResponse(f"/import/{batch.id}", status_code=303)
    except svc.ImportServiceError as exc:
        raise HTTPException(exc.status_code, exc.message) from None
    return templates.TemplateResponse(request, "import_mapping.html", ctx)


# --------------------------------------------------------------------------- step 3: check


def _form_request(form: FormData, batch) -> dict[str, Any]:
    """Mapping, sheets, header row and options from the step 2/3 form."""
    sheets = [str(s) for s in form.getlist("blatt") if str(s).strip()]
    header_row = _int_or_none(form.get("kopfzeile"))
    mapping: dict[str, Any] = {}
    for field in svc.FIELDS:
        raw = str(form.get(f"map_{field}") or "").strip()
        mapping[field] = int(raw) if raw.isdigit() else None
    category_map: dict[str, str] = {}
    texts = form.getlist("kat_text")
    codes = form.getlist("kat_code")
    origs = form.getlist("kat_orig")
    for i, text in enumerate(texts):
        text = str(text).strip()
        code = str(codes[i]).strip() if i < len(codes) else ""
        orig = str(origs[i]).strip() if i < len(origs) else ""
        if text and code and code != orig:
            category_map[text] = code
    state_options = (batch.mapping_json or {}).get("options") or {}
    # Keep earlier manual choices that are not shown as changed in this form.
    for text, code in (state_options.get("category_map") or {}).items():
        category_map.setdefault(text, code)
    sent = _truthy(form.get("optionen"))
    options = {
        "default_category": str(form.get("standard_kategorie") or "").strip() or None,
        "category_map": category_map,
        "skip_errors": True,
        "skip_duplicates": _truthy(form.get("duplikate_ueberspringen")) if sent else True,
        "remember_mappings": _truthy(form.get("merken")) if sent else True,
        "include_rows": [str(r) for r in form.getlist("einschliessen")],
        "exclude_rows": [str(r) for r in form.getlist("ausschliessen")],
        "reason": str(form.get("begruendung") or "").strip() or None,
        "include_outside_periods": _truthy(form.get("ausserhalb_importieren")) if sent else False,
    }
    return {"mapping": mapping, "sheets": sheets or None, "header_row": header_row, "options": options}


def _by_header(db: Session, batch, req: dict[str, Any]) -> dict[str, Any]:
    """With several sheets, translate column indexes into header texts (resolved per sheet)."""
    sheets = req["sheets"] or []
    if len(sheets) < 2:
        return req["mapping"]
    preview = svc.preview(db, batch.id, sheet=sheets[0], header_row=req["header_row"])
    if not preview["header_row"]:
        return req["mapping"]
    headers = preview["headers"]
    return {f: (headers[i] if isinstance(i, int) and i < len(headers) else i) for f, i in req["mapping"].items()}


def _check_ctx(db: Session, batch, req: dict[str, Any], report: dict[str, Any] | None,
               error: str | None = None) -> dict[str, Any]:
    rows = report["rows"] if report else []
    problems = [r for r in rows if r["status"] in (svc.STATUS_ERROR, svc.STATUS_DUPLICATE) or r["warnings"]]
    categories: list[dict[str, Any]] = []
    if report:
        for u in report["unmapped_categories"]:
            categories.append({"text": u["text"], "count": u["count"], "code": "", "source": None,
                               "rows": u["rows"]})
        for c in report["category_resolution"]:
            categories.append({"text": c["text"], "count": c["count"], "code": c["code"], "source": c["source"],
                               "rows": []})
    opts = (report or {}).get("options") or req["options"]
    mapped = [svc.FIELD_LABELS[f] for f, v in req["mapping"].items() if v is not None]
    return _base_ctx(
        3,
        batch=batch,
        report=report,
        req=req,
        opts=opts,
        sheets=req["sheets"] or [],
        header_row=req["header_row"],
        mapping={f: ("" if v is None else str(v)) for f, v in req["mapping"].items()},
        mapped_labels=mapped,
        problems=problems[:PROBLEM_ROWS_MAX],
        problems_total=len(problems),
        all_rows=rows[:ALL_ROWS_MAX],
        rows_total=len(rows),
        categories=categories,
        catalogue=svc.catalogue(db),
        error=error,
        back_query=_sheet_query(req["sheets"] or [], req["header_row"]),
    )


def _run_check(db: Session, batch, req: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    try:
        report = svc.validate(db, batch.id, _by_header(db, batch, req), sheets=req["sheets"],
                              header_row=req["header_row"], options=req["options"])
        return report, None
    except svc.ImportServiceError as exc:
        db.rollback()
        return None, exc.message


@router.post("/import/{batch_id}/pruefen", response_class=HTMLResponse)
async def import_check(batch_id: str, request: Request, db: DbSession) -> Response:
    batch = _batch_or_404(db, batch_id)
    guard = _guard_open(request, batch)
    if guard is not None:
        return guard
    form = await request.form()
    req = _form_request(form, batch)
    report, error = _run_check(db, batch, req)
    if report is None and _truthy(form.get("aus_zuordnung")):
        # Invalid mapping: stay on step 2 with the user's choices.
        try:
            ctx = _mapping_ctx(db, batch, req["sheets"] or [], req["header_row"], req["mapping"], req["options"],
                               error=error)
        except svc.ImportServiceError as exc:
            raise HTTPException(exc.status_code, exc.message) from None
        return templates.TemplateResponse(request, "import_mapping.html", ctx, status_code=422)
    ctx = _check_ctx(db, batch, req, report, error)
    return templates.TemplateResponse(request, "import_check.html", ctx, status_code=200 if report else 422)


@router.get("/import/{batch_id}/pruefen", response_class=HTMLResponse)
def import_check_again(batch_id: str, request: Request, db: DbSession) -> Response:
    batch = _batch_or_404(db, batch_id)
    guard = _guard_open(request, batch)
    if guard is not None:
        return guard
    state = batch.mapping_json or {}
    if not state.get("mapping"):
        return RedirectResponse(f"/import/{batch.id}/zuordnung", status_code=303)
    mapping = {f: state["mapping"].get(f) for f in svc.FIELDS}
    req = {"mapping": mapping, "sheets": state.get("selected_sheets") or None,
           "header_row": state.get("header_row"), "options": state.get("options") or {}}
    report, error = _run_check(db, batch, req)
    if report is not None:
        # The state may hold header texts (several sheets): the hidden fields need column indexes.
        first_sheet = (req["sheets"] or [""])[0] or ""
        cols = report["columns"].get(first_sheet) or next(iter(report["columns"].values()), None)
        if cols:
            req["mapping"] = {f: cols.get(f) for f in svc.FIELDS}
    ctx = _check_ctx(db, batch, req, report, error)
    return templates.TemplateResponse(request, "import_check.html", ctx)


# --------------------------------------------------------------------------- step 4: commit / result


@router.post("/import/{batch_id}/uebernehmen", response_class=HTMLResponse)
async def import_commit(batch_id: str, request: Request, db: DbSession) -> Response:
    batch = _batch_or_404(db, batch_id)
    guard = _guard_open(request, batch)
    if guard is not None:
        return guard
    form = await request.form()
    req = _form_request(form, batch)
    try:
        result = svc.commit(db, batch.id, _by_header(db, batch, req), sheets=req["sheets"],
                            header_row=req["header_row"], options=req["options"])
    except svc.ImportConflict as exc:
        db.rollback()
        flash(request, exc.message, "warn")
        return RedirectResponse(f"/import/{batch.id}/ergebnis", status_code=303)
    except svc.ImportServiceError as exc:
        db.rollback()
        report, _ = _run_check(db, batch, req)
        ctx = _check_ctx(db, batch, req, report, exc.message)
        return templates.TemplateResponse(request, "import_check.html", ctx, status_code=exc.status_code)
    message = f"{result['imported_rows']} Zeile(n) übernommen ({result['entries_created']} Einträge)."
    if result["mappings_remembered"]:
        message += f" {result['mappings_remembered']} Kategorie-Zuordnung(en) gemerkt."
    flash(request, message, "success")
    return RedirectResponse(f"/import/{batch.id}/ergebnis", status_code=303)


def _batch_months(db: Session, batch_id: str) -> list[dict[str, Any]]:
    """Imported hours per month (active entries of the batch)."""
    sums: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    q = select(TimeEntry.day, TimeEntry.duration_seconds).where(
        TimeEntry.import_batch_id == batch_id, TimeEntry.deleted_at.is_(None))
    for day, secs in db.execute(q):
        key = f"{day.year:04d}-{day.month:02d}"
        sums[key][0] += 1
        sums[key][1] += secs or 0
    result = []
    for ym, (count, secs) in sorted(sums.items()):
        first = date(int(ym[:4]), int(ym[5:]), 1)
        last = date(first.year + first.month // 12, first.month % 12 + 1, 1) - timedelta(days=1)
        # /monate/{ym} only exists for months inside a period; otherwise link the entry list of that month.
        in_period = any(cal.period_for_date(db, d) is not None for d in (first, last))
        result.append({
            "ym": ym, "count": count, "hm": fmt_hm(secs), "in_period": in_period,
            "entries_href": f"/eintraege?{urlencode({'von': first.isoformat(), 'bis': last.isoformat()})}",
        })
    return result


@router.get("/import/{batch_id}/ergebnis", response_class=HTMLResponse)
def import_result(batch_id: str, request: Request, db: DbSession) -> Response:
    batch = _batch_or_404(db, batch_id)
    if _stage(batch) not in (svc.STAGE_COMMITTED, svc.STAGE_UNDONE):
        return RedirectResponse(f"/import/{batch.id}", status_code=303)
    info = svc.batch_to_dict(db, batch)
    counts = (batch.mapping_json or {}).get("counts") or {}
    ctx = _base_ctx(4, batch=batch, info=info, skipped=counts.get("skipped") or {},
                    months=_batch_months(db, batch.id))
    return templates.TemplateResponse(request, "import_result.html", ctx)


# --------------------------------------------------------------------------- undo


def _undo_ctx(db: Session, batch, error: str | None = None, reason: str = "") -> dict[str, Any]:
    info = svc.batch_to_dict(db, batch)
    return _base_ctx(0, page_title="Import rückgängig", batch=batch, info=info, error=error, reason=reason,
                     committed=_stage(batch) == svc.STAGE_COMMITTED)


@router.get("/import/{batch_id}/rueckgaengig", response_class=HTMLResponse)
def import_undo_page(batch_id: str, request: Request, db: DbSession) -> Response:
    batch = _batch_or_404(db, batch_id)
    if _stage(batch) == svc.STAGE_UNDONE or batch.undone_at is not None:
        flash(request, "Dieser Import wurde bereits rückgängig gemacht.", "info")
        return RedirectResponse("/import", status_code=303)
    return templates.TemplateResponse(request, "import_undo.html", _undo_ctx(db, batch))


@router.post("/import/{batch_id}/rueckgaengig", response_class=HTMLResponse)
async def import_undo(batch_id: str, request: Request, db: DbSession) -> Response:
    batch = _batch_or_404(db, batch_id)
    form = await request.form()
    reason = str(form.get("begruendung") or "").strip()
    if not _truthy(form.get("bestaetigt")):
        ctx = _undo_ctx(db, batch, "Bitte bestätigen, dass die Einträge dieses Imports gelöscht werden.", reason)
        return templates.TemplateResponse(request, "import_undo.html", ctx, status_code=422)
    try:
        result = svc.undo(db, batch.id, reason=reason or None)
    except svc.ImportLocked as exc:
        db.rollback()
        ctx = _undo_ctx(db, batch, exc.message, reason)
        return templates.TemplateResponse(request, "import_undo.html", ctx, status_code=exc.status_code)
    except svc.ImportConflict as exc:
        db.rollback()
        flash(request, exc.message, "warn")
        return RedirectResponse("/import", status_code=303)
    if result.get("discarded"):
        flash(request, "Unvollständiger Import verworfen.", "success")
    else:
        flash(request, f"Import rückgängig gemacht: {result['entries_deleted']} Einträge gelöscht "
                       "(in der Historie nachvollziehbar).", "success")
    return RedirectResponse("/import", status_code=303)


# --------------------------------------------------------------------------- mapping memory


@router.post("/import/zuordnungen/loeschen", response_class=HTMLResponse)
async def import_forget_mapping(request: Request, db: DbSession) -> Response:
    form = await request.form()
    text = str(form.get("source_text") or "")
    try:
        svc.delete_mapping(db, text)
        flash(request, f"Zuordnung „{text}“ vergessen.", "success")
    except svc.ImportServiceError as exc:
        db.rollback()
        flash(request, exc.message, "warn")
    return RedirectResponse("/import#zuordnungen", status_code=303)
