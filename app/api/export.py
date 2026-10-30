"""JSON API – Exports (Konzept §10.4): CSV, JSON full backup, ICS, and the JSON restore.

Mounted by app.main under the ``/api`` prefix. No login (DEPLOY.md: export downloads
are reachable in the LAN without login – accepted, documented in DEPLOY.md). Every generated file is
also stored under ``DATA_DIR/exports`` (bounded: see ``services.export.cleanup_exports``). CSV and ICS
carry a Prüf-Hash over the exported entries.

- ``GET  /export/csv?date_from&date_to&include_deleted`` – entries, UTF-8 BOM, ``;``, decimal comma
- ``GET  /export/json``                                   – full backup of all tables
- ``GET  /export/ics?date_from&date_to``                   – entries as calendar (read-only view)
- ``POST /export/restore``                                 – JSON backup upload; requires ``confirm=true``
  (form field or query). Accepts ``multipart/form-data`` (field ``file``) or a raw JSON body.
  ``validate_only=true`` only checks the file and returns the row counts.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import export as svc

router = APIRouter(tags=["export"])
log = logging.getLogger(__name__)

DB = Annotated[Session, Depends(get_db)]

TRUE_VALUES = ("1", "true", "yes", "on", "ja")


def _attachment(content: bytes, filename: str, media_type: str) -> Response:
    try:
        svc.store_export(filename, content)
    except OSError as exc:  # storing is a convenience; the download must still work
        log.warning("Export %s could not be stored: %s", filename, exc)
    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


def _range(date_from: date | None, date_to: date | None) -> None:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "„Von“ liegt nach „Bis“.")


@router.get("/export/csv")
def export_csv(
    db: DB,
    date_from: date | None = None,
    date_to: date | None = None,
    include_deleted: bool = False,
    include_vorschlaege: bool = False,
) -> Response:
    _range(date_from, date_to)
    content = svc.export_csv(
        db, date_from=date_from, date_to=date_to, include_deleted=include_deleted,
        include_vorschlaege=include_vorschlaege,
    )
    return _attachment(content, f"deputat_eintraege_{svc.timestamp_slug()}.csv", "text/csv; charset=utf-8")


@router.get("/export/json")
def export_json(db: DB) -> Response:
    content = svc.export_json_bytes(db)
    return _attachment(content, f"deputat_backup_{svc.timestamp_slug()}.json", "application/json")


@router.get("/export/ics")
def export_ics(db: DB, date_from: date | None = None, date_to: date | None = None) -> Response:
    _range(date_from, date_to)
    content = svc.export_ics(db, date_from=date_from, date_to=date_to)
    return _attachment(content, f"deputat_eintraege_{svc.timestamp_slug()}.ics", "text/calendar; charset=utf-8")


async def _read_upload(request: Request) -> tuple[bytes, dict[str, str]]:
    """Return (file content, form fields) for multipart or raw JSON bodies."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form(max_files=1, max_fields=20)
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            raise HTTPException(422, "Bitte eine JSON-Sicherung als Datei („file“) hochladen.")
        content = await upload.read(svc.MAX_RESTORE_BYTES + 1)
        fields = {k: v for k, v in form.items() if isinstance(v, str)}
        return content, fields
    body = await request.body()
    if not body:
        raise HTTPException(422, "Keine Sicherung übermittelt.")
    return body, {}


@router.post("/export/restore")
async def restore(
    request: Request,
    db: DB,
    confirm: Annotated[bool, Query(description="Bestätigung: vorhandene Daten werden ersetzt")] = False,
    validate_only: bool = False,
) -> dict[str, Any]:
    content, fields = await _read_upload(request)
    if len(content) > svc.MAX_RESTORE_BYTES:
        raise HTTPException(413, "Die Datei ist zu groß.")
    confirmed = confirm or fields.get("confirm", "").strip().lower() in TRUE_VALUES
    validate_only = validate_only or fields.get("validate_only", "").strip().lower() in TRUE_VALUES
    # Parsing, validation, the safety backup and the restore are blocking work: never on the event loop
    # (it would stall every request including /healthz).
    return await run_in_threadpool(_restore_sync, db, content, confirmed, validate_only)


def _restore_sync(db: Session, content: bytes, confirmed: bool, validate_only: bool) -> dict[str, Any]:
    try:
        data = svc.parse_backup(content)
        rows = svc.validate_backup(db, data)
        if validate_only:
            return {
                "valid": True,
                "restored": False,
                "counts": {name: len(r) for name, r in rows.items()},
                "created_at": data.get("created_at"),
                "app_version": data.get("app_version"),
                "datenbank_hat_daten": svc.has_user_data(db),
            }
        if not confirmed:
            raise HTTPException(
                400,
                "Wiederherstellen ersetzt alle vorhandenen Daten. Bitte mit confirm=true ausdrücklich bestätigen.",
            )
        result = svc.restore_json(db, data, replace=True)
    except svc.ExportError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    return {
        "valid": True,
        "restored": True,
        "replaced": result.replaced,
        "counts": result.counts,
        "sicherheitskopie": result.safety_backup,
    }
