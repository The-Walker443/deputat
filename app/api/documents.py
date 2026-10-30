"""JSON API – Letter generator (Konzept §11, Phase 2).

Mounted by app.main under ``/api`` (no login; unsafe methods need the CSRF header ``X-CSRF-Token``):

- ``GET  /api/documents``                 list (``?type=`` filter) + ``geltendmachung_wirksam_ab``
- ``GET  /api/documents/types``           types (label, purpose, suggested recipient) + placeholder docs
- ``POST /api/documents/{type}``          render from the account data and save (``save: false`` = preview)
- ``GET  /api/documents/{id}``            one document incl. ``rendered_md``
- ``POST /api/documents/{id}/sent``       mark as sent (``{"sent_on": "YYYY-MM-DD"}`` optional, default now)
- ``GET  /api/documents/{id}/download``   the Markdown file (attachment)

Every rendered/saved text starts with the non-removable notice (enforced in ``app.services.documents``).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import documents as docs

router = APIRouter(tags=["documents"])

DbSession = Annotated[Session, Depends(get_db)]


class DocumentIn(BaseModel):
    period_from: date | None = None
    period_to: date | None = None
    monat: str | None = Field(default=None, description="YYYY-MM (nur antrag_freizeitausgleich)")
    overrides: dict[str, str] = Field(default_factory=dict)
    text: str | None = Field(default=None, max_length=docs.MAX_MD_LENGTH, description="bearbeiteter Text")
    note: str | None = Field(default=None, max_length=2000)
    save: bool = True


class SentIn(BaseModel):
    sent_on: date | None = None


def _out(doc: Any, *, with_text: bool = True) -> dict[str, Any]:
    data = docs.document_dict(doc)
    if not with_text:
        data.pop("rendered_md", None)
    return data


def _get(db: Session, doc_id: str) -> Any:
    try:
        return docs.get_document(db, doc_id)
    except docs.DocumentNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None


@router.get("/documents")
def list_documents(db: DbSession, type: Annotated[str | None, Query()] = None) -> dict[str, Any]:
    try:
        items = docs.list_documents(db, type or None)
    except docs.DocumentError as exc:
        raise HTTPException(422, str(exc)) from None
    return {
        "documents": [_out(d, with_text=False) for d in items],
        "geltendmachung_wirksam_ab": docs.geltendmachung_wirksam_ab(db),
    }


@router.get("/documents/types")
def document_types() -> dict[str, Any]:
    return {
        "types": [{"type": t, **info} for t, info in docs.TYPE_INFO.items()],
        "placeholders": docs.PLACEHOLDERS,
        "overridable": list(docs.OVERRIDABLE),
        "notice": docs.NOTICE,
    }


@router.post("/documents/{doc_type}")
def create_document(doc_type: str, body: DocumentIn, db: DbSession) -> dict[str, Any]:
    if doc_type not in docs.TYPE_INFO:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unbekannter Schreiben-Typ „{doc_type}“.")
    overrides = dict(body.overrides)
    if body.monat:
        overrides["monat"] = body.monat
    try:
        if not body.save:
            result = docs.prepare(db, doc_type, body.period_from, body.period_to, overrides)
            return {"document": None, "markdown": result.markdown, "warnings": result.warnings,
                    "values": result.values}
        doc, result = docs.create(
            db, doc_type, body.period_from, body.period_to, overrides, text=body.text, note=body.note
        )
    except docs.DocumentError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"document": _out(doc), "markdown": doc.rendered_md, "warnings": result.warnings,
            "values": result.values}


@router.get("/documents/{doc_id}")
def get_document(doc_id: str, db: DbSession) -> dict[str, Any]:
    return _out(_get(db, doc_id))


@router.post("/documents/{doc_id}/sent")
def mark_sent(doc_id: str, db: DbSession, body: SentIn | None = None) -> dict[str, Any]:
    _get(db, doc_id)
    try:
        doc = docs.mark_sent(db, doc_id, body.sent_on if body else None)
    except docs.DocumentError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"document": _out(doc, with_text=False),
            "geltendmachung_wirksam_ab": docs.geltendmachung_wirksam_ab(db)}


@router.get("/documents/{doc_id}/download")
def download(doc_id: str, db: DbSession) -> Response:
    doc = _get(db, doc_id)
    name = docs.download_name(doc)
    return Response(
        docs.ensure_notice(doc.rendered_md),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
                 "Cache-Control": "no-store"},
    )
