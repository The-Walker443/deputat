"""JSON API – Import wizard (Konzept §12). Uses services/import_service.py.

Endpoints (relative to ``/api``):

* ``POST /import/preview`` (multipart: ``file``, optional ``sheet``) → 201, batch id + preview + guessed mapping
* ``GET /import`` → batch list; ``GET /import/{batch_id}?sheet=&header_row=`` → batch + preview
* ``POST /import/{batch_id}/validate`` ``{mapping, sheets?|sheet?, header_row?, options?}`` → report
* ``POST /import/commit`` ``{batch_id, mapping?, sheets?|sheet?, header_row?, options?}`` → result
* ``DELETE /import/{batch_id}`` (``?reason=`` or JSON ``{reason}``) → undo whole batch / discard upload
* ``GET /import/mappings``, ``PUT /import/mappings`` ``{source_text, category_code}``,
  ``DELETE /import/mappings?source_text=`` – category mapping memory (Merkfunktion)

Mounted by app.main under the ``/api`` prefix without auth; unsafe methods need the CSRF header.
Errors: ``{"detail": "..."}`` with 404, 409 (already committed/undone), 413 (file too large),
422 (invalid file/mapping/options) or 423 (undo touches closed months without reason).
"""

from __future__ import annotations

from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import import_service as svc

router = APIRouter(tags=["import"])

DbSession = Annotated[Session, Depends(get_db)]


def raise_http(exc: svc.ImportServiceError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


class ImportOptionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_category: str | None = None
    category_map: dict[str, str | None] | None = None
    remember_mappings: bool | None = None
    skip_errors: bool | None = None
    skip_duplicates: bool | None = None
    include_rows: list[str | int] | None = None
    exclude_rows: list[str | int] | None = None
    reason: str | None = None


class ValidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mapping: dict[str, int | str | None]
    sheet: str | None = None
    sheets: list[str] | None = None
    header_row: int | None = None
    options: ImportOptionsIn | None = None


class CommitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str
    mapping: dict[str, int | str | None] | None = None
    sheet: str | None = None
    sheets: list[str] | None = None
    header_row: int | None = None
    options: ImportOptionsIn | None = None


class ReasonBody(BaseModel):
    reason: str | None = None


class MappingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_text: str
    category_code: str


def _sheets(body: ValidateBody | CommitBody) -> list[str] | None:
    if body.sheets:
        return body.sheets
    return [body.sheet] if body.sheet else None


def _options(body: ValidateBody | CommitBody) -> dict[str, Any] | None:
    return body.options.model_dump(exclude_none=True) if body.options is not None else None


@router.post("/import/preview", status_code=status.HTTP_201_CREATED)
def upload_preview(
    db: DbSession,
    file: Annotated[UploadFile, File(description="CSV- oder XLSX-Datei")],
    sheet: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    content = file.file.read(svc.MAX_UPLOAD_BYTES + 1)
    try:
        return svc.upload(db, content, file.filename, sheet=sheet or None)
    except svc.ImportServiceError as exc:
        raise_http(exc)


@router.get("/import")
def list_batches(db: DbSession) -> dict[str, Any]:
    batches = svc.list_batches(db)
    return {"batches": batches, "count": len(batches)}


@router.get("/import/mappings")
def list_mappings(db: DbSession) -> dict[str, Any]:
    mappings = svc.list_mappings(db)
    return {"mappings": mappings, "count": len(mappings), "categories": svc.catalogue(db)}


@router.put("/import/mappings")
def save_mapping(body: MappingIn, db: DbSession) -> dict[str, Any]:
    try:
        return {"mapping": svc.save_mapping(db, body.source_text, body.category_code)}
    except svc.ImportServiceError as exc:
        db.rollback()
        raise_http(exc)


@router.delete("/import/mappings")
def delete_mapping(db: DbSession, source_text: Annotated[str, Query(min_length=1)]) -> dict[str, Any]:
    try:
        svc.delete_mapping(db, source_text)
    except svc.ImportServiceError as exc:
        raise_http(exc)
    return {"deleted": source_text}


@router.post("/import/commit")
def commit(body: CommitBody, db: DbSession) -> dict[str, Any]:
    try:
        return svc.commit(db, body.batch_id, body.mapping, sheets=_sheets(body), header_row=body.header_row,
                          options=_options(body))
    except svc.ImportServiceError as exc:
        db.rollback()
        raise_http(exc)


@router.get("/import/{batch_id}")
def get_batch(
    batch_id: str,
    db: DbSession,
    sheet: str | None = None,
    header_row: Annotated[int | None, Query(ge=0)] = None,
) -> dict[str, Any]:
    try:
        batch = svc.get_batch(db, batch_id)
        result: dict[str, Any] = {"batch": svc.batch_to_dict(db, batch)}
        try:
            result["preview"] = svc.preview(db, batch_id, sheet=sheet, header_row=header_row)
        except svc.ImportNotFound:
            result["preview"] = None  # original file gone; batch info is still useful
        return result
    except svc.ImportServiceError as exc:
        raise_http(exc)


@router.post("/import/{batch_id}/validate")
def validate(batch_id: str, body: ValidateBody, db: DbSession) -> dict[str, Any]:
    try:
        return svc.validate(db, batch_id, body.mapping, sheets=_sheets(body), header_row=body.header_row,
                            options=_options(body))
    except svc.ImportServiceError as exc:
        db.rollback()
        raise_http(exc)


@router.delete("/import/{batch_id}")
def undo(
    batch_id: str,
    db: DbSession,
    reason: str | None = None,
    body: Annotated[ReasonBody | None, Body()] = None,
) -> dict[str, Any]:
    if body is not None and body.reason:
        reason = body.reason
    try:
        return svc.undo(db, batch_id, reason=reason)
    except svc.ImportServiceError as exc:
        db.rollback()
        raise_http(exc)
