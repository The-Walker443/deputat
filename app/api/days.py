"""JSON API – Day view and day confirmation (Konzept §7.2, §16). Uses services/timetable.py (which uses
services/ledger.py for soll/ist and services/entries.py for every write).

- ``GET  /days/open?until=``          To-do list: unconfirmed school days / school days without entries
- ``GET  /days/{date}``               day type, soll, ist, entries, suggestions, slots, conflicts, confirmation
- ``POST /days/{date}/suggestions``   create the timetable suggestions of the day (idempotent)
- ``POST /days/{date}/confirm``       confirm the suggestions with deviations (``Tag wie geplant`` = empty body)

Mounted by app.main under the ``/api`` prefix, so paths here are relative to ``/api``. Unsafe methods need
the CSRF header. Errors: ``{"detail": "..."}`` with 404 (no period), 409 (a slot collides with an existing
entry), 422 (invalid input, not a school day, future day) or 423 (closed month without ``reason``).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import calendar_service as cal
from app.services import entries as entries_svc
from app.services import timetable as svc

router = APIRouter(tags=["days"])

DB = Annotated[Session, Depends(get_db)]


@contextmanager
def domain_errors(db: Session | None = None) -> Iterator[None]:
    """Map service exceptions to HTTP errors with German messages (shared with the timetable router)."""
    try:
        yield
    except entries_svc.DomainError as exc:
        if db is not None:
            db.rollback()
        raise HTTPException(exc.status_code, exc.message) from None
    except (cal.NoPeriodError, cal.NotFoundError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    except cal.CalendarError as exc:
        raise HTTPException(422, str(exc)) from None


class DeviationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_id: str | None = None
    entry_id: str | None = None
    type: str
    klasse: str | None = None
    fach: str | None = None
    art: str | None = None  # vertretung: 'angeordnet' (default) | 'genehmigt'
    start: str | None = None  # HH:MM (local)
    end: str | None = None
    note: str | None = None
    category_id: int | None = None  # exkursion: category instead of SV


class ExtraVertretungIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str
    end: str
    klasse: str
    fach: str | None = None
    art: str = "angeordnet"
    note: str | None = None


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deviations: list[DeviationIn] = []
    extra: list[ExtraVertretungIn] = []
    reason: str | None = None


@router.get("/days/open")
def open_days(db: DB, until: date | None = None, period: str | None = None) -> dict[str, Any]:
    with domain_errors():
        return svc.open_days(db, until, period=period).to_dict()


@router.get("/days/{day}")
def get_day(day: date, db: DB) -> dict[str, Any]:
    with domain_errors():
        return svc.day_view(db, day)


@router.post("/days/{day}/suggestions")
def generate_suggestions(day: date, db: DB) -> dict[str, Any]:
    with domain_errors(db):
        return svc.generate_suggestions(db, day).to_dict()


@router.post("/days/{day}/confirm")
def confirm_day(day: date, db: DB, body: ConfirmIn | None = None) -> dict[str, Any]:
    body = body or ConfirmIn()
    deviations = [d.model_dump(exclude_none=True) for d in body.deviations]
    extra = [x.model_dump(exclude_none=True) for x in body.extra]
    with domain_errors(db):
        result = svc.confirm_day(db, day, deviations, extra=extra, reason=body.reason)
        return {"result": result.to_dict(), "day": svc.day_view(db, day)}
