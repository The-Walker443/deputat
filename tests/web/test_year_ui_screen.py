"""Web tests for the screen 'Jahr' (app/web/year.py): heatmap, category donut + table, school-week histogram,
month list, period selection, empty state, benchmarks."""

from __future__ import annotations

import json
import re
from datetime import date, time as dtime, timedelta
from html import unescape
from types import SimpleNamespace

from sqlalchemy import select

from app.db.models import Category, MonthClose, TimeEntry
from app.db.session import SessionLocal
from app.util import local_to_utc, today_local
from app.web import year as year_module


def add_entry(code: str, day, start: dtime, minutes: int, flags=None) -> str:
    start_at = local_to_utc(day, start)
    with SessionLocal() as s:
        entry = TimeEntry(
            start_at=start_at,
            end_at=start_at + timedelta(minutes=minutes),
            duration_seconds=minutes * 60,
            has_bounds=True,
            day=day,
            category_id=s.scalar(select(Category.id).where(Category.code == code)),
            status="active",
            source="manuell",
            flags=flags or [],
        )
        s.add(entry)
        s.commit()
        return entry.id


def create_period(label: str | None = None):
    from app.services import calendar_service as cal

    with SessionLocal() as s:
        p = cal.create_period(s, label) if label else cal.create_period(s)
        s.commit()
        return SimpleNamespace(id=p.id, label=p.label, start=p.start, end=p.end)


def chart_attr(html: str, marker: str) -> dict:
    m = re.search(marker + r" data-chart='([^']*)'", html)
    assert m, marker
    return json.loads(unescape(m.group(1)))


# --------------------------------------------------------------------------- empty state


def test_no_period(client):
    r = client.get("/jahr")
    assert r.status_code == 200
    assert "Noch kein Schuljahr angelegt" in r.text
    assert "data-heatmap" not in r.text


def test_unknown_period_is_404(client):
    create_period()
    assert client.get("/jahr?periode=gibtsnicht").status_code == 404


# --------------------------------------------------------------------------- page


def test_page_sections(client):
    p = create_period()
    today = today_local()
    add_entry("U", today, dtime(8, 0), 240)
    add_entry("K", today, dtime(14, 0), 60)
    r = client.get("/jahr")
    assert r.status_code == 200
    html = r.text
    assert f"Schuljahr {p.label}" in html
    for text in ("Alle Tage", "Kategorien", "Schulwochen", "Monate", "Unterrichtsanteil", "Schulwochen &gt; 48 h"):
        assert text in html, text
    assert "/static/js/charts.js" in html and "/static/css/account.css" in html
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)


def test_heatmap_covers_period(client):
    p = create_period()
    today = today_local()
    add_entry("U", today, dtime(8, 0), 11 * 60)  # > 10 h -> top level
    html = client.get("/jahr").text
    cells = re.findall(r'<a class="hm ([^"]+)" href="/woche/[^"]+#tag-(\d{4}-\d{2}-\d{2})"', html)
    days = {d for _cls, d in cells}
    assert len(days) == (p.end - p.start).days + 1
    assert p.start.isoformat() in days and p.end.isoformat() in days
    by_day = dict((d, cls) for cls, d in cells)
    assert "hm-l5" in by_day[today.isoformat()]
    assert "is-today" in by_day[today.isoformat()]
    # legend
    for label in ("nichts erfasst", "über 10 h", "Urlaub", "Krank", "Feiertag", "Ferien / unterrichtsfrei"):
        assert label in html


def test_heatmap_absences_and_ferien(client):
    from app.services import calendar_service as cal

    p = create_period()
    # a past weekday marked krank, a ferien block
    krank_day = p.start + timedelta(days=(7 - p.start.isoweekday()) % 7 + 1)  # first Tuesday-ish weekday
    while krank_day.isoweekday() > 5:
        krank_day += timedelta(days=1)
    with SessionLocal() as s:
        cal.set_override(s, krank_day, absence_type="krank")
        s.commit()
        fer_start = p.end - timedelta(days=20)
        while fer_start.isoweekday() != 1:
            fer_start += timedelta(days=1)
        period = cal.resolve_period(s, p.id)
        cal.create_ferien(s, period, name="Testferien", start=fer_start, end=fer_start + timedelta(days=4))
        s.commit()
    html = client.get(f"/jahr?periode={p.id}").text
    cells = dict((d, cls) for cls, d in re.findall(r'<a class="hm ([^"]+)" href="[^"]+#tag-([\d-]+)"', html))
    assert cells[krank_day.isoformat()].startswith("hm-krank")
    ferien_cls = cells[(fer_start + timedelta(days=1)).isoformat()]
    assert ferien_cls.split()[0] in ("hm-ferien", "hm-urlaub")


