"""JSON API – quick entry of day totals (screen "Erfassen", :mod:`app.services.quick_entry`).

- ``GET /weeks/{ref}``         one ISO week (``2026-W45``): per day ist (recorded total), soll, day type,
                               absence, badges, locked, has_detail_entries, tagessumme_seconds; week sums
- ``PUT /days/{date}/total``   ``{"seconds": 27000}`` or ``{"value": "7,5" | "7,30" | "7:30" | ""}``, optional
                               ``at`` (device time of the input, ISO with offset), ``client_action_id`` and
                               ``reason`` (closed month). Sets the day's total; 0 / empty removes the
                               Tagessumme entry. Idempotent, last write wins by input time, so the offline
                               queue may resend it. Answers with the week (``days``) plus ``day`` and
                               ``action`` (angelegt | geaendert | geloescht | unveraendert | bestaetigt |
                               veraltet).

Mounted by app.main under the ``/api`` prefix. PUT needs the CSRF header. Errors ``{"detail": ...}``:
404 (unknown week), 409 (total below the entries recorded individually; ``detail_seconds`` /
``min_seconds`` in the body),
422 (invalid input, e.g. "abc", "25", negative) and 423 (closed month without ``reason``).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import entries as entries_svc
from app.services import quick_entry as svc

router = APIRouter(tags=["erfassen"])

DB = Annotated[Session, Depends(get_db)]


class DayTotalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seconds: int | None = Field(default=None, ge=0, le=svc.MAX_ANY_DAY)
    value: str | None = Field(default=None, max_length=32)
    at: datetime | None = None  # device time of the input (offline queue)
    client_action_id: str | None = Field(default=None, max_length=64)
    reason: str | None = Field(default=None, max_length=500)


@router.get("/weeks/{ref}")
def get_week(ref: str, db: DB) -> dict[str, Any]:
    monday = svc.parse_week_ref(ref)
    if monday is None:
        raise HTTPException(404, "Unbekannte Kalenderwoche – Format z. B. 2026-W45.")
    return svc.week_view(db, monday)


@router.put("/days/{day}/total", response_model=None)
def put_day_total(day: date, body: DayTotalIn, db: DB) -> dict[str, Any] | JSONResponse:
    if body.seconds is not None and body.value is not None:
        raise HTTPException(422, "Bitte entweder „seconds“ oder „value“ angeben, nicht beides.")
    if body.seconds is None and body.value is None:
        raise HTTPException(422, "„seconds“ oder „value“ fehlt.")
    try:
        entries_svc._check_year(day.year)
        max_seconds = svc.day_length(day)
        seconds = body.seconds if body.seconds is not None else svc.parse_total(body.value, max_seconds=max_seconds)
        return svc.set_day_total(db, day, seconds, client_action_id=body.client_action_id, reason=body.reason,
                                 at=body.at)
    except svc.DayTotalBelowDetail as exc:
        db.rollback()
        return JSONResponse(
            {"detail": exc.message, "detail_seconds": exc.detail_seconds, "min_seconds": exc.min_seconds,
             "entries_url": f"/eintraege?von={day.isoformat()}&bis={day.isoformat()}"},
            status_code=exc.status_code,
        )
    except entries_svc.EntryValidationError as exc:
        db.rollback()
        raise HTTPException(422, exc.message) from None
    except entries_svc.DomainError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, exc.message) from None
