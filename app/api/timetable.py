"""JSON API – Timetable (Stundenplan) versions and slots (Konzept §7.2). Uses services/timetable.py.

- ``GET    /timetable``                     versions (without slots) + zeitraster + id of the current version
- ``POST   /timetable``                     create a version ``{valid_from, valid_to?, label?, close_previous?, slots?}``
- ``GET    /timetable/{id|current}``        one version with slots
- ``PATCH  /timetable/{id}``                ``{label?, valid_from?, valid_to?, close_previous?}``
- ``DELETE /timetable/{id}``
- ``POST   /timetable/{id}/copy``           Halbjahreswechsel ``{valid_from, valid_to?, label?, close_source?}``
- ``GET    /timetable/{id}/slots``          slots of a version
- ``POST   /timetable/{id}/slots``          add a slot (times default to the zeitraster of ``slot_no``)
- ``PUT    /timetable/{id}/slots``          replace all slots (grid editor; matched by weekday + slot_no)
- ``PATCH  /timetable/slots/{slot_id}``     change a slot
- ``DELETE /timetable/slots/{slot_id}``
- ``GET    /timetable/for-date/{date}``     the version valid on a date (``null`` if none)

Mounted by app.main under the ``/api`` prefix. Unsafe methods need the CSRF header. Errors: ``{"detail"}``
with 404, 409 (overlapping versions/slots) or 422 (invalid input).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.days import domain_errors
from app.db.models import Settings
from app.db.session import get_db
from app.services import timetable as svc
from app.util import today_local

router = APIRouter(tags=["timetable"])

DB = Annotated[Session, Depends(get_db)]


class SlotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weekday: int
    slot_no: int
    start_time: str | None = None  # HH:MM; default from settings.zeitraster
    end_time: str | None = None
    kind: str = "unterricht"
    klasse: str | None = None
    fach: str | None = None
    category_id: int | None = None


class SlotPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weekday: int | None = None
    slot_no: int | None = None
    start_time: str | None = None
    end_time: str | None = None
    kind: str | None = None
    klasse: str | None = None
    fach: str | None = None
    category_id: int | None = None


class TimetableIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid_from: date
    valid_to: date | None = None
    label: str | None = None
    close_previous: bool = False
    slots: list[SlotIn] | None = None


class TimetablePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid_from: date | None = None
    valid_to: date | None = None
    label: str | None = None
    close_previous: bool = False


class CopyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid_from: date
    valid_to: date | None = None
    label: str | None = None
    close_source: bool = True


class SlotsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slots: list[SlotIn]


def _slot_data(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(exclude_unset=True)


@router.get("/timetable")
def list_timetables(db: DB) -> dict[str, Any]:
    today = today_local()
    current = svc.timetable_for_date(db, today)
    settings = db.get(Settings, 1)
    return {
        "timetables": [svc.timetable_to_dict(db, tt, include_slots=False, today=today)
                       for tt in svc.list_timetables(db)],
        "current_id": current.id if current else None,
        "zeitraster": list(settings.zeitraster or []) if settings else [],
        "kinds": [{"value": k, "label": v} for k, v in svc.KIND_LABELS.items()],
    }


@router.post("/timetable", status_code=status.HTTP_201_CREATED)
def create_timetable(body: TimetableIn, db: DB) -> dict[str, Any]:
    with domain_errors(db):
        tt = svc.create_timetable(
            db, valid_from=body.valid_from, valid_to=body.valid_to, label=body.label,
            slots=[_slot_data(s) for s in body.slots] if body.slots is not None else None,
            close_previous=body.close_previous,
        )
        return svc.timetable_to_dict(db, tt)


@router.get("/timetable/for-date/{day}")
def timetable_for_date(day: date, db: DB) -> dict[str, Any]:
    tt = svc.timetable_for_date(db, day)
    return {"date": day, "timetable": svc.timetable_to_dict(db, tt) if tt else None}


@router.patch("/timetable/slots/{slot_id}")
def update_slot(slot_id: str, body: SlotPatch, db: DB) -> dict[str, Any]:
    with domain_errors(db):
        slot = svc.update_slot(db, slot_id, _slot_data(body))
        return svc.slot_to_dict(db, slot)


@router.delete("/timetable/slots/{slot_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_slot(slot_id: str, db: DB) -> None:
    with domain_errors(db):
        svc.delete_slot(db, slot_id)


@router.get("/timetable/{ref}")
def get_timetable(ref: str, db: DB) -> dict[str, Any]:
    with domain_errors():
        return svc.timetable_to_dict(db, svc.resolve_timetable(db, ref))


@router.patch("/timetable/{timetable_id}")
def update_timetable(timetable_id: str, body: TimetablePatch, db: DB) -> dict[str, Any]:
    with domain_errors(db):
        tt = svc.update_timetable(db, timetable_id, body.model_dump(exclude_unset=True))
        return svc.timetable_to_dict(db, tt)


@router.delete("/timetable/{timetable_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_timetable(timetable_id: str, db: DB) -> None:
    with domain_errors(db):
        svc.delete_timetable(db, timetable_id)


@router.post("/timetable/{timetable_id}/copy", status_code=status.HTTP_201_CREATED)
def copy_timetable(timetable_id: str, body: CopyIn, db: DB) -> dict[str, Any]:
    with domain_errors(db):
        tt = svc.copy_timetable(db, timetable_id, valid_from=body.valid_from, valid_to=body.valid_to,
                                label=body.label, close_source=body.close_source)
        return svc.timetable_to_dict(db, tt)


@router.get("/timetable/{timetable_id}/slots")
def list_slots(timetable_id: str, db: DB) -> dict[str, Any]:
    with domain_errors():
        tt = svc.get_timetable(db, timetable_id)
        return {"timetable_id": tt.id, "slots": [svc.slot_to_dict(db, s) for s in tt.slots]}


@router.post("/timetable/{timetable_id}/slots", status_code=status.HTTP_201_CREATED)
def add_slot(timetable_id: str, body: SlotIn, db: DB) -> dict[str, Any]:
    with domain_errors(db):
        slot = svc.add_slot(db, timetable_id, _slot_data(body))
        return svc.slot_to_dict(db, slot)


@router.put("/timetable/{timetable_id}/slots")
def replace_slots(timetable_id: str, body: SlotsIn, db: DB) -> dict[str, Any]:
    with domain_errors(db):
        slots = svc.set_slots(db, timetable_id, [_slot_data(s) for s in body.slots])
        return {"timetable_id": timetable_id, "slots": [svc.slot_to_dict(db, s) for s in slots]}
