"""HTML – Screen 'Woche' (Konzept §9.2, §6.4): Mo–So grid with the entries as colored blocks, per-day
Ist/Soll, week Ist vs. Soll vs. W_max, navigation per link and swipe (``static/js/week.js``).

Routes (mounted by app.main without prefix, no login):

- ``GET /woche``                     the current ISO week
- ``GET /woche/{iso_year}-W{week}``  a given ISO week, e.g. ``/woche/2026-W44`` (invalid -> 404)

Figures come from :mod:`app.services.ledger` (via :func:`app.web.account.load_ctx`) for the period that
contains the week; days outside every period show the recorded time only (no Soll). Entries are read
directly (active, not deleted) for the block layout; blocks link to the entry edit screen.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Category, EntryStatus, Period, TimeEntry
from app.db.session import get_db
from app.util import fmt_hm, now_utc, to_local, today_local
from app.web.account import BENCHMARK_LABEL, CtxResult, benchmarks, day_label, load_ctx
from app.web.templating import WEEKDAYS_LONG, WEEKDAYS_SHORT, templates

log = logging.getLogger("app.web.week")

router = APIRouter()

WEEK_RE = re.compile(r"^(\d{4})-W(\d{1,2})$")
DEFAULT_HOURS = (7, 17)  # visible time range of the grid unless entries lie outside
EU_LIMIT_H = 48
REFERENCE_H = 40
BENCH_LABEL_GAP = 11.0  # % of the meter width between two study-value labels in one row


# =========================================================================== helpers


def week_ref(d: date) -> str:
    """ISO week reference of a date: ``2026-W44``."""
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def parse_week(ref: str) -> date | None:
    """Monday of ``YYYY-Www`` (``None`` if malformed or out of range)."""
    m = WEEK_RE.match(ref or "")
    if not m:
        return None
    try:
        return date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except ValueError:
        return None


def monday_of(d: date) -> date:
    return d - timedelta(days=d.isoweekday() - 1)


def _periods(db: Session) -> list[Period] | None:
    try:
        return list(db.scalars(select(Period).order_by(Period.start)))
    except Exception:
        log.exception("Schuljahre konnten nicht gelesen werden")
        db.rollback()
        return None


def _period_for_week(periods: list[Period], days: list[date]) -> Period | None:
    """The period holding Thursday (ISO convention), else the one holding most days of the week."""
    def holding(d: date) -> Period | None:
        return next((p for p in periods if p.start <= d <= p.end), None)

    thursday = holding(days[3])
    if thursday is not None:
        return thursday
    counts: dict[str, int] = defaultdict(int)
    by_id: dict[str, Period] = {}
    for d in days:
        p = holding(d)
        if p is not None:
            counts[p.id] += 1
            by_id[p.id] = p
    if not counts:
        return None
    return by_id[max(counts, key=lambda k: counts[k])]


COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
FALLBACK_COLOR = "#8a93a0"


def safe_color(value: str | None) -> str:
    """Category colors go into inline styles: accept hex colors only."""
    return value if value and COLOR_RE.match(value) else FALLBACK_COLOR


def _main_category(cats: dict[int, Category], cid: int | None) -> Category | None:
    cat = cats.get(cid) if cid is not None else None
    hops = 0
    while cat is not None and cat.parent_id is not None and hops < 5:
        parent = cats.get(cat.parent_id)
        if parent is None:
            break
        cat = parent
        hops += 1
    return cat


@dataclass
class Block:
    id: str
    code: str
    name: str
    color: str
    seconds: int
    start_text: str
    end_text: str
    top: float = 0.0  # % of the grid height
    height: float = 0.0
    lane: int = 0
    lanes: int = 1
    running: bool = False
    pseudo: bool = False
    note: str | None = None
    start_min: int = 0
    end_min: int = 0
    bounded: bool = True

    @property
    def left(self) -> float:
        return round(100 * self.lane / self.lanes, 3)

    @property
    def width(self) -> float:
        return round(100 / self.lanes, 3)

    @property
    def title(self) -> str:
        text = f"{self.start_text}–{self.end_text} · {self.name} · {fmt_hm(self.seconds)} h"
        if self.running:
            text += " · läuft"
        if self.note:
            text += f" · {self.note}"
        return text


@dataclass
class DayCol:
    date: date
    weekday: str
    weekday_long: str
    is_today: bool
    today: date
    in_period: bool
    label: str
    kind: str  # schultag | ferientag | feiertag | wochenende | unterrichtsfrei | ausserhalb
    absence: str | None
    soll: int | None
    ist: int
    ist_erfasst: int
    krank_gutschrift: int = 0
    blocks: list[Block] = field(default_factory=list)
    unbounded: list[Block] = field(default_factory=list)

    @property
    def anchor(self) -> str:
        return f"tag-{self.date.isoformat()}"

    @property
    def future(self) -> bool:
        return self.date > self.today

    @property
    def delta(self) -> int | None:
        """Ist − Soll; ``None`` without Soll and for days still ahead (no "−8:00" for tomorrow)."""
        if self.soll is None or self.future:
            return None
        return self.ist - self.soll


def _hm_text(dt: datetime) -> str:
    return f"{dt.hour:02d}:{dt.minute:02d}"


def _load_entries(db: Session, start: date, end: date) -> tuple[list[TimeEntry], dict[int, Category]]:
    cats = {c.id: c for c in db.scalars(select(Category))}
    entries = list(
        db.scalars(
            select(TimeEntry)
            .where(
                TimeEntry.day.between(start, end),
                TimeEntry.status == EntryStatus.ACTIVE.value,
                TimeEntry.deleted_at.is_(None),
            )
            .order_by(TimeEntry.start_at)
        )
    )
    return entries, cats


def _assign_lanes(blocks: list[Block]) -> None:
    """Side-by-side lanes for overlapping blocks (normally there are none)."""
    lane_end: list[int] = []
    for b in sorted(blocks, key=lambda x: (x.start_min, x.end_min)):
        for i, end in enumerate(lane_end):
            if end <= b.start_min:
                b.lane = i
                lane_end[i] = b.end_min
                break
        else:
            b.lane = len(lane_end)
            lane_end.append(b.end_min)
    for b in blocks:
        b.lanes = max(1, len(lane_end))


def build_week(db: Session, monday: date, res: CtxResult | None, today: date) -> dict[str, Any]:
    """View model for one ISO week."""
    days = [monday + timedelta(days=i) for i in range(7)]
    entries, cats = _load_entries(db, days[0], days[-1])
    now = now_utc()
    ctx = res.ctx if res is not None and res.ok else None
    core_days = {d.date: d for d in ctx.days} if ctx is not None else {}

    blocks_by_day: dict[date, list[Block]] = defaultdict(list)
    unbounded_by_day: dict[date, list[Block]] = defaultdict(list)
    recorded_by_day: dict[date, int] = defaultdict(int)
    per_cat: dict[int, int] = defaultdict(int)
    for e in entries:
        cat = _main_category(cats, e.category_id)
        if cat is None:
            continue
        running = e.end_at is None
        seconds = max(0, int((now - e.start_at).total_seconds())) if running else int(e.duration_seconds or 0)
        if not cat.is_pseudo:
            recorded_by_day[e.day] += seconds
            per_cat[cat.id] += seconds
        start_l = to_local(e.start_at)
        end_l = to_local(e.end_at if e.end_at is not None else now)
        block = Block(
            id=e.id,
            code=cat.code,
            name=cat.name,
            color=safe_color(cat.color),
            seconds=seconds,
            start_text=_hm_text(start_l),
            end_text=_hm_text(end_l),
            running=running,
            pseudo=bool(cat.is_pseudo),
            note=(e.note or "").strip()[:80] or None,
        )
        if not e.has_bounds:
            block.bounded = False
            unbounded_by_day[e.day].append(block)
            continue
        base = datetime.combine(e.day, datetime.min.time(), tzinfo=start_l.tzinfo)
        block.start_min = max(0, min(1440, int((start_l - base).total_seconds() // 60)))
        block.end_min = max(block.start_min, min(1440, int(-(-(end_l - base).total_seconds() // 60))))
        blocks_by_day[e.day].append(block)

    # Visible hour range.
    lo, hi = DEFAULT_HOURS
    for blocks in blocks_by_day.values():
        for b in blocks:
            lo = min(lo, b.start_min // 60)
            hi = max(hi, -(-b.end_min // 60))
    lo, hi = max(0, lo), min(24, max(hi, lo + 1))
    span = (hi - lo) * 60
    for blocks in blocks_by_day.values():
        for b in blocks:
            b.top = round(100 * (b.start_min - lo * 60) / span, 3)
            b.height = round(100 * max(b.end_min - b.start_min, 1) / span, 3)
        _assign_lanes(blocks)

    cols: list[DayCol] = []
    for d in days:
        row = ctx.row(d) if ctx is not None else None
        core = core_days.get(d)
        if row is not None and core is not None:
            kind = core.day_type.value
            label = day_label(core, ctx)
            absence = core.absence.value if core.absence else None
            soll, ist, erfasst, kg = row.soll, row.ist, row.ist_erfasst, row.krank_gutschrift
        else:
            kind = "wochenende" if d.isoweekday() >= 6 else "ausserhalb"
            label = "außerhalb des Schuljahrs" if kind == "ausserhalb" else "Wochenende"
            absence = None
            soll, ist, erfasst, kg = None, recorded_by_day.get(d, 0), recorded_by_day.get(d, 0), 0
        cols.append(
            DayCol(
                date=d,
                weekday=WEEKDAYS_SHORT[d.weekday()],
                weekday_long=WEEKDAYS_LONG[d.weekday()],
                is_today=d == today,
                today=today,
                in_period=row is not None,
                label=label,
                kind=kind,
                absence=absence,
                soll=soll,
                ist=ist,
                ist_erfasst=erfasst,
                krank_gutschrift=kg,
                blocks=sorted(blocks_by_day.get(d, []), key=lambda b: b.start_min),
                unbounded=unbounded_by_day.get(d, []),
            )
        )

    week_sum = ctx.week_of(monday) if ctx is not None else None
    ist = sum(c.ist for c in cols)
    ist_erfasst = sum(c.ist_erfasst for c in cols)
    soll_known = [c.soll for c in cols if c.soll is not None]
    soll = sum(soll_known) if soll_known else None
    is_schulwoche = bool(week_sum and week_sum.is_schulwoche)
    w_max = int(ctx.w_max) if ctx is not None else None

    total_cat = sum(per_cat.values())
    categories = sorted(
        (
            {
                "code": cats[cid].code,
                "name": cats[cid].name,
                "color": safe_color(cats[cid].color),
                "seconds": secs,
                "share": round(100 * secs / total_cat) if total_cat else 0,
                "width": round(100 * secs / total_cat, 2) if total_cat else 0,
                "is_unterricht": bool(cats[cid].is_unterricht),
            }
            for cid, secs in per_cat.items()
            if secs > 0
        ),
        key=lambda c: -c["seconds"],
    )

    return {
        "monday": monday,
        "sunday": days[-1],
        "ref": week_ref(monday),
        "kw": monday.isocalendar()[1],
        "cols": cols,
        "hours": list(range(lo, hi)),
        "hour_lo": lo,
        "hour_hi": hi,
        "ist": ist,
        "ist_erfasst": ist_erfasst,
        "krank_gutschrift": sum(c.krank_gutschrift for c in cols),
        "soll": soll,
        # Differenz only over the days up to today: a future week is not "−40 h".
        "delta": _week_delta(cols),
        "delta_partial": any(c.future and c.soll for c in cols) and any(not c.future for c in cols),
        "w_max": w_max,
        "is_schulwoche": is_schulwoche,
        "schultage": week_sum.schultage if week_sum else 0,
        "partial": any(not c.in_period for c in cols) and any(c.in_period for c in cols),
        "categories": categories,
        "meter": _meter(ist_erfasst, soll, w_max if is_schulwoche else None),
        "has_entries": bool(entries),
    }


def _week_delta(cols: list[DayCol]) -> int | None:
    days = [c for c in cols if c.soll is not None and not c.future]
    if not days:
        return None
    return sum(c.ist - c.soll for c in days)


def _meter(ist: int, soll: int | None, w_max: int | None) -> dict[str, Any]:
    """Horizontal week meter: Ist bar with ticks for Soll, W_max, 40 h, 48 h and study values."""
    bms = benchmarks()
    candidates = [ist, EU_LIMIT_H * 3600, 56 * 3600, *(b["hours_per_week"] * 3600 for b in bms)]
    if soll:
        candidates.append(soll)
    if w_max:
        candidates.append(w_max)
    scale = max(candidates) * 1.04

    def pos(seconds: float) -> float:
        return round(min(100.0, 100 * seconds / scale), 2)

    ticks = [
        {"pos": pos(REFERENCE_H * 3600), "label": f"{REFERENCE_H} h", "kind": "ref", "title": "Referenz 40h-Job"},
        {"pos": pos(EU_LIMIT_H * 3600), "label": f"{EU_LIMIT_H} h", "kind": "eu", "title": "EU-Höchstgrenze 48 h"},
    ]
    if soll:
        ticks.append({"pos": pos(soll), "label": "Soll", "kind": "soll", "title": f"Wochen-Soll {fmt_hm(soll)} h"})
    if w_max:
        ticks.append({"pos": pos(w_max), "label": "Wmax", "kind": "wmax", "title": f"W_max {fmt_hm(w_max)} h"})
    over = None
    if w_max:
        over = "wmax" if ist > w_max else None
    if ist > EU_LIMIT_H * 3600:
        over = "eu"
    bench = sorted(({**b, "pos": pos(b["hours_per_week"] * 3600)} for b in bms), key=lambda b: b["pos"])
    # Study values often lie within a few hours of each other: stagger their labels in rows so they
    # do not overlap (a label "47:58" is about 10 % of the meter wide at 375 px).
    row_end: list[float] = []
    for b in bench:
        for i, last in enumerate(row_end):
            if b["pos"] - last >= BENCH_LABEL_GAP:
                b["row"] = i
                row_end[i] = b["pos"]
                break
        else:
            b["row"] = len(row_end)
            row_end.append(b["pos"])
    return {
        "ist_pos": pos(ist),
        "ticks": ticks,
        "over": over,
        "benchmarks": bench,
        "bench_rows": len(row_end),
        "benchmark_label": BENCHMARK_LABEL,
    }


# =========================================================================== routes


def _render(request: Request, db: Session, monday: date) -> HTMLResponse:
    today = today_local()
    periods = _periods(db)
    data: dict[str, Any] = {
        "page_title": "Woche",
        "today": today,
        "state": "ok",
        "message": None,
        "prev_ref": week_ref(monday - timedelta(days=7)),
        "next_ref": week_ref(monday + timedelta(days=7)),
        "this_ref": week_ref(today),
        "is_current": monday == monday_of(today),
    }
    if periods is not None and not periods:
        data["state"] = "no_period"
        return templates.TemplateResponse(request, "week.html", data)

    days = [monday + timedelta(days=i) for i in range(7)]
    period = _period_for_week(periods or [], days)
    res = load_ctx(db, period=period, today=today) if period is not None else None
    if res is not None and not res.ok and res.state == "error":
        data["message"] = res.message
    try:
        week = build_week(db, monday, res, today)
    except Exception:
        log.exception("Woche konnte nicht aufgebaut werden")
        db.rollback()
        data.update(state="error", message="Die Woche kann gerade nicht angezeigt werden.")
        return templates.TemplateResponse(request, "week.html", data)
    data.update(
        week=week,
        period=period,
        outside=period is None,
        next_url=f"/woche/{week['ref']}",
    )
    return templates.TemplateResponse(request, "week.html", data)


@router.get("/woche", response_class=HTMLResponse)
def week_current(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _render(request, db, monday_of(today_local()))


@router.get("/woche/{ref}", response_class=HTMLResponse)
def week_page(request: Request, ref: str, db: Session = Depends(get_db)) -> HTMLResponse:
    monday = parse_week(ref)
    if monday is None:
        raise HTTPException(404, "Unbekannte Kalenderwoche – Format z. B. 2026-W44.")
    return _render(request, db, monday)
