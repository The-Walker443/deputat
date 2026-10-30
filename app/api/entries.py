"""JSON API – Time entries: GET/POST /entries, GET/PATCH/DELETE /entries/{id}, GET /entries/{id}/revisions,
POST /entries/{id}/restore. All writes go through services/entries.py (revisions, overlap check, midnight
split, locks).

Mounted by app.main under the ``/api`` prefix, so paths here are relative to ``/api``.
Domain errors map to ``{"detail": "..."}`` with 400 (validation), 404, 409 (overlap/conflict) or
423 (closed month without reason).

Provenance: entries created through this API are always ``source='manuell'`` and ``status='active'``.
Imported entries come only from services/import_service.py (with an import batch, undoable), timetable
suggestions only from services/timetable.py, timer entries only from /api/timer – a client must not be
able to fake the "Quelle" column or bypass the overlap check (imports/suggestions are exempt from it).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.db.models import EntrySource, EntryStatus
from app.db.session import get_db
from app.services import entries as svc

router = APIRouter(tags=["entries"])

DbSession = Annotated[Session, Depends(get_db)]


def raise_http(exc: svc.DomainError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


class EntryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_at: datetime | None = None
    end_at: datetime | None = None
    duration_seconds: int | None = None
    duration: str | None = None  # alternative to duration_seconds: "2,5", "2:30", "150 min"
    has_bounds: bool | None = None
    day: date | None = None
    category_id: int
    subcategory_id: int | None = None
    klasse: str | None = None
    fach: str | None = None
    location: str | None = None
    note: str | None = None
    flags: list[str] | None = None
    status: str | None = None
    source: str = EntrySource.MANUELL.value
    timetable_slot_id: str | None = None
    reason: str | None = None


class EntryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_at: datetime | None = None
    end_at: datetime | None = None
    duration_seconds: int | None = None
    duration: str | None = None
    has_bounds: bool | None = None
    day: date | None = None
    category_id: int | None = None
    subcategory_id: int | None = None
    klasse: str | None = None
    fach: str | None = None
    location: str | None = None
    note: str | None = None
    flags: list[str] | None = None
    status: str | None = None
    timetable_slot_id: str | None = None
    reason: str | None = None


class ReasonBody(BaseModel):
    reason: str | None = None


def _payload(model: BaseModel) -> tuple[dict[str, Any], str | None]:
    data = model.model_dump(exclude_unset=True)
    reason = data.pop("reason", None)
    duration = data.pop("duration", None)
    if duration is not None and "duration_seconds" not in data:
        data["duration_seconds"] = duration
    return data, reason


@router.get("/entries")
def list_entries(
    db: DbSession,
    date_from: date | None = None,
    date_to: date | None = None,
    category_id: int | None = None,
    source: str | None = None,
    status_: Annotated[str | None, Query(alias="status")] = None,
    flag: Annotated[list[str] | None, Query()] = None,
    include_deleted: bool = False,
    limit: Annotated[int | None, Query(ge=1, le=5000)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    if source is not None and source not in {s.value for s in EntrySource}:
        raise HTTPException(422, f"Unbekannte Quelle „{source}“.")
    if status_ is not None and status_ not in {s.value for s in EntryStatus}:
        raise HTTPException(422, f"Unbekannter Status „{status_}“.")
    rows = svc.list_entries(
        db, date_from=date_from, date_to=date_to, category_id=category_id, source=source, status=status_,
        flags=flag, include_deleted=include_deleted, limit=limit, offset=offset,
    )
    return {"entries": [svc.entry_to_dict(e) for e in rows], "count": len(rows)}


@router.post("/entries", status_code=status.HTTP_201_CREATED)
def create_entry(body: EntryCreate, db: DbSession) -> dict[str, Any]:
    data, reason = _payload(body)
    source = data.pop("source", EntrySource.MANUELL.value)
    if source == EntrySource.TIMER.value:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Timer-Einträge bitte über /api/timer anlegen.")
    if source != EntrySource.MANUELL.value:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Über diese Schnittstelle können nur manuelle Einträge angelegt werden "
            "(Importe über den Import-Assistenten, Stundenplan-Vorschläge über den Stundenplan).",
        )
    if data.get("status") not in (None, EntryStatus.ACTIVE.value):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Neue Einträge sind immer aktiv.")
    try:
        created = svc.create_entry(db, data, source=source, reason=reason)
    except svc.DomainError as exc:
        db.rollback()
        raise_http(exc)
    return {"entries": [svc.entry_to_dict(e) for e in created], "entry": svc.entry_to_dict(created[0])}


@router.get("/entries/{entry_id}")
def get_entry(entry_id: str, db: DbSession) -> dict[str, Any]:
    try:
        return svc.entry_to_dict(svc.get_entry(db, entry_id))
    except svc.DomainError as exc:
        raise_http(exc)


@router.patch("/entries/{entry_id}")
def update_entry(entry_id: str, body: EntryUpdate, db: DbSession) -> dict[str, Any]:
    changes, reason = _payload(body)
    if changes.get("status") not in (None, EntryStatus.ACTIVE.value):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Der Status kann hier nur auf „aktiv“ gesetzt werden (Löschen über DELETE).",
        )
    try:
        result = svc.update_entry(db, entry_id, changes, reason=reason)
    except svc.DomainError as exc:
        db.rollback()
        raise_http(exc)
    return {"entry": svc.entry_to_dict(result[0]), "entries": [svc.entry_to_dict(e) for e in result]}


@router.delete("/entries/{entry_id}")
def delete_entry(
    entry_id: str,
    db: DbSession,
    reason: str | None = None,
    body: Annotated[ReasonBody | None, Body()] = None,
) -> dict[str, Any]:
    if body is not None and body.reason:
        reason = body.reason
    try:
        entry = svc.delete_entry(db, entry_id, reason=reason)
    except svc.DomainError as exc:
        db.rollback()
        raise_http(exc)
    return {"entry": svc.entry_to_dict(entry)}


@router.post("/entries/{entry_id}/restore")
def restore_entry(
    entry_id: str,
    db: DbSession,
    body: Annotated[ReasonBody | None, Body()] = None,
) -> dict[str, Any]:
    try:
        entry = svc.restore_entry(db, entry_id, reason=body.reason if body else None)
    except svc.DomainError as exc:
        db.rollback()
        raise_http(exc)
    return {"entry": svc.entry_to_dict(entry)}


@router.get("/entries/{entry_id}/revisions")
def entry_revisions(entry_id: str, db: DbSession) -> dict[str, Any]:
    try:
        rows = svc.revisions(db, entry_id)
    except svc.DomainError as exc:
        raise_http(exc)
    return {"revisions": [svc.revision_to_dict(r) for r in rows]}
