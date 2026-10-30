"""Web tests for 'Monate & Berichte' (app/web/months.py): list, detail, Mehrarbeit, close dialog/page, reports."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import Path

import pytest

EXAMPLE_ICS = Path(__file__).resolve().parents[2] / "examples" / "ferien_bayern_BEISPIEL.ics"
SEPT_SCHOOL_DAYS = [date(2026, 9, d) for d in (15, 16, 17, 18, 21, 22, 23, 24, 25, 28, 29, 30)]


def _cat(code: str) -> int:
    from sqlalchemy import select

    from app.db.models import Category
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        return db.scalar(select(Category.id).where(Category.code == code))


def _entry(client, day: date, hour: int, minutes: int, code: str = "VN", **extra) -> dict:
    start = datetime(day.year, day.month, day.day, hour, 0)
    end = start + timedelta(minutes=minutes)
    r = client.post("/api/entries", json={"start_at": start.isoformat() + "+02:00", "end_at": end.isoformat() + "+02:00",
                                          "category_id": _cat(code), **extra})
    assert r.status_code == 201, r.text
    return r.json()["entry"]


@pytest.fixture
def year(client):
    r = client.post("/api/calendar/periods", json={"label": "2026/27"})
    assert r.status_code == 201, r.text
    period = r.json()
    r = client.post(f"/api/calendar/periods/{period['id']}/ics/commit",
                    json={"content": EXAMPLE_ICS.read_text(encoding="utf-8")})
    assert r.status_code == 200, r.text
    return period


@pytest.fixture
def september(client, year):
    for d in SEPT_SCHOOL_DAYS:
        _entry(client, d, 8, 8 * 60)
    return SEPT_SCHOOL_DAYS


def text_of(html: str) -> str:
    return unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)))


# --------------------------------------------------------------------------- month list


def test_months_list_without_period(client):
    r = client.get("/monate")
    assert r.status_code == 200
    assert "Keine Monate" in r.text and 'href="/kalender"' in r.text


def test_months_list_rows_chart_and_links(client, september):
    r = client.get("/monate")
    assert r.status_code == 200
    assert "/static/css/months.css" in r.text and "/static/js/months.js" in r.text
    assert "chart.umd.min.js" in r.text
    t = text_of(r.text)
    assert "September 2026" in t and "96:00" in t
    assert 'href="/monate/2026-09"' in r.text
    assert 'href="/berichte/jahr?periode=' in r.text and 'href="/export"' in r.text and 'href="/import"' in r.text
    data = json.loads(unescape(re.search(r"data-chart='([^']*)'", r.text).group(1)))
    sept = next(m for m in data if m["label"] == "Sep 26")
    assert sept["ist"] == 96.0
    # September is over and not closed -> hint banner
    assert "Jetzt prüfen und abschließen" in r.text


def test_months_list_unknown_period_404(client, year):
    assert client.get("/monate", params={"periode": "1999/00"}).status_code == 404


# --------------------------------------------------------------------------- detail


def test_month_detail_figures_and_close_dialog(client, september):
    r = client.get("/monate/2026-09")
    assert r.status_code == 200
    t = text_of(r.text)
    assert "Saldo Monat" in t and "Keine offenen Punkte" in t
    assert 'data-dialog-open="close-dialog"' in r.text and '<dialog class="months-dialog" id="close-dialog"' in r.text
    assert "nicht wieder geöffnet" in t and "gesperrt" in t
    assert 'id="mehrarbeit"' in r.text and "Bagatellgrenze" in t
    assert 'href="/monate/2026-08"' in r.text and 'href="/monate/2026-10"' in r.text


def test_month_detail_open_items_have_fix_links(client, year):
    for d in SEPT_SCHOOL_DAYS[1:]:
        _entry(client, d, 8, 60)
    r = client.get("/monate/2026-09")
    assert r.status_code == 200
    assert "/eintraege/neu?datum=2026-09-15" in r.text
    assert "Nachtragen" in r.text
    assert "Trotz offener Punkte abschließen" in r.text and 'name="begruendung"' in r.text


def test_month_detail_bad_month(client, year):
    assert client.get("/monate/2026-13").status_code == 404
    assert client.get("/monate/1990-01").status_code == 404


# --------------------------------------------------------------------------- Mehrarbeit


def test_mehrarbeit_htmx_update_returns_partial(client, year):
    for i in range(4):
        _entry(client, date(2026, 10, 5) + timedelta(days=i), 8, 45, "U", flags=["vertretung_angeordnet"])
    r = client.get("/monate/2026-10")
    t = text_of(r.text)
    assert "ausgleichsfähig" in t and "31.01.2027" in t  # Frist-Hinweis (3 Monate)

    r = client.post("/monate/2026-10/mehrarbeit", data={"status": "beantragt", "notiz": "per Mail", "erhalten": "1,5"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert r.text.lstrip().startswith('<section class="card months-ma" id="mehrarbeit"')
    assert "Mehrarbeit gespeichert." in r.text
    assert '<option value="beantragt" selected>' in r.text and "per Mail" in r.text and 'value="1,5"' in r.text

    r = client.post("/monate/2026-10/mehrarbeit", data={"status": "quatsch"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Ungültiger Status" in r.text


def test_mehrarbeit_form_post_redirects(client, year):
    r = client.post("/monate/2026-10/mehrarbeit", data={"status": "offen", "notiz": ""}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/monate/2026-10#mehrarbeit"


# --------------------------------------------------------------------------- close


def test_close_page_requires_confirmation(client, september):
    r = client.get("/monate/2026-09/abschliessen")
    assert r.status_code == 200 and 'name="bestaetigt"' in r.text
    r = client.post("/monate/2026-09/abschliessen", data={})
    assert r.status_code == 422 and "Bitte bestätigen" in r.text


def test_close_flow_hash_report_and_hint(client, september, settings_env):
    r = client.post("/monate/2026-09/abschliessen", data={"bestaetigt": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/monate/2026-09?abgeschlossen=1"
    r = client.get(r.headers["location"])
    assert r.status_code == 200
    t = text_of(r.text)
    hash_value = re.search(r"<code class=\"months-hash-full\" data-copy-text>([0-9a-f]{64})</code>", r.text).group(1)
    assert "Abgeschlossen am" in t and "Bericht jetzt exportieren und an die eigene Mailadresse senden" in t
    assert "Mail-Zeitstempel als externer Nachweis" in t
    assert 'href="/monate/2026-09/bericht"' in r.text and "abgeschlossen" in t
    assert 'data-dialog-open="close-dialog"' not in r.text

    # stored report with injected toolbar (screen only), original file untouched
    stored = (settings_env.data_dir / "reports" / "2026-09.html").read_text(encoding="utf-8")
    assert "months-report-bar" not in stored
    r = client.get("/monate/2026-09/bericht")
    assert r.status_code == 200 and hash_value in r.text
    assert 'class="months-report-bar"' in r.text and "data-print" in r.text
    assert re.search(r'<script src="/static/js/months.js\?v=[0-9a-f]{12}" defer></script>', r.text)
    assert "Gespeicherte Abschluss-Fassung" in r.text
    r = client.get("/monate/2026-09/bericht", params={"download": "1"})
    assert r.headers["content-disposition"].startswith("attachment") and "months-report-bar" not in r.text
    assert r.text == stored
    r = client.get("/monate/2026-09/bericht", params={"aktuell": "1"})
    assert r.status_code == 200 and "Aktueller Stand" in r.text

    # list shows status + short hash; closing again just redirects with a flash
    r = client.get("/monate")
    assert hash_value[:10] in r.text
    r = client.get("/monate/2026-09/abschliessen", follow_redirects=False)
    assert r.status_code == 303


def test_close_with_open_items_needs_note(client, year):
    for d in SEPT_SCHOOL_DAYS[1:]:
        _entry(client, d, 8, 60)
    r = client.post("/monate/2026-09/abschliessen", data={"bestaetigt": "1"})
    assert r.status_code == 409 and "offene" in text_of(r.text).lower()
    r = client.post("/monate/2026-09/abschliessen", data={"bestaetigt": "1", "trotzdem": "1"})
    assert r.status_code == 422 and "begründen" in r.text
    r = client.post("/monate/2026-09/abschliessen",
                    data={"bestaetigt": "1", "trotzdem": "1", "begruendung": "Wandertag"}, follow_redirects=False)
    assert r.status_code == 303
    r = client.get("/monate/2026-09/bericht")
    assert "Wandertag" in r.text


def test_preliminary_report_for_open_month(client, september):
    r = client.get("/monate/2026-09/bericht")
    assert r.status_code == 200 and "Vorläufiger Bericht" in r.text
    assert "Monatsbericht September 2026" in r.text


# --------------------------------------------------------------------------- year report


def test_year_report_with_toolbar(client, september):
    r = client.get("/berichte/jahr", params={"periode": "2026/27"})
    assert r.status_code == 200
    assert "Jahresbericht 2026/27" in r.text and 'class="months-report-bar"' in r.text
    assert "Ohne Eintragsliste" in r.text
    r = client.get("/berichte/jahr", params={"periode": "2026/27", "eintraege": "0", "download": "1"})
    assert 'filename="Jahresbericht_2026-27_kurz.html"' in r.headers["content-disposition"]
    assert client.get("/berichte/jahr", params={"periode": "1999/00"}).status_code == 404


def test_static_assets_served(client):
    for path in ("/static/css/months.css", "/static/js/months.js", "/static/js/import.js"):
        assert client.get(path).status_code == 200
