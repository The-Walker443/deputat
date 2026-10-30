"""JSON API – Timer endpoints (Konzept §16):

- ``POST /timer/start``  {category_id, subcategory_id?, klasse?, fach?, location?, started_at?, note?, flags?,
  client_action_id?}  (a running timer is stopped at ``started_at`` first). ``flags``: list of
  ``klassenfahrt`` | ``nachtbereitschaft`` | ``vertretung_angeordnet`` | ``vertretung_genehmigt`` (the last two
  only for Unterricht; otherwise 400).
- ``POST /timer/switch`` same body (alias of start)
- ``POST /timer/stop``   {stopped_at?, client_action_id?}
- ``POST /timer/pause``  {at?, client_action_id?}
- ``POST /timer/resume`` {at?, client_action_id?}
- ``GET  /timer/status`` (also ``GET /timer``)

Start/stop times come from the device (offline queue); ``client_action_id`` makes actions idempotent.
Every endpoint answers with the timer status (see :func:`status_to_dict`). Errors: ``{"detail": ...}``
with 400 / 409 (no running timer, conflict, overlap) / 423.

Mounted by app.main under the ``/api`` prefix.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import entries as entries_service
from app.services import timer as svc
from app.services.entries import DomainError
from app.util import fmt_hm

router = APIRouter(tags=["timer"])

DbSession = Annotated[Session, Depends(get_db)]


class TimerStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_id: int
    subcategory_id: int | None = None
    klasse: str | None = None
    fach: str | None = None
    location: str | None = None
    note: str | None = None
    flags: list[str] | None = Field(default=None, max_length=8)
    started_at: datetime | None = None
    at: datetime | None = None  # alias for started_at (switch)
    client_action_id: str | None = None


class TimerStop(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stopped_at: datetime | None = None
    at: datetime | None = None
    client_action_id: str | None = None


class TimerAt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: datetime | None = None
    client_action_id: str | None = None


def status_to_dict(result: svc.TimerStatus) -> dict[str, Any]:
    return {
        "running": entries_service.entry_to_dict(result.running) if result.running else None,
        "elapsed_seconds": result.elapsed_seconds,
        "elapsed_hm": fmt_hm(result.elapsed_seconds),
        "paused": result.paused,
        "warnings": result.warnings,
        "crosses_midnight": result.crosses_midnight,
        "stopped": [entries_service.entry_to_dict(e) for e in result.stopped],
        "duplicate": result.duplicate,
        "auto_ended": result.auto_ended,
        "notices": result.notices,
        "server_time": result.server_time.isoformat() + "Z" if result.server_time else None,
    }


def _run(db: Session, action, *args, **kwargs) -> dict[str, Any]:
    try:
        return status_to_dict(action(db, *args, **kwargs))
    except DomainError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


def _start_kwargs(body: TimerStart) -> dict[str, Any]:
    return {
        "subcategory_id": body.subcategory_id,
        "klasse": body.klasse,
        "fach": body.fach,
        "location": body.location,
        "note": body.note,
        "flags": body.flags,
        "started_at": body.started_at or body.at,
        "client_action_id": body.client_action_id,
    }


@router.get("/timer")
@router.get("/timer/status")
def timer_status(db: DbSession) -> dict[str, Any]:
    return status_to_dict(svc.status(db))


@router.post("/timer/start")
def timer_start(body: TimerStart, db: DbSession) -> dict[str, Any]:
    return _run(db, svc.start, body.category_id, **_start_kwargs(body))


@router.post("/timer/switch")
def timer_switch(body: TimerStart, db: DbSession) -> dict[str, Any]:
    return _run(db, svc.switch, body.category_id, **_start_kwargs(body))


@router.post("/timer/stop")
def timer_stop(db: DbSession, body: Annotated[TimerStop | None, Body()] = None) -> dict[str, Any]:
    body = body or TimerStop()
    return _run(db, svc.stop, body.stopped_at or body.at, client_action_id=body.client_action_id)


@router.post("/timer/pause")
def timer_pause(db: DbSession, body: Annotated[TimerAt | None, Body()] = None) -> dict[str, Any]:
    body = body or TimerAt()
    return _run(db, svc.pause, body.at, client_action_id=body.client_action_id)


@router.post("/timer/resume")
def timer_resume(db: DbSession, body: Annotated[TimerAt | None, Body()] = None) -> dict[str, Any]:
    body = body or TimerAt()
    return _run(db, svc.resume, body.at, client_action_id=body.client_action_id)
