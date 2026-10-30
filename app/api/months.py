"""JSON API – Months and reports (Konzept §10.1–§10.3). Uses services/months.py and services/reports.py.

Mounted by app.main under the ``/api`` prefix, so paths here are relative to ``/api``. Unsafe methods
need the CSRF header. There is no login (DEPLOY.md).

- ``GET  /months?period=``               – one row per month of the period (default: current)
- ``GET  /months/{ym}``                  – overview incl. open items, Mehrarbeit and close state
- ``POST /months/{ym}/close``            – ``{force?, note?}``; 409 with ``open_items`` if refused
- ``PATCH /months/{ym}/mehrarbeit``      – ``{status?, note?, ustd_vertretung_erhalten?}``
- ``GET  /months/{ym}/report``           – HTML (live; ``?stored=true`` returns the file stored at close,
  ``?download=true`` as attachment)
- ``GET  /reports/year?period=``         – HTML year report (``?entries=false`` without the entry list)

A closed month cannot be reopened; corrections go through ``/api/entries`` with a reason.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import date
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.models import MonthClose
from app.db.session import get_db
from app.services import calendar_service as cal
from app.services import months as svc
from app.services import reports

router = APIRouter(tags=["months"])

DB = Annotated[Session, Depends(get_db)]

HTML_HEADERS = {"Cache-Control": "no-store"}


def _error(exc: svc.MonthError) -> HTTPException:
    return HTTPException(exc.status_code, exc.message)


def _ym(ym: str) -> str:
    try:
        start, _ = svc.parse_ym(ym)
    except svc.MonthError as exc:
        raise _error(exc) from None
    return svc.ym_of(start)


class CloseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: bool = False
    note: str | None = Field(None, max_length=2000)


class MehrarbeitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str | None = None
    note: str | None = Field(None, max_length=2000)
    ustd_vertretung_erhalten: float | None = None


# =========================================================================== months


@router.get("/months")
def list_months(
    db: DB,
    period: Annotated[str | None, Query(description="Id, Bezeichnung (2026/27) oder 'current'")] = None,
    stichtag: Annotated[date | None, Query(description="Auswertungsdatum (Standard: heute)")] = None,
) -> dict[str, Any]:
    try:
        rows = svc.list_months(db, period, today=stichtag)
        resolved = cal.resolve_period(db, period, stichtag)
    except svc.MonthError as exc:
        raise _error(exc) from None
    except (cal.NoPeriodError, cal.NotFoundError) as exc:
        raise HTTPException(404, str(exc)) from None
    return {
        "period": {"id": resolved.id, "label": resolved.label, "start": resolved.start, "end": resolved.end},
        "months": [svc.month_row_to_dict(r) for r in rows],
    }


@router.get("/months/{ym}")
def get_month(
    ym: str,
    db: DB,
    stichtag: Annotated[date | None, Query(description="Auswertungsdatum (Standard: heute)")] = None,
) -> dict[str, Any]:
    try:
        overview = svc.month_overview(db, _ym(ym), today=stichtag)
    except svc.MonthError as exc:
        raise _error(exc) from None
    return svc.overview_to_dict(overview)


@router.post("/months/{ym}/close", response_model=None)
def close_month(ym: str, db: DB, body: CloseBody | None = None) -> JSONResponse | dict[str, Any]:
    body = body or CloseBody()
    try:
        result = svc.close_month(db, _ym(ym), force=body.force, note=body.note)
    except svc.MonthOpenItems as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content=jsonable_encoder({
                "detail": exc.message,
                "open_items": [asdict(i) for i in exc.items],
                "can_force": not any(i.blocking for i in exc.items),
            }),
        )
    except svc.MonthError as exc:
        raise _error(exc) from None
    mc = result.month_close
    return {
        "year_month": mc.year_month,
        "closed_at": mc.closed_at,
        "hash": mc.entries_hash_sha256,
        "soll": mc.soll_seconds,
        "ist": mc.ist_seconds,
        "saldo": mc.saldo_seconds,
        "report_file": mc.report_file,
        "report_url": f"/api/months/{mc.year_month}/report?stored=true",
        "forced_items": [i.message for i in result.forced_items],
        "hinweis": result.hinweis,
        "month": svc.overview_to_dict(result.overview),
    }


@router.patch("/months/{ym}/mehrarbeit")
def update_mehrarbeit(ym: str, body: MehrarbeitBody, db: DB) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if "note" in body.model_fields_set:
        kwargs["note"] = body.note
    try:
        info = svc.update_mehrarbeit(
            db, _ym(ym), status=body.status, ustd_vertretung_erhalten=body.ustd_vertretung_erhalten, **kwargs
        )
    except svc.MonthError as exc:
        raise _error(exc) from None
    return asdict(info)


# =========================================================================== reports


def content_disposition(kind: str, filename: str) -> str:
    """``Content-Disposition`` for a user-influenced file name (e.g. a period label).

    ASCII fallback restricted to ``[A-Za-z0-9._-]`` (no quote can break out of the parameter, no
    non-Latin-1 character can make the header unencodable) plus the full name as RFC 5987 ``filename*``.
    """
    fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("_") or "download"
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def _disposition(filename: str, download: bool) -> dict[str, str]:
    kind = "attachment" if download else "inline"
    return {**HTML_HEADERS, "Content-Disposition": content_disposition(kind, filename)}


@router.get("/months/{ym}/report", response_class=HTMLResponse, response_model=None)
def month_report(
    ym: str,
    db: DB,
    stored: bool = False,
    download: bool = False,
) -> HTMLResponse | FileResponse:
    ym = _ym(ym)
    if stored:
        mc = db.get(MonthClose, ym)
        path = svc.report_path(mc) if mc is not None else None
        if path is None or not path.is_file():
            raise HTTPException(404, "Für diesen Monat gibt es keinen gespeicherten Abschlussbericht.")
        return FileResponse(path, media_type="text/html; charset=utf-8",
                            headers=_disposition(f"Monatsbericht_{ym}.html", download))
    try:
        html = reports.render_month_report(db, ym)
    except svc.MonthError as exc:
        raise _error(exc) from None
    return HTMLResponse(html, headers=_disposition(f"Monatsbericht_{ym}.html", download))


@router.get("/reports/year", response_class=HTMLResponse)
def year_report(
    db: DB,
    period: Annotated[str | None, Query(description="Id, Bezeichnung (2026/27) oder 'current'")] = None,
    entries: bool = True,
    download: bool = False,
) -> HTMLResponse:
    try:
        resolved = cal.resolve_period(db, period)
        html = reports.render_year_report(db, resolved, include_entries=entries)
    except (cal.NoPeriodError, cal.NotFoundError) as exc:
        raise HTTPException(404, str(exc)) from None
    except svc.MonthError as exc:
        raise _error(exc) from None
    label = resolved.label.replace("/", "-")
    return HTMLResponse(html, headers=_disposition(f"Jahresbericht_{label}.html", download))