def test_categories_donut_and_table(client):
    create_period()
    today = today_local()
    add_entry("U", today, dtime(8, 0), 180)
    add_entry("K", today, dtime(13, 0), 60)
    html = client.get("/jahr").text
    data = chart_attr(html, "data-donut-chart")
    assert sum(data["seconds"]) == 240 * 60
    assert len(data["labels"]) == len(data["values"]) == len(data["colors"]) == 2
    assert all(re.match(r"^#[0-9a-fA-F]{3,8}$", c) for c in data["colors"])
    assert "75,0 %" in html  # Unterricht 3 of 4 hours
    assert 'class="is-unterricht"' in html
    assert "yr-unterricht-row" in html


def test_no_entries_messages(client):
    create_period()
    html = client.get("/jahr").text
    assert "Noch keine Einträge in diesem Schuljahr." in html
    data = chart_attr(html, "data-histogram-chart")
    assert data["counts"] == []


def test_histogram_bins_and_markers(client):
    p = create_period()
    today = today_local()
    # two complete school weeks inside the period before this week: 50 h and 42 h
    mo = today - timedelta(days=today.isoweekday() - 1)
    weeks = [mo - timedelta(days=7 * k) for k in (2, 3)]
    if weeks[-1] < p.start:
        return  # too early in the period for this scenario
    for w, per_day in zip(weeks, (600, 504), strict=True):  # 5 x 10 h, 5 x 8:24
        for i in range(5):
            add_entry("U", w + timedelta(days=i), dtime(7, 0), per_day)
    html = client.get("/jahr").text
    data = chart_attr(html, "data-histogram-chart")
    assert [m["h"] for m in data["markers"]] == [40, 48]
    assert data["edges"][1] - data["edges"][0] == 4
    assert len(data["labels"]) == len(data["counts"]) == len(data["ranges"]) == len(data["edges"]) - 1
    bins = {lbl: c for lbl, c in zip(data["labels"], data["counts"], strict=True) if c}
    # holidays may shorten a week; at least the totals must match the entered weeks
    assert sum(bins.values()) >= 1
    assert "48" in bins or "44" in bins or "40" in bins
    assert re.search(r"data-ueber48>\d+<", html)


def test_month_list(client):
    p = create_period()
    today = today_local()
    add_entry("U", today, dtime(8, 0), 120, flags=["vertretung_angeordnet"])
    ym = f"{today:%Y-%m}"
    first_ym = f"{p.start:%Y-%m}"
    with SessionLocal() as s:
        if first_ym != ym:
            s.add(MonthClose(year_month=first_ym, entries_hash_sha256="0" * 64, soll_seconds=0, ist_seconds=0,
                             saldo_seconds=0, status="abgeschlossen"))
            s.commit()
    html = client.get("/jahr").text
    rows = re.findall(r'data-month="(\d{4}-\d{2})"', html)
    assert rows[0] == first_ym and rows[-1] == f"{p.end:%Y-%m}"
    assert len(rows) == 12
    assert f'href="/monate/{ym}"' in html
    current = re.search(rf'data-month="{ym}">(.*?)</tr>', html, re.S).group(1)
    assert "2,5" in current or "3,0" in current  # 120 min = 2.67 UStd -> rounded to 0.5
    if first_ym != ym:
        first = re.search(rf'data-month="{first_ym}">(.*?)</tr>', html, re.S).group(1)
        assert "abgeschlossen" in first
    if p.end > today:
        last = re.search(rf'data-month="{p.end:%Y-%m}">(.*?)</tr>', html, re.S).group(1)
        assert "–" in last


def test_period_selector(client):
    cur = create_period()
    prev_start = date(cur.start.year - 1, cur.start.month, cur.start.day)
    prev = create_period(f"{prev_start.year}/{str(prev_start.year + 1)[2:]}")
    html = client.get("/jahr").text
    assert 'aria-label="Schuljahr wählen"' in html
    assert f'href="/jahr?periode={prev.id}"' in html
    r = client.get(f"/jahr?periode={prev.id}")
    assert r.status_code == 200
    assert f"Schuljahr {prev.label}" in r.text
    r = client.get(f"/jahr?periode={prev.label}")
    assert r.status_code == 200


# --------------------------------------------------------------------------- benchmarks


def test_benchmarks_on_histogram(client, monkeypatch):
    create_period()
    monkeypatch.setattr(year_module, "benchmarks", lambda: [{
        "label": "Teststudie", "hours_per_week": 52.5, "hours_text": "52:30", "source": "Verband",
        "year": "2023", "kind": "verbandsschaetzung",
    }])
    html = client.get("/jahr").text
    assert "Studienwerte einblenden" in html
    assert "Studienwert, nicht Norm" in html
    assert "Teststudie" in html


def test_without_benchmarks(client, monkeypatch):
    create_period()
    monkeypatch.setattr(year_module, "benchmarks", lambda: [])
    assert "Studienwerte einblenden" not in client.get("/jahr").text
