"""Web tests for the screen 'Woche' (app/web/week.py): grid, blocks, per-day Ist/Soll, W_max, navigation,
unbounded entries, out-of-period weeks, empty state, benchmarks."""

from __future__ import annotations

import re
from datetime import date, time as dtime, timedelta

from sqlalchemy import select

from app.db.models import Category, TimeEntry
from app.db.session import SessionLocal
from app.util import local_to_utc, today_local
from app.web import week as week_module


def add_entry(code: str, day, start: dtime, minutes: int, *, has_bounds: bool = True, status: str = "active",
              note: str | None = None) -> str:
    start_at = local_to_utc(day, start)
    with SessionLocal() as s:
        entry = TimeEntry(
            start_at=start_at,
            end_at=start_at + timedelta(minutes=minutes),
            duration_seconds=minutes * 60,
            has_bounds=has_bounds,
            day=day,
            category_id=s.scalar(select(Category.id).where(Category.code == code)),
            status=status,
            source="manuell",
            flags=[],
            note=note,
        )
        s.add(entry)
        s.commit()
        return entry.id


def create_period():
    from app.services import calendar_service as cal

    with SessionLocal() as s:
        p = cal.create_period(s)
        s.commit()
        return p.start, p.end


def monday(d: date) -> date:
    return d - timedelta(days=d.isoweekday() - 1)


def ref(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


# --------------------------------------------------------------------------- helpers


def test_parse_and_format_week_refs():
    assert week_module.parse_week("2026-W44") == date(2026, 10, 26)
    assert week_module.parse_week("2026-W1") == date(2025, 12, 29)
    assert week_module.parse_week("2026-W54") is None
    assert week_module.parse_week("2026-44") is None
    assert week_module.week_ref(date(2026, 10, 30)) == "2026-W44"
    assert week_module.week_ref(date(2027, 1, 1)) == "2026-W53"
    assert week_module.safe_color("#2563EB") == "#2563EB"
    assert week_module.safe_color("red;background:url(x)") == week_module.FALLBACK_COLOR


# --------------------------------------------------------------------------- empty / errors


def test_no_period(client):
    r = client.get("/woche")
    assert r.status_code == 200
    assert "Noch kein Schuljahr angelegt" in r.text
    assert "data-week-grid" not in r.text


def test_invalid_week_is_404(client):
    assert client.get("/woche/2026-W99").status_code == 404
    assert client.get("/woche/irgendwas").status_code == 404


# --------------------------------------------------------------------------- page


def test_current_week_grid_and_totals(client):
    create_period()
    today = today_local()
    mo = monday(today)
    eid = add_entry("U", today, dtime(8, 0), 90, note="7b Mathe")
    add_entry("U", mo, dtime(9, 0), 45)
    unb = add_entry("K", today, dtime(0, 0), 60, has_bounds=False)
    add_entry("U", today, dtime(14, 0), 30, status="vorschlag")  # suggestions do not count

    r = client.get("/woche")
    assert r.status_code == 200
    html = r.text
    assert f"KW {mo.isocalendar()[1]}" in html
    assert "data-week-grid" in html
    assert html.count('class="wk-col') == 7
    assert "/static/js/week.js" in html and "/static/css/account.css" in html
    # navigation
    assert f'data-prev="/woche/{ref(mo - timedelta(days=7))}"' in html
    assert f'data-next="/woche/{ref(mo + timedelta(days=7))}"' in html
    assert "Zur aktuellen Woche" not in html
    # today highlighted
    assert 'aria-current="date"' in html
    assert re.search(r'class="wk-col[^"]*is-today', html)
    # block positioned by time, linking to the edit screen with a way back
    assert f'href="/eintraege/{eid}?next=/woche/{ref(mo)}"' in html
    block = re.search(rf'href="/eintraege/{eid}\?[^"]*"\s+style="top: ([\d.]+)%; height: ([\d.]+)%', html)
    assert block, "block with position"
    assert float(block.group(2)) > 0
    assert "08:00–09:30" in html
    assert "7b Mathe" in html
    # unbounded entry listed below the day, not as block
    assert f'href="/eintraege/{unb}?next=' in html
    assert "ohne Uhrzeit" in html
    assert re.search(r'class="wk-unbounded"[^>]*>\+1<', html)
    # totals: 90 + 45 + 60 = 3:15 recorded (suggestion excluded)
    assert re.search(r"data-week-ist>3:15<", html)
    assert "Soll" in html and "W<sub>max</sub>" in html
    # day list with add link
    assert f'href="/eintraege/neu?datum={today.isoformat()}&amp;next=/woche/{ref(mo)}"' in html
    assert f'id="tag-{today.isoformat()}"' in html
    # category breakdown
    assert "Kategorien der Woche" in html
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)


