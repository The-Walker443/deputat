"""Web tests for the screen 'Heute' (app/web/today.py): page, partials, timer state, chips, suggestions."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime, time as dtime, timedelta
from html import unescape
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import Category, TimeEntry
from app.db.session import SessionLocal
from app.util import local_to_utc, today_local

ROOT = Path(__file__).resolve().parents[2]


def cat_id(code: str) -> int:
    with SessionLocal() as s:
        return s.scalar(select(Category.id).where(Category.code == code))


def ago(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).replace(microsecond=0).isoformat()


def add_entry(code: str, day, start: dtime, minutes: int, *, status: str = "active", source: str = "manuell",
              **extra) -> str:
    start_at = local_to_utc(day, start)
    with SessionLocal() as s:
        entry = TimeEntry(
            start_at=start_at,
            end_at=start_at + timedelta(minutes=minutes),
            duration_seconds=minutes * 60,
            has_bounds=True,
            day=day,
            category_id=s.scalar(select(Category.id).where(Category.code == code)),
            status=status,
            source=source,
            flags=[],
            **extra,
        )
        s.add(entry)
        s.commit()
        return entry.id


def create_period() -> None:
    from app.services import calendar_service as cal

    with SessionLocal() as s:
        cal.create_period(s)
        s.commit()


def data_state(html: str) -> dict:
    match = re.search(r"data-state='([^']*)'", html)
    assert match, "data-state attribute missing"
    return json.loads(unescape(match.group(1)))


# --------------------------------------------------------------------------- page


def test_page_without_period_shows_hint(client):
    r = client.get("/heute")
    assert r.status_code == 200
    html = r.text
    assert "data-today" in html
    assert "Noch kein Schuljahr angelegt → Kalender" in html
    assert 'href="/kalender"' in html
    # tiles render with fallbacks
    for label in ("Ist heute", "Soll heute", "Saldo gesamt", "Woche Ist / Soll", "Nächste Ferien"):
        assert label in html
    assert "Tippen zum Starten" in html
    assert data_state(html)["running"] is None
    # timer + queue scripts are external; no inline scripts or handlers (CSP)
    assert "/static/js/timer.js" in html and "/static/js/offline-queue.js" in html
    for tag in re.findall(r"<script\b[^>]*>", html):
        assert "src=" in tag, tag
    assert not re.search(r"\son(click|load|submit|change)=", html)
    # optional start fields from settings
    assert 'data-timer-field="klasse"' in html and '<option value="3b">3b</option>' in html
    assert 'data-timer-field="fach"' in html and "Mathematik" in html
    assert 'data-timer-field="location"' in html


def test_page_with_period(client):
    create_period()
    r = client.get("/heute")
    assert r.status_code == 200
    html = r.text
    assert "Noch kein Schuljahr angelegt" not in html
    assert "Schuljahr " in html  # period label under the saldo tile
    tiles = client.get("/heute/teil/kacheln")
    assert tiles.status_code == 200
    assert "data-tiles" in tiles.text and "<html" not in tiles.text
    # soll is a number with a period (0:00 on weekends), never the fallback dash
    soll = re.search(r"Soll heute</span>\s*<span class=\"kpi-value num\">([^<]+)<", tiles.text)
    assert soll and soll.group(1).strip() != "–"


def test_ledger_failure_degrades_gracefully(client, monkeypatch):
    from app.services import ledger

    def boom(*args, **kwargs):
        raise RuntimeError("ledger not ready")

    monkeypatch.setattr(ledger, "load_context", boom)
    r = client.get("/heute")
    assert r.status_code == 200
    assert "Kennzahlen sind gerade nicht verfügbar." in r.text
    assert "Noch kein Schuljahr angelegt" not in r.text


def test_running_timer_is_rendered(client):
    r = client.post("/api/timer/start", json={"category_id": cat_id("K"), "klasse": "3b", "started_at": ago(30)})
    assert r.status_code == 200, r.text
    entry_id = r.json()["running"]["id"]

    html = client.get("/heute").text
    state = data_state(html)
    assert state["running"]["category_id"] == cat_id("K")
    assert state["running"]["code"] == "K"
    assert state["running"]["klasse"] == "3b"
    assert state["running"]["started_at"].endswith("Z")
    assert "btn-timer is-running" in html
    assert 'data-timer-pause' in html and "Tippen zum Beenden" in html
    # chip of the running category is pressed
    assert re.search(r'data-code="K"[^>]*aria-pressed="true"', html)

    entries = client.get("/heute/teil/eintraege").text
    assert f'href="/eintraege/{entry_id}"' in entries
    assert "läuft" in entries and "data-running-start" in entries
    assert "3b" in entries


def test_paused_timer_state(client):
    client.post("/api/timer/start", json={"category_id": cat_id("VN"), "started_at": ago(20)})
    assert client.post("/api/timer/pause", json={}).status_code == 200
    html = client.get("/heute").text
    state = data_state(html)
    assert state["running"] is None
    assert state["paused"]["code"] == "VN"
    assert "btn-timer is-paused" in html
    assert "Pausiert" in html


def test_chips_recently_used_first(client):
    today = today_local()
    add_entry("KO", today - timedelta(days=2), dtime(15, 0), 30)
    add_entry("E", today - timedelta(days=1), dtime(16, 0), 30)
    html = client.get("/heute").text
    codes = re.findall(r'data-chip\s+data-category-id="\d+" data-code="([^"]+)"', html)
    assert codes[:2] == ["E", "KO"]
    rest = codes[2:]
    assert rest[:3] == ["U", "VN", "K"]  # catalogue order for unused categories
    assert "KR" not in codes  # pseudo category is no timer chip
    assert len(codes) == 13


def test_entries_partial_with_flags_and_empty_state(client):
    empty = client.get("/heute/teil/eintraege")
    assert empty.status_code == 200
    assert "Heute noch keine Einträge" in empty.text

    today = today_local()
    entry_id = add_entry("VN", today, dtime(6, 0), 45, fach="Deutsch")
    with SessionLocal() as s:
        entry = s.get(TimeEntry, entry_id)
        entry.flags = ["nachtraeglich"]
        s.commit()
    html = client.get("/heute/teil/eintraege").text
    assert "06:00–06:45" in html
    assert "0:45" in html
    assert "nachträglich" in html
    assert f'href="/eintraege/{entry_id}"' in html


def test_suggestions_partial_only_with_vorschlag(client):
    empty = client.get("/heute/teil/vorschlaege")
    assert empty.status_code == 200
    assert empty.text.strip() == ""
    assert "Tag wie geplant" not in client.get("/heute").text

    today = today_local()
    add_entry("U", today, dtime(8, 0), 45, status="vorschlag", source="stundenplan", klasse="2a", fach="HSU")
    part = client.get("/heute/teil/vorschlaege").text
    assert "Tag wie geplant" in part
    assert f'data-date="{today.isoformat()}"' in part and "data-confirm-day" in part
    assert "08:00–08:45" in part and "2a" in part
    page = client.get("/heute").text
    assert "Stundenplan heute" in page
    # suggestions do not count as entries
    assert "Heute noch keine Einträge" in page


def test_shortcut_param_is_passed_to_js(client):
    html = client.get("/heute?aktion=start").text
    assert 'data-aktion="start"' in html
    assert "Kategorie antippen, um den Timer zu starten." in html


def test_pwa_files(client):
    sw = client.get("/sw.js")
    assert sw.status_code == 200
    for needle in ('"/heute"', "/static/js/timer.js", "/static/js/offline-queue.js", "skipWaiting", "clients.claim"):
        assert needle in sw.text
    manifest = client.get("/manifest.webmanifest").json()
    names = {s["name"]: s["url"] for s in manifest["shortcuts"]}
    assert names["Timer starten"] == "/heute?aktion=start"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("rel", ["app/static/js/timer.js", "app/static/js/offline-queue.js", "app/static/sw.js"])
def test_js_syntax(rel):
    result = subprocess.run(["node", "--check", str(ROOT / rel)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
