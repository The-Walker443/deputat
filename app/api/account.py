"""JSON API – Account figures (Konzept §9.1): summary, saldo series for the chart, Ferien-Guthaben-
Rechner (with planned work per ferien, persisted in ``settings.ferienarbeit_schaetzung``), the
comparison of both leave modes and statistics. Uses services/ledger.py.

Mounted by app.main under the ``/api`` prefix. All amounts are integer seconds; ``*_hm`` fields are
formatted ``h:mm`` strings for display.

Common query parameters: ``period`` (id, label like ``2026/27`` or ``current``; default current),
``leave_mode`` and ``klassenfahrt_variante`` (what-if switches, default from the settings) and
``stichtag`` (evaluation date, default today).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.calendar import DB, PeriodOut, leave_plan_out, period_out, service_errors
from app.core import account as core_account
from app.core.types import KlassenfahrtVariante, LeaveMode
from app.services import calendar_service as cal
from app.services import ledger as ledger_service
from app.services.settings_service import get_settings, set_ferienarbeit_schaetzung
from app.util import fmt_dec, fmt_hm

router = APIRouter(tags=["account"])


class Params:
    """Shared query parameters of the account endpoints."""

    def __init__(
        self,
        period: Annotated[str | None, Query(description="Id, Bezeichnung (2026/27) oder 'current'")] = None,
        leave_mode: Annotated[LeaveMode | None, Query()] = None,
        klassenfahrt_variante: Annotated[KlassenfahrtVariante | None, Query()] = None,
        stichtag: Annotated[date | None, Query(description="Auswertungsdatum (Standard: heute)")] = None,
    ):
        self.period = period
        self.leave_mode = leave_mode
        self.klassenfahrt_variante = klassenfahrt_variante
        self.stichtag = stichtag


P = Annotated[Params, Depends()]


def _context(db: Session, p: Params) -> ledger_service.LedgerContext:
    with service_errors():
        return ledger_service.load_context(
            db,
            p.period,
            today=p.stichtag,
            leave_mode=p.leave_mode,
            klassenfahrt_variante=p.klassenfahrt_variante,
        )


def hm(seconds: int, plus: bool = False) -> str:
    return fmt_hm(seconds, plus=plus)


# =========================================================================== models


class EuOut(BaseModel):
    hours: float
    hours_text: str
    ampel: str
    ist_seconds: int
    absence_days: int
    divisor_weeks: float
    window_start: date
    window_end: date


class TodayOut(BaseModel):
    date: date
    day_type: str | None
    absence: str | None
    soll: int
    ist: int
    soll_hm: str
    ist_hm: str


class WeekOut(BaseModel):
    iso_year: int
    iso_week: int
    monday: date
    soll: int
    ist: int
    ist_erfasst: int
    schultage: int
    delta: int
    soll_hm: str
    ist_hm: str
    delta_hm: str
    ueber_48: bool


class SummaryOut(BaseModel):
    period: PeriodOut
    stichtag: date
    leave_mode: str
    klassenfahrt_variante: str
    saldo_heute: int
    saldo_heute_hm: str
    saldo_tage: float
    prognose_ende: int
    prognose_ende_hm: str
    j_soll: int
    j_soll_hm: str
    soll_bisher: int
    ist_bisher: int
    ist_bisher_hm: str
    ferienarbeit_bisher: int
    ferienarbeit_bisher_hm: str
    ferienarbeit_geplant: int
    w_max: int
    w_max_hm: str
    eu_mittel: EuOut
    schulwochen_ueber_48: int
    heute: TodayOut
    woche: WeekOut | None
    naechste_ferien: dict[str, Any] | None
    urlaub: dict[str, Any]
    hinweise: list[str]


class ForecastItemOut(BaseModel):
    ferien_id: str
    name: str
    kind: str
    start: date
    end: date
    laufend: bool
    prognose_bis: int
    saldo_vor: int
    abbau_kapazitaet: int
    erwartete_ferienarbeit: int
    ferienarbeit_bisher: int
    saldo_nach: int
    geplant_h: float | None
    saldo_vor_hm: str
    abbau_kapazitaet_hm: str
    erwartete_ferienarbeit_hm: str
    saldo_nach_hm: str
    text: str


class ForecastOut(BaseModel):
    period_id: str
    stichtag: date
    saldo_heute: int
    saldo_heute_hm: str
    items: list[ForecastItemOut]


class PlannedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    planned: dict[str, float | None]


# =========================================================================== helpers


def _week_out(w: ledger_service.WeekSummary) -> WeekOut:
    return WeekOut(
        iso_year=w.iso_year,
        iso_week=w.iso_week,
        monday=w.monday,
        soll=w.soll,
        ist=w.ist,
        ist_erfasst=w.ist_erfasst,
        schultage=w.schultage,
        delta=w.delta,
        soll_hm=hm(w.soll),
        ist_hm=hm(w.ist),
        delta_hm=hm(w.delta, plus=True),
        ueber_48=w.is_schulwoche and w.ist_erfasst > 48 * 3600,
    )


def _forecast_text(f: Any) -> str:
    """Konzept §6.4 sentence for the Ferien-Guthaben-Rechner."""
    vor = hm(f.saldo_vor, plus=True)
    text = f"Du gehst voraussichtlich mit {vor} h in die {f.name}. Die Ferien bauen maximal {hm(f.abbau_kapazitaet)} h ab."
    if f.erwartete_ferienarbeit:
        text += (
            f" Wenn du {hm(f.erwartete_ferienarbeit)} h in den Ferien arbeitest, "
            f"kommst du mit {hm(f.saldo_nach, plus=True)} h zurück."
        )
    else:
        text += f" Ohne Ferienarbeit kommst du mit {hm(f.saldo_nach, plus=True)} h zurück."
    return text


def _forecast_out(ctx: ledger_service.LedgerContext, schaetzung: dict[str, float]) -> ForecastOut:
    items = []
    for f in ctx.holiday_forecast:
        items.append(
            ForecastItemOut(
                ferien_id=f.ferien_id,
                name=f.name,
                kind=f.kind.value,
                start=f.start,
                end=f.end,
                laufend=f.start <= ctx.today <= f.end,
                prognose_bis=f.prognose_bis,
                saldo_vor=f.saldo_vor,
                abbau_kapazitaet=f.abbau_kapazitaet,
                erwartete_ferienarbeit=f.erwartete_ferienarbeit,
                ferienarbeit_bisher=f.ferienarbeit_bisher,
                saldo_nach=f.saldo_nach,
                geplant_h=schaetzung.get(f.ferien_id),
                saldo_vor_hm=hm(f.saldo_vor, plus=True),
                abbau_kapazitaet_hm=hm(f.abbau_kapazitaet),
                erwartete_ferienarbeit_hm=hm(f.erwartete_ferienarbeit),
                saldo_nach_hm=hm(f.saldo_nach, plus=True),
                text=_forecast_text(f),
            )
        )
    return ForecastOut(
        period_id=ctx.period.id,
        stichtag=ctx.today,
        saldo_heute=ctx.saldo_heute,
        saldo_heute_hm=hm(ctx.saldo_heute, plus=True),
        items=items,
    )


def _leave_dict(ctx: ledger_service.LedgerContext) -> dict[str, Any]:
    info = cal.leave_plan_info_from(
        cal.PeriodDays(
            ctx.period.id, ctx.period.start, ctx.period.end, ctx.days, ctx.plan, ctx.leave_mode, ctx.ferien, ctx.holidays
        )
    )
    return leave_plan_out(ctx.period.id, info).model_dump(mode="json")


# =========================================================================== endpoints


@router.get("/account/summary", response_model=SummaryOut)
def summary(db: DB, p: P) -> SummaryOut:
    ctx = _context(db, p)
    row = ctx.row(ctx.today)
    week = ctx.week_of(ctx.today)
    upcoming = ctx.holiday_forecast[0] if ctx.holiday_forecast else None
    hints: list[str] = []
    if not ctx.ferien:
        hints.append("Für dieses Schuljahr sind noch keine Ferien erfasst (ICS-Import oder manuell).")
    shortfall = cal.leave_shortfall(ctx.plan, ctx.leave_mode)
    if shortfall:
        hints.append(f"{shortfall} Urlaubstag(e) konnten nicht zugeordnet werden.")
    if not (ctx.period.start <= ctx.today <= ctx.period.end):
        hints.append("Der Stichtag liegt außerhalb der Kontoperiode.")
    return SummaryOut(
        period=period_out(ctx.period, ctx.today),
        stichtag=ctx.today,
        leave_mode=ctx.leave_mode.value,
        klassenfahrt_variante=ctx.klassenfahrt_variante.value,
        saldo_heute=ctx.saldo_heute,
        saldo_heute_hm=hm(ctx.saldo_heute, plus=True),
        saldo_tage=round(ctx.saldo_tage, 2),
        prognose_ende=ctx.prognose_ende,
        prognose_ende_hm=hm(ctx.prognose_ende, plus=True),
        j_soll=ctx.j_soll,
        j_soll_hm=hm(ctx.j_soll),
        soll_bisher=ctx.soll_bisher,
        ist_bisher=ctx.ist_bisher,
        ist_bisher_hm=hm(ctx.ist_bisher),
        ferienarbeit_bisher=ctx.ferienarbeit_bisher,
        ferienarbeit_bisher_hm=hm(ctx.ferienarbeit_bisher),
        ferienarbeit_geplant=ctx.ferienarbeit_geplant,
        w_max=ctx.w_max,
        w_max_hm=hm(ctx.w_max),
        eu_mittel=EuOut(
            hours=round(ctx.eu.hours, 2),
            hours_text=fmt_dec(ctx.eu.hours * 3600),
            ampel=ctx.eu.ampel.value,
            ist_seconds=ctx.eu.ist_seconds,
            absence_days=ctx.eu.absence_days,
            divisor_weeks=round(ctx.eu.divisor_weeks, 2),
            window_start=ctx.eu.window_start,
            window_end=ctx.eu.window_end,
        ),
        schulwochen_ueber_48=ctx.schulwochen_ueber_48,
        heute=TodayOut(
            date=ctx.today,
            day_type=row.day_type.value if row else None,
            absence=row.absence.value if row and row.absence else None,
            soll=row.soll if row else 0,
            ist=row.ist if row else 0,
            soll_hm=hm(row.soll if row else 0),
            ist_hm=hm(row.ist if row else 0),
        ),
        woche=_week_out(week) if week else None,
        naechste_ferien=(
            {
                "ferien_id": upcoming.ferien_id,
                "name": upcoming.name,
                "start": upcoming.start.isoformat(),
                "end": upcoming.end.isoformat(),
                "abbau_kapazitaet": upcoming.abbau_kapazitaet,
                "abbau_kapazitaet_hm": hm(upcoming.abbau_kapazitaet),
                "saldo_vor": upcoming.saldo_vor,
                "saldo_vor_hm": hm(upcoming.saldo_vor, plus=True),
            }
            if upcoming
            else None
        ),
        urlaub=_leave_dict(ctx),
        hinweise=hints,
    )


@router.get("/account/series")
def series(db: DB, p: P) -> dict[str, Any]:
    """Chart data: saldo per day up to the Stichtag, forecast line, ferien bands, leave, holidays."""
    ctx = _context(db, p)
    points = [
        {
            "date": r.date.isoformat(),
            "saldo": saldo,
            "soll": r.soll,
            "ist": r.ist,
            "day_type": r.day_type.value,
            "absence": r.absence.value if r.absence else None,
            "ferien_id": r.ferien_id,
        }
        for r, (_d, saldo) in zip(ctx.ledger, ctx.series, strict=True)
        if r.date <= ctx.today
    ]
    # Forecast line from core results: today → before/after each ferien block → period end.
    prognose: list[dict[str, Any]] = []
    if ctx.today < ctx.period.end:
        start = max(ctx.today, ctx.period.start - timedelta(days=1))
        prognose.append({"date": start.isoformat(), "saldo": ctx.saldo_heute if ctx.today >= ctx.period.start else 0})
        for f in ctx.holiday_forecast:
            before = f.start - timedelta(days=1)
            if before > start:
                prognose.append({"date": before.isoformat(), "saldo": f.saldo_vor})
            if f.end <= ctx.period.end:
                prognose.append({"date": f.end.isoformat(), "saldo": f.saldo_nach})
        prognose.append({"date": ctx.period.end.isoformat(), "saldo": ctx.prognose_ende})
    return {
        "period": period_out(ctx.period, ctx.today).model_dump(mode="json"),
        "stichtag": ctx.today.isoformat(),
        "points": points,
        "prognose": prognose,
        "ferien": [
            {"id": f.id, "name": f.name, "kind": f.kind.value, "start": f.start.isoformat(), "end": f.end.isoformat()}
            for f in ctx.ferien
        ],
        "urlaub": sorted(d.isoformat() for d in ctx.plan.days),
        "feiertage": [
            {"date": d.isoformat(), "name": name, "scope": scope} for d, (name, scope) in sorted(ctx.holidays.items())
        ],
    }


@router.get("/account/holiday-forecast", response_model=ForecastOut)
def holiday_forecast(db: DB, p: P) -> ForecastOut:
    ctx = _context(db, p)
    return _forecast_out(ctx, dict(get_settings(db).ferienarbeit_schaetzung or {}))


@router.put("/account/holiday-forecast", response_model=ForecastOut)
def set_planned_work(body: PlannedIn, db: DB, p: P) -> ForecastOut:
    """Store the planned work (hours) per ferien block; ``null`` restores the default estimate."""
    with service_errors():
        period = cal.resolve_period(db, p.period, p.stichtag)
        known = {f.id for f in cal.ferien_for_period(period, cal.load_inputs(db, period.start, period.end).ferien)}
    unknown = [k for k in body.planned if k not in known]
    if unknown:
        raise HTTPException(422, "Unbekannte Ferien: " + ", ".join(unknown))
    with service_errors():
        try:
            merged = set_ferienarbeit_schaetzung(db, body.planned)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
    return _forecast_out(_context(db, p), merged)


@router.get("/account/compare-leave-modes")
def compare_leave_modes(db: DB, p: P) -> dict[str, Any]:
    """Konzept §6.2: account with leave = Sommerferien vs. proportional distribution."""
    with service_errors():
        period = cal.resolve_period(db, p.period, p.stichtag)
        variants = ledger_service.compare_leave_modes(db, period, today=p.stichtag)
    return {
        "period": period_out(period, p.stichtag).model_dump(mode="json"),
        "varianten": [
            {
                **{k: v for k, v in item.items() if k != "urlaub"},
                "saldo_heute_hm": hm(item["saldo_heute"], plus=True),
                "prognose_ende_hm": hm(item["prognose_ende"], plus=True),
                "saldo_periodenende_hm": hm(item["saldo_periodenende"], plus=True),
                "urlaub": leave_plan_out(period.id, item["urlaub"]).model_dump(mode="json"),
            }
            for item in variants
        ],
    }


@router.get("/account/statistics")
def statistics(db: DB, p: P) -> dict[str, Any]:
    """Schuljahr screen: weeks, months (with Mehrarbeit), category distribution, Unterricht share."""
    ctx = _context(db, p)
    # Months and weeks only up to the Stichtag: after it the ledger has full Soll and no Ist, which would
    # show a phantom deficit for the rest of the year (the current month/week is partial).
    until_today = [r for r in ctx.ledger if r.date <= ctx.today]
    month_rows = core_account.month_summary(until_today)
    week_rows = core_account.week_summary(until_today)
    months = []
    for m in month_rows:
        ma = ctx.mehrarbeit.get((m.year, m.month))
        months.append(
            {
                "year_month": f"{m.year:04d}-{m.month:02d}",
                "soll": m.soll,
                "ist": m.ist,
                "ist_erfasst": m.ist_erfasst,
                "delta": m.delta,
                "ferienarbeit": m.ferienarbeit,
                "saldo_end": m.saldo_end,
                "soll_hm": hm(m.soll),
                "ist_hm": hm(m.ist),
                "delta_hm": hm(m.delta, plus=True),
                "saldo_end_hm": hm(m.saldo_end, plus=True),
                "mehrarbeit": (
                    {
                        "ustd_angeordnet": ma.ustd_angeordnet,
                        "ustd_genehmigt": ma.ustd_genehmigt,
                        "total": ma.total,
                        "ausgleichsfaehig": ma.ausgleichsfaehig,
                    }
                    if ma
                    else None
                ),
            }
        )
    return {
        "period": period_out(ctx.period, ctx.today).model_dump(mode="json"),
        "stichtag": ctx.today.isoformat(),
        "weeks": [_week_out(w).model_dump(mode="json") for w in week_rows],
        "months": months,
        "categories": [
            {
                "category_id": c.category_id,
                "code": c.code,
                "name": c.name,
                "color": c.color,
                "seconds": c.seconds,
                "hm": hm(c.seconds),
                "share": round(c.share, 4),
                "is_unterricht": c.is_unterricht,
            }
            for c in ctx.categories
        ],
        "unterricht_seconds": ctx.unterricht_seconds,
        "unterricht_share": round(ctx.unterricht_share, 4),
        "schulwochen_ueber_48": ctx.schulwochen_ueber_48,
        "heatmap": [
            {"date": r.date.isoformat(), "ist_erfasst": r.ist_erfasst, "day_type": r.day_type.value,
             "absence": r.absence.value if r.absence else None}
            for r in ctx.ledger
        ],
    }
