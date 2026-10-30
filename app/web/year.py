"""HTML – Screen 'Jahr' / Schuljahr (Konzept §9.3, §6.5, §6.6): heatmap of all days, category donut +
table (Unterrichtsanteil), histogram of the school-week Ist with markers at 40 h / 48 h, month list.

Routes (mounted by app.main without prefix, no login):

- ``GET /jahr``                   the current period (``?periode=<id or label>`` = another period)

All figures come from :mod:`app.services.ledger` (one :func:`load_context` per request, via
:func:`app.web.account.load_ctx`); the month status is read from ``month_close`` (read-only).
Charts are drawn by ``static/js/charts.js`` from ``data-chart`` JSON attributes; the heatmap is
server-rendered (CSS grid).
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MonthClose, Period, Settings
from app.db.session import get_db
from app.util import fmt_dec, fmt_hm, today_local
from app.web.account import BENCHMARK_LABEL, benchmarks, day_label, load_ctx
from app.web.templating import MONTHS, WEEKDAYS_SHORT, templates
from app.web.week import safe_color, week_ref

log = logging.getLogger("app.web.year")

router = APIRouter()

# Heatmap levels: upper bounds in hours of recorded work per day (level 1..5; 0 = nothing recorded).
HEAT_LEVELS = (3, 6, 8, 10)
HEAT_LEGEND = ("bis 3 h", "3–6 h", "6–8 h", "8–10 h", "über 10 h")
HIST_BIN_H = 4
HIST_MARKERS = (40, 48)
STATUS_LABELS = {
    "offen": "offen",
    "abgeschlossen": "abgeschlossen",
    "nachtraeglich_korrigiert": "nachträglich korrigiert",
}
STATUS_BADGES = {"abgeschlossen": "badge-success", "nachtraeglich_korrigiert": "badge-warn"}


def _level(seconds: int) -> int:
    if seconds <= 0:
        return 0
    h = seconds / 3600
    for i, bound in enumerate(HEAT_LEVELS, start=1):
        if h <= bound:
            return i
    return len(HEAT_LEVELS) + 1


def _monday(d: date) -> date:
    return d - timedelta(days=d.isoweekday() - 1)


# =========================================================================== heatmap


def heatmap(ctx: Any, today: date) -> dict[str, Any]:
    """Week columns (Mo–So rows) covering the whole period; one cell per day."""
    start, end = ctx.period.start, ctx.period.end
    core_days = {d.date: d for d in ctx.days}
    first = _monday(start)
    last = end + timedelta(days=7 - end.isoweekday())
    weeks: list[dict[str, Any]] = []
    d = first
    seen_months: set[tuple[int, int]] = set()
    while d <= last:
        cells = []
        month_label = ""
        # The month label sits above the first week whose Monday lies in that month (the first column shows
        # the period's start month): a week like 26.10.–01.11. stays "Okt", not "Nov" (audit).
        anchor = max(d, start)
        if anchor <= end and (anchor.year, anchor.month) not in seen_months and (anchor.day <= 7 or anchor == start):
            seen_months.add((anchor.year, anchor.month))
            month_label = MONTHS[anchor.month - 1][:3]
        for i in range(7):
            day = d + timedelta(days=i)
            cells.append(_cell(ctx, core_days.get(day), day, today, start, end))
        weeks.append({"ref": week_ref(d), "month": month_label, "cells": cells})
        d += timedelta(days=7)
    return {
        "weeks": weeks,
        "weekday_labels": [w if i in (0, 2, 4) else "" for i, w in enumerate(WEEKDAYS_SHORT)],
        "legend_levels": [{"cls": f"hm-l{i}", "label": label} for i, label in enumerate(HEAT_LEGEND, start=1)],
        "today_ref": week_ref(today) if start <= today <= end else None,
    }


def _cell(ctx: Any, core: Any, day: date, today: date, start: date, end: date) -> dict[str, Any]:
    if core is None or not (start <= day <= end):
        return {"cls": "hm-out", "title": "", "date": day}
    row = ctx.row(day)
    ist = int(row.ist_erfasst) if row is not None else 0
    soll = int(row.soll) if row is not None else 0
    absence = core.absence.value if core.absence else None
    kind = core.day_type.value
    level = _level(ist)
    if absence == "krank":
        cls = "hm-krank"
    elif absence == "urlaub":
        cls = "hm-urlaub"
    elif level:
        cls = f"hm-l{level}"
    elif kind == "feiertag" or core.is_holiday:
        cls = "hm-feiertag"
    elif kind in ("ferientag", "unterrichtsfrei"):
        cls = "hm-ferien"
    elif kind == "wochenende":
        cls = "hm-we"
    else:
        cls = "hm-l0"
    extra = []
    if level and cls in ("hm-krank", "hm-urlaub"):
        extra.append("has-ist")
    if level and kind in ("ferientag", "feiertag", "unterrichtsfrei") and cls.startswith("hm-l"):
        extra.append("is-frei")
    if day == today:
        extra.append("is-today")
    elif day > today:
        extra.append("is-future")
    title = f"{WEEKDAYS_SHORT[day.weekday()]} {day:%d.%m.%Y} · {day_label(core, ctx)} · Ist {fmt_hm(ist)} h"
    if soll:
        title += f" / Soll {fmt_hm(soll)} h"
    return {"cls": " ".join([cls, *extra]), "title": title, "date": day}


# =========================================================================== categories


def categories(ctx: Any) -> dict[str, Any]:
    rows = [
        {
            "code": c.code,
            "name": c.name,
            "color": safe_color(c.color),
            "seconds": int(c.seconds),
            "share": c.share,
            "share_text": f"{100 * c.share:.1f}".replace(".", ",") + " %",
            "is_unterricht": c.is_unterricht,
        }
        for c in ctx.categories
        if c.seconds > 0
    ]
    chart = {
        "labels": [r["name"] for r in rows],
        "values": [round(r["seconds"] / 3600, 3) for r in rows],
        "colors": [r["color"] for r in rows],
        "seconds": [r["seconds"] for r in rows],
    }
    total = sum(r["seconds"] for r in rows)
    return {
        "rows": rows,
        "chart": chart,
        "total": total,
        "unterricht": int(ctx.unterricht_seconds),
        "unterricht_share_text": f"{100 * ctx.unterricht_share:.1f}".replace(".", ",") + " %",
        "unterricht_pct": round(100 * ctx.unterricht_share),
    }


def teaching(ctx: Any, settings: Any) -> dict[str, Any]:
    """Konzept §6.1: Altersermäßigung and Anrechnungsstunden are shown in the teaching statistics – they lower
    the expected teaching load (UPZ tatsächlich), not the Soll. Expected = UPZ tatsächlich × school weeks so far
    (school days without full-day absence / 5); recorded = Unterricht in UStd."""
    as_of = min(ctx.today, ctx.period.end)
    school_days = sum(
        1 for r in ctx.ledger
        if r.date <= as_of and r.day_type.value == "schultag" and r.absence is None and not r.vor_erfassung
    )
    weeks = school_days / 5
    minutes = int(settings.unterrichtsstunde_min or 45)
    upz = float(settings.upz_ist or 0)
    expected_ustd = upz * weeks
    recorded_ustd = int(ctx.unterricht_seconds) / (minutes * 60) if minutes else 0.0
    fs = next((c.seconds for c in ctx.categories if c.code == "FS"), 0)

    def num(x: float) -> str:
        return f"{x:.1f}".replace(".", ",").removesuffix(",0")

    return {
        "weeks_text": num(weeks),
        "upz_text": num(upz),
        "expected_text": num(expected_ustd),
        "recorded_text": num(recorded_ustd),
        "minutes": minutes,
        "ae_text": num(float(settings.altersermaessigung or 0)),
        "anrechnung_text": num(float(settings.anrechnungsstunden or 0)),
        "anrechnung": float(settings.anrechnungsstunden or 0),
        "anrechnung_grund": settings.anrechnungs_grund or "",
        "fs_seconds": int(fs),
        "fs_expected_text": num(float(settings.anrechnungsstunden or 0) * weeks),
    }


# =========================================================================== histogram


def histogram(ctx: Any, today: date) -> dict[str, Any]:
    """School weeks that are over (Sunday before today) and have recorded time, in 4-hour bins.

    Weeks without any entry (e.g. before recording started) are left out: they are missing data,
    not zero-hour weeks.
    """
    weeks = [
        w for w in ctx.weeks
        if w.is_schulwoche and w.ist_erfasst > 0 and w.monday + timedelta(days=6) < today
    ]
    hours = [w.ist_erfasst / 3600 for w in weeks]
    bms = benchmarks()
    # The ledger counter also counts the running week once it is above 48 h; the histogram only
    # shows finished weeks, so say how many of the counted weeks are not (yet) in the chart.
    running = [
        w for w in ctx.weeks
        if w.is_schulwoche and w.monday <= today <= w.monday + timedelta(days=6)
        and w.ist_erfasst > HIST_MARKERS[-1] * 3600
    ]
    result: dict[str, Any] = {
        "weeks": len(weeks),
        "ueber_48": int(ctx.schulwochen_ueber_48),
        "ueber_48_laufend": len(running),
        "ueber_40": sum(1 for h in hours if h > 40),
        "mean": fmt_hm(sum(w.ist_erfasst for w in weeks) / len(weeks)) if weeks else None,
        "max": fmt_hm(max(w.ist_erfasst for w in weeks)) if weeks else None,
        "benchmarks": bms,
        "benchmark_label": BENCHMARK_LABEL,
    }
    if not weeks:
        result["chart"] = {"labels": [], "counts": [], "edges": [], "markers": [], "benchmarks": []}
        return result
    lo = min(32, int(math.floor(min(hours) / HIST_BIN_H)) * HIST_BIN_H)
    hi = max(56, int(math.floor(max(hours) / HIST_BIN_H)) * HIST_BIN_H + HIST_BIN_H)
    for b in bms:
        hi = max(hi, int(math.floor(b["hours_per_week"] / HIST_BIN_H)) * HIST_BIN_H + HIST_BIN_H)
    edges = list(range(lo, hi + 1, HIST_BIN_H))
    counts = [0] * (len(edges) - 1)
    for h in hours:
        counts[min(len(counts) - 1, int((h - lo) // HIST_BIN_H))] += 1
    result["chart"] = {
        "labels": [str(a) for a in edges[:-1]],
        "ranges": [f"{a}–{b} h" for a, b in zip(edges, edges[1:], strict=False)],
        "counts": counts,
        "edges": edges,
        "markers": [{"h": m, "label": f"{m} h"} for m in HIST_MARKERS],
        "benchmarks": [
            {"h": b["hours_per_week"], "short": b["hours_text"], "label": b["label"]} for b in bms
        ],
    }
    return result


# =========================================================================== months


def _month_closes(db: Session, start: date, end: date) -> dict[str, MonthClose]:
    try:
        return {
            mc.year_month: mc
            for mc in db.scalars(
                select(MonthClose).where(
                    MonthClose.year_month.between(f"{start:%Y-%m}", f"{end:%Y-%m}")
                )
            )
        }
    except Exception:
        log.exception("Monatsabschlüsse konnten nicht gelesen werden")
        db.rollback()
        return {}


def month_rows(db: Session, ctx: Any, today: date) -> list[dict[str, Any]]:
    closes = _month_closes(db, ctx.period.start, ctx.period.end)
    by_month: dict[tuple[int, int], list[Any]] = defaultdict(list)
    for r in ctx.ledger:
        by_month[(r.date.year, r.date.month)].append(r)
    result = []
    for (year, month), rows in sorted(by_month.items()):
        ym = f"{year:04d}-{month:02d}"
        past = [r for r in rows if r.date <= today]
        future = not past
        soll = sum(r.soll for r in past)
        ist = sum(r.ist for r in past)
        mm = ctx.mehrarbeit.get((year, month))
        mc = closes.get(ym)
        status = mc.status if mc is not None else "offen"
        laufend = bool(past) and len(past) < len(rows)
        result.append(
            {
                "ym": ym,
                "label": f"{MONTHS[month - 1]} {year}",
                "short": f"{MONTHS[month - 1][:3]} {year % 100:02d}",
                "soll": soll,
                "ist": ist,
                "saldo": ist - soll,
                "ustd": mm.total if mm else 0.0,
                "ustd_text": fmt_dec(mm.total * 3600, places=1) if mm and mm.total else "–",
                "ausgleichsfaehig": bool(mm and mm.ausgleichsfaehig),
                "status": status,
                "status_label": "läuft" if laufend and mc is None else STATUS_LABELS.get(status, status),
                "status_badge": STATUS_BADGES.get(status, "badge-primary" if laufend else "badge-outline"),
                "future": future,
                "laufend": laufend,
            }
        )
    return result


# =========================================================================== page


def _kpis(ctx: Any, hist: dict[str, Any]) -> dict[str, Any]:
    return {
        "saldo": ctx.saldo_heute,
        "ist_bisher": ctx.ist_bisher,
        "soll_bisher": ctx.soll_bisher,
        "j_soll": ctx.j_soll,
        "prognose": ctx.prognose_ende,
        "ferienarbeit": ctx.ferienarbeit_bisher,
        "unterricht_share_text": f"{100 * ctx.unterricht_share:.0f} %",
        "ueber_48": int(ctx.schulwochen_ueber_48),
        "mean_week": hist.get("mean"),
    }


def _periods(db: Session) -> list[Period]:
    try:
        return list(db.scalars(select(Period).order_by(Period.start)))
    except Exception:
        log.exception("Schuljahre konnten nicht gelesen werden")
        db.rollback()
        return []


@router.get("/jahr", response_class=HTMLResponse)
def year_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    today = today_local()
    ref = (request.query_params.get("periode") or "").strip() or None
    periods = _periods(db)
    period = None
    if ref:
        period = next((p for p in periods if p.id == ref or p.label == ref), None)
        if period is None:
            raise HTTPException(404, f"Schuljahr „{ref}“ nicht gefunden.")
    res = load_ctx(db, period=period, today=today)
    data: dict[str, Any] = {"page_title": "Jahr", "res": res, "today": today}
    if not res.ok:
        return templates.TemplateResponse(request, "year.html", data)
    ctx = res.ctx
    try:
        hist = histogram(ctx, today)
        data.update(
            ctx=ctx,
            periods=[
                {"id": p.id, "label": p.label, "active": p.id == ctx.period.id,
                 "href": "/jahr" if p.id == _current_id(periods, today) else f"/jahr?periode={p.id}"}
                for p in periods
            ],
            heat=heatmap(ctx, today),
            cats=categories(ctx),
            teach=teaching(ctx, db.get(Settings, 1)),
            hist=hist,
            months=month_rows(db, ctx, today),
            kpis=_kpis(ctx, hist),
            as_of=ctx.as_of,
        )
    except Exception:
        log.exception("Jahresübersicht konnte nicht aufgebaut werden")
        db.rollback()
        data["res"] = type(res)(state="error", message="Die Jahresübersicht ist gerade nicht verfügbar.")
    return templates.TemplateResponse(request, "year.html", data)


def _current_id(periods: list[Period], today: date) -> str | None:
    cur = next((p for p in periods if p.start <= today <= p.end), None)
    if cur is None:
        started = [p for p in periods if p.start <= today]
        cur = started[-1] if started else (periods[0] if periods else None)
    return cur.id if cur else None
