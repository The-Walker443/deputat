"""JSON API – Calendar master data: periods, ferien (CRUD + ICS import), holidays, day overrides
(absences, unterrichtsfrei), leave assignment. Uses services/calendar_service.py.

Mounted by app.main under the ``/api`` prefix, so paths here are relative to ``/api``. Unsafe
methods need the CSRF header.

Periods are addressed by id or ``current`` (the period containing today, else the latest started).
Every change rematerialises the affected ``calendar_day`` rows.

Closed months (Konzept §8.3/§10.1): a change of overrides, ferien, ICS import or leave assignment that
alters the day types/absences of a closed month needs ``reason`` (body field, ``?reason=`` for DELETE);
without it the answer is 423 and nothing changes. With a reason the month is marked "nachträglich
korrigiert" and the change is listed in its history. A period containing a closed month cannot be
deleted (409).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.types import LeaveMode
from app.db.models import CalendarDay, DayOverride, Ferien, Holiday, Period
from app.db.session import get_db
from app.services import calendar_service as cal
from app.services.settings_service import SettingsValidationError
from app.util import fmt_hm, today_local

router = APIRouter(tags=["calendar"])

DB = Annotated[Session, Depends(get_db)]
REASON_DESC = "Begründung – Pflicht, wenn ein abgeschlossener Monat betroffen ist"


@contextmanager
def service_errors() -> Iterator[None]:
    """Map service exceptions to HTTP errors with German messages (shared by account/settings)."""
    try:
        yield
    except (cal.NoPeriodError, cal.NotFoundError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None
    except (cal.CalendarLocked, cal.CalendarConflict) as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except (cal.CalendarError, SettingsValidationError) as exc:
        raise HTTPException(422, str(exc)) from None


def get_period_or_404(db: Session, ref: str | None) -> Period:
    with service_errors():
        return cal.resolve_period(db, ref)


# =========================================================================== models


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PeriodOut(_Out):
    id: str
    label: str
    start: date
    end: date
    kind: str
    tage: int
    aktuell: bool


class PeriodCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(None, description="z. B. '2026/27' (Schuljahr) oder '2027' (Kalenderjahr)")
    start: date | None = None
    end: date | None = None
    kind: str | None = None


class FerienOut(_Out):
    id: str
    period_id: str
    name: str
    kind: str
    start: date
    end: date
    source: str
    quelle_url: str | None
    tage: int
    werktage: int


class FerienCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    start: date
    end: date
    kind: str | None = None
    quelle_url: str | None = None
    reason: str | None = Field(None, description=REASON_DESC)


class FerienPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    start: date | None = None
    end: date | None = None
    kind: str | None = None
    quelle_url: str | None = None
    reason: str | None = Field(None, description=REASON_DESC)


class IcsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(..., description="Inhalt der ICS-Datei")
    replace: bool = False


class IcsCommitIn(IcsIn):
    selected: list[int] | None = None
    quelle_url: str | None = None
    reason: str | None = Field(None, description=REASON_DESC)


class IcsItemOut(BaseModel):
    index: int
    name: str
    kind: str
    start: date
    end: date
    tage: int
    werktage: int
    status: str
    hinweis: str
    importierbar: bool
    vorausgewaehlt: bool


class IcsPreviewOut(BaseModel):
    period_id: str
    items: list[IcsItemOut]
    warnings: list[str]
    importierbar: int


class IcsCommitOut(BaseModel):
    created: list[FerienOut]
    replaced: list[str]
    skipped: list[dict[str, Any]]
    warnings: list[str]


class HolidayOut(_Out):
    date: date
    name: str
    scope: str
    source: str
    weekday: int


class CalendarDayOut(BaseModel):
    date: date
    weekday: int
    day_type: str
    absence_type: str | None
    ferien_id: str | None
    holiday_name: str | None
    soll_seconds: int
    soll_hm: str
    note: str | None


class OverrideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    absence_type: str | None = None
    day_type: str | None = None
    urlaub_manuell: bool = False
    note: str | None = None
    reason: str | None = Field(None, description=REASON_DESC)


class OverrideRangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date
    end: date
    absence_type: str | None = None
    day_type: str | None = None
    note: str | None = None
    weekdays_only: bool = True
    reason: str | None = Field(None, description=REASON_DESC)


class OverrideOut(_Out):
    date: date
    absence_type: str | None
    day_type: str | None
    urlaub_manuell: bool
    note: str | None


class FerienLeaveOut(BaseModel):
    ferien_id: str
    name: str
    kind: str
    start: date
    end: date
    kandidaten: int
    urlaubstage: int


class LeavePlanOut(BaseModel):
    period_id: str
    mode: str
    mode_label: str
    requested: int
    assigned: int
    candidates: int
    shortfall: int
    noch_zuzuordnen: int
    excess: int
    invalid: list[date]
    is_complete: bool
    days: list[date]
    per_ferien: list[FerienLeaveOut]
    ausserhalb_ferien: int
    hinweise: list[str]
    info: list[str] = []


class AdoptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: LeaveMode = LeaveMode.SOMMERFERIEN_ZUERST
    reason: str | None = Field(None, description=REASON_DESC)


# =========================================================================== converters


def period_out(p: Period, today: date | None = None) -> PeriodOut:
    today = today or today_local()
    return PeriodOut(
        id=p.id,
        label=p.label,
        start=p.start,
        end=p.end,
        kind=p.kind,
        tage=(p.end - p.start).days + 1,
        aktuell=p.start <= today <= p.end,
    )


def ferien_out(f: Ferien) -> FerienOut:
    return FerienOut(
        id=f.id,
        period_id=f.period_id,
        name=f.name,
        kind=f.kind,
        start=f.start,
        end=f.end,
        source=f.source,
        quelle_url=f.quelle_url,
        tage=(f.end - f.start).days + 1,
        werktage=cal.werktage(f.start, f.end),
    )


def calendar_day_out(row: CalendarDay) -> CalendarDayOut:
    return CalendarDayOut(
        date=row.date,
        weekday=row.weekday,
        day_type=row.day_type,
        absence_type=row.absence_type,
        ferien_id=row.ferien_id,
        holiday_name=row.holiday_name,
        soll_seconds=row.soll_seconds,
        soll_hm=fmt_hm(row.soll_seconds),
        note=row.note,
    )


def holiday_out(h: Holiday) -> HolidayOut:
    return HolidayOut(date=h.date, name=h.name, scope=h.scope, source=h.source, weekday=h.date.isoweekday())


def override_out(o: DayOverride) -> OverrideOut:
    return OverrideOut.model_validate(o)


def leave_plan_out(period_id: str, info: cal.LeavePlanInfo) -> LeavePlanOut:
    return LeavePlanOut(
        period_id=period_id,
        **{k: v for k, v in info.__dict__.items() if k != "per_ferien"},
        per_ferien=[FerienLeaveOut(**fl.__dict__) for fl in info.per_ferien],
    )


# =========================================================================== periods


@router.get("/calendar/periods", response_model=list[PeriodOut])
def list_periods(db: DB) -> list[PeriodOut]:
    today = today_local()
    return [period_out(p, today) for p in cal.list_periods(db)]


@router.post("/calendar/periods", response_model=PeriodOut, status_code=status.HTTP_201_CREATED)
def create_period(body: PeriodCreate, db: DB) -> PeriodOut:
    with service_errors():
        period = cal.create_period(db, body.label, start=body.start, end=body.end, kind=body.kind)
    return period_out(period)


@router.get("/calendar/periods/suggestion")
def period_suggestion(db: DB) -> dict[str, str]:
    """Label suggestion for the create form (period containing today)."""
    return {"label": cal.suggest_period_label(db)}


@router.get("/calendar/periods/{period_ref}", response_model=PeriodOut)
def get_period(period_ref: str, db: DB) -> PeriodOut:
    return period_out(get_period_or_404(db, period_ref))


@router.delete("/calendar/periods/{period_ref}", status_code=status.HTTP_204_NO_CONTENT)
def delete_period(period_ref: str, db: DB) -> Response:
    period = get_period_or_404(db, period_ref)
    with service_errors():
        cal.delete_period(db, period.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/calendar/periods/{period_ref}/rematerialize")
def rematerialize(period_ref: str, db: DB) -> dict[str, Any]:
    period = get_period_or_404(db, period_ref)
    with service_errors():
        pdays = cal.rematerialize(db, period)
    return {"period_id": period.id, "tage": len(pdays.days), "urlaubstage": len(pdays.plan.days)}


@router.get("/calendar/periods/{period_ref}/days", response_model=list[CalendarDayOut])
def list_days(
    period_ref: str,
    db: DB,
    start: date | None = None,
    end: date | None = None,
) -> list[CalendarDayOut]:
    period = get_period_or_404(db, period_ref)
    lo = max(start or period.start, period.start)
    hi = min(end or period.end, period.end)
    return [calendar_day_out(r) for r in cal.list_calendar_days(db, lo, hi)]


@router.get("/calendar/periods/{period_ref}/holidays", response_model=list[HolidayOut])
def list_holidays(period_ref: str, db: DB) -> list[HolidayOut]:
    period = get_period_or_404(db, period_ref)
    return [holiday_out(h) for h in cal.list_holidays(db, period)]


# =========================================================================== ferien


@router.get("/calendar/periods/{period_ref}/ferien", response_model=list[FerienOut])
def list_ferien(period_ref: str, db: DB) -> list[FerienOut]:
    period = get_period_or_404(db, period_ref)
    return [ferien_out(f) for f in cal.list_ferien(db, period)]


@router.post(
    "/calendar/periods/{period_ref}/ferien", response_model=FerienOut, status_code=status.HTTP_201_CREATED
)
def create_ferien(period_ref: str, body: FerienCreate, db: DB) -> FerienOut:
    period = get_period_or_404(db, period_ref)
    with service_errors():
        row = cal.create_ferien(
            db, period, name=body.name, start=body.start, end=body.end, kind=body.kind, quelle_url=body.quelle_url,
            reason=body.reason,
        )
    return ferien_out(row)


@router.patch("/calendar/ferien/{ferien_id}", response_model=FerienOut)
def update_ferien(ferien_id: str, body: FerienPatch, db: DB) -> FerienOut:
    with service_errors():
        row = cal.update_ferien(db, ferien_id, **body.model_dump(exclude_unset=True))
    return ferien_out(row)


@router.delete("/calendar/ferien/{ferien_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ferien(ferien_id: str, db: DB, reason: str | None = None) -> Response:
    with service_errors():
        cal.delete_ferien(db, ferien_id, reason=reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/calendar/periods/{period_ref}/ics/preview", response_model=IcsPreviewOut)
def ics_preview(period_ref: str, body: IcsIn, db: DB) -> IcsPreviewOut:
    period = get_period_or_404(db, period_ref)
    with service_errors():
        preview = cal.preview_ics(db, period, body.content, replace=body.replace)
    return IcsPreviewOut(
        period_id=period.id,
        items=[IcsItemOut(**{k: v for k, v in i.__dict__.items() if k != "konflikt_ids"}) for i in preview.items],
        warnings=preview.warnings,
        importierbar=preview.importable_count,
    )


@router.post("/calendar/periods/{period_ref}/ics/commit", response_model=IcsCommitOut)
def ics_commit(period_ref: str, body: IcsCommitIn, db: DB) -> IcsCommitOut:
    period = get_period_or_404(db, period_ref)
    with service_errors():
        result = cal.commit_ics(
            db, period, body.content, selected=body.selected, replace=body.replace, quelle_url=body.quelle_url,
            reason=body.reason,
        )
    return IcsCommitOut(
        created=[ferien_out(f) for f in result.created],
        replaced=result.replaced,
        skipped=[{"index": i, "grund": reason} for i, reason in result.skipped],
        warnings=result.warnings,
    )


# =========================================================================== overrides


@router.get("/calendar/overrides", response_model=list[OverrideOut])
def list_overrides(db: DB, start: date | None = None, end: date | None = None) -> list[OverrideOut]:
    return [override_out(o) for o in cal.list_overrides(db, start, end)]


@router.post("/calendar/overrides/range", response_model=list[OverrideOut])
def set_override_range(body: OverrideRangeIn, db: DB) -> list[OverrideOut]:
    with service_errors():
        rows = cal.set_override_range(
            db,
            body.start,
            body.end,
            absence_type=body.absence_type,
            day_type=body.day_type,
            note=body.note,
            weekdays_only=body.weekdays_only,
            reason=body.reason,
        )
    return [override_out(o) for o in rows]


@router.get("/calendar/overrides/{day}", response_model=OverrideOut)
def get_override(day: date, db: DB) -> OverrideOut:
    row = cal.get_override(db, day)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Für diesen Tag gibt es keine Eingabe.")
    return override_out(row)


@router.put("/calendar/overrides/{day}", response_model=OverrideOut | None)
def set_override(day: date, body: OverrideIn, db: DB) -> OverrideOut | None:
    """Replace the day's input; all fields empty removes it (response ``null``)."""
    with service_errors():
        row = cal.set_override(db, day, **body.model_dump())
    return override_out(row) if row is not None else None


@router.delete("/calendar/overrides/{day}", status_code=status.HTTP_204_NO_CONTENT)
def delete_override(day: date, db: DB, reason: str | None = None) -> Response:
    with service_errors():
        cal.delete_override(db, day, reason=reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# =========================================================================== leave


@router.get("/calendar/periods/{period_ref}/leave-plan", response_model=LeavePlanOut)
def leave_plan(period_ref: str, db: DB, mode: Annotated[LeaveMode | None, Query()] = None) -> LeavePlanOut:
    period = get_period_or_404(db, period_ref)
    with service_errors():
        info = cal.leave_plan_info(db, period, mode)
    return leave_plan_out(period.id, info)


@router.post("/calendar/periods/{period_ref}/leave-plan/adopt", response_model=LeavePlanOut)
def adopt_leave_plan(period_ref: str, body: AdoptIn, db: DB) -> LeavePlanOut:
    """Copy an automatic plan into the manual selection (``urlaub_manuell``)."""
    period = get_period_or_404(db, period_ref)
    with service_errors():
        info = cal.adopt_leave_plan(db, period, body.mode, reason=body.reason)
    return leave_plan_out(period.id, info)