def test_week_by_reference_and_back_link(client):
    create_period()
    today = today_local()
    other = monday(today) - timedelta(days=14)
    add_entry("U", other + timedelta(days=1), dtime(10, 0), 60)
    r = client.get(f"/woche/{ref(other)}")
    assert r.status_code == 200
    assert f"KW {other.isocalendar()[1]}" in r.text
    assert "Zur aktuellen Woche" in r.text
    assert 'aria-current="date"' not in r.text
    assert "10:00–11:00" in r.text


def test_day_labels_and_soll(client):
    start, end = create_period()
    # a Saturday inside the period: labelled weekend, soll 0:00
    sat = monday(today_local()) + timedelta(days=5)
    html = client.get("/woche").text
    if start <= sat <= end:
        m = re.search(rf'id="tag-{sat.isoformat()}".*?wk-badge[^>]*>([^<]+)<', html, re.S)
        assert m and "Wochenende" in m.group(1)


def test_holiday_label(client):
    from app.services import calendar_service as cal

    start, end = create_period()
    with SessionLocal() as s:
        holidays = [
            h for h in cal.list_holidays(s, cal.resolve_period(s))
            if h.scope == "gesetzlich" and h.date.isoweekday() <= 5
        ]
    assert holidays
    h = holidays[0]
    html = client.get(f"/woche/{ref(h.date)}").text
    assert h.name in html
    assert "wk-kind-feiertag" in html


def test_week_outside_any_period(client):
    start, _end = create_period()
    far = monday(start) - timedelta(days=21)
    add_entry("U", far + timedelta(days=2), dtime(8, 0), 120)
    r = client.get(f"/woche/{ref(far)}")
    assert r.status_code == 200
    assert "data-outside" in r.text
    assert "keinem angelegten" in r.text
    assert re.search(r"data-week-ist>2:00<", r.text)


def test_empty_week_hint(client):
    create_period()
    r = client.get("/woche")
    assert "In dieser Woche ist noch nichts erfasst." in r.text


def test_hour_range_extends_for_late_entries(client):
    create_period()
    add_entry("K", today_local(), dtime(21, 0), 90)
    html = client.get("/woche").text
    assert re.search(r"7–23 Uhr", html)


# --------------------------------------------------------------------------- benchmarks


def test_benchmarks_toggle_when_available(client, monkeypatch):
    create_period()
    monkeypatch.setattr(week_module, "benchmarks", lambda: [{
        "label": "Teststudie", "hours_per_week": 47.97, "hours_text": "47:58", "source": "Quelle X",
        "year": "2016", "kind": "studie",
    }])
    html = client.get("/woche").text
    assert "Studienwerte einblenden" in html
    assert "Studienwert, nicht Norm" in html
    assert "Teststudie" in html and "wk-tick-bench" in html


def test_no_benchmarks_without_module(client, monkeypatch):
    create_period()
    monkeypatch.setattr(week_module, "benchmarks", lambda: [])
    html = client.get("/woche").text
    assert "Studienwerte einblenden" not in html
    assert "wk-tick-bench" not in html


def test_future_week_has_no_negative_difference(client):
    start, end = create_period()
    nxt = monday(today_local()) + timedelta(days=14)
    if nxt + timedelta(days=6) > end:
        return
    html = client.get(f"/woche/{ref(nxt)}").text
    assert "Differenz zum Soll" not in html  # a week ahead is not "−40:00"
    assert not re.search(r'class="text-xs is-negative"> −\d+:\d\d<', html)


def test_benchmark_labels_are_staggered():
    bms = [
        {"label": n, "hours_per_week": h, "hours_text": t, "source": "", "year": "", "kind": ""}
        for n, h, t in (("A", 47.97, "47:58"), ("B", 49.75, "49:45"), ("C", 52.5, "52:30"), ("D", 70.0, "70:00"))
    ]
    orig = week_module.benchmarks
    week_module.benchmarks = lambda: bms
    try:
        meter = week_module._meter(50 * 3600, 40 * 3600, 47 * 3600)
    finally:
        week_module.benchmarks = orig
    rows = [b["row"] for b in meter["benchmarks"]]
    assert rows[:3] == [0, 1, 2]  # close together -> one row each
    assert rows[3] == 0  # far away -> back to the first row
    assert meter["bench_rows"] == 3
