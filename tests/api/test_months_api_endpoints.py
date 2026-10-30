"""API: /api/months, /api/months/{ym}/close, /mehrarbeit, /report and /api/reports/year."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

EXAMPLE_ICS = Path(__file__).resolve().parents[2] / "examples" / "ferien_bayern_BEISPIEL.ics"
SEPT_SCHOOL_DAYS = [date(2026, 9, d) for d in (15, 16, 17, 18, 21, 22, 23, 24, 25, 28, 29, 30)]


def _cat(client, code: str) -> int:
    from sqlalchemy import select

    from app.db.models import Category
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        return db.scalar(select(Category.id).where(Category.code == code))


def _entry(client, day: date, hour: int, minutes: int, code: str = "VN", **extra) -> dict:
    start = datetime(day.year, day.month, day.day, hour, 0).isoformat() + "+02:00"
    end = (datetime(day.year, day.month, day.day, hour, 0) + timedelta(minutes=minutes)).isoformat() + "+02:00"
    r = client.post("/api/entries", json={"start_at": start, "end_at": end, "category_id": _cat(client, code), **extra})
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


def test_list_months(client, september):
    r = client.get("/api/months", params={"period": "2026/27"})
    assert r.status_code == 200
    data = r.json()
    assert data["period"]["label"] == "2026/27"
    assert len(data["months"]) == 12
    sept = next(m for m in data["months"] if m["year_month"] == "2026-09")
    assert sept["ist"] == 12 * 8 * 3600 and sept["status"] == "offen" and sept["ist_hm"] == "96:00"


def test_list_months_without_period_404(client):
    r = client.get("/api/months")
    assert r.status_code == 404
    assert "Schuljahr" in r.json()["detail"]


def test_get_month_overview(client, september):
    r = client.get("/api/months/2026-09")
    assert r.status_code == 200
    data = r.json()
    assert data["label"] == "September 2026"
    assert data["ist"] == 12 * 8 * 3600 and data["saldo"] == data["ist"] - data["soll"]
    assert data["open_items"] == [] and data["can_close"] is True
    assert data["mehrarbeit"]["ustd_total"] == 0.0
    assert data["eu_mittel"]["ampel"] in ("gruen", "gelb", "rot")
    assert data["period"]["label"] == "2026/27"


def test_get_month_bad_input(client, year):
    assert client.get("/api/months/2026-13").status_code == 422
    assert client.get("/api/months/2020-01").status_code == 404


def test_close_month_flow(client, september, settings_env):
    r = client.post("/api/months/2026-09/close", json={})
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["hash"]) == 64 and data["report_file"] == "reports/2026-09.html"
    assert "Bericht jetzt exportieren" in data["hinweis"]
    assert data["month"]["status"] == "abgeschlossen"
    assert (settings_env.data_dir / "reports" / "2026-09.html").is_file()

    # stored report
    r = client.get(data["report_url"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert data["hash"] in r.text
    r = client.get("/api/months/2026-09/report", params={"stored": True, "download": True})
    assert r.headers["content-disposition"].startswith("attachment")

    # live report
    r = client.get("/api/months/2026-09/report")
    assert r.status_code == 200 and data["hash"] in r.text and "Monatsbericht September 2026" in r.text

    # second close refused, entries locked -> 423 without reason
    r = client.post("/api/months/2026-09/close", json={})
    assert r.status_code == 409 and "bereits abgeschlossen" in r.json()["detail"]
    entries = client.get("/api/entries", params={"date_from": "2026-09-15", "date_to": "2026-09-15"}).json()
    entry = entries[0] if isinstance(entries, list) else entries["entries"][0]
    assert entry["locked"] is True
    r = client.patch(f"/api/entries/{entry['id']}", json={"note": "x"})
    assert r.status_code == 423
    r = client.patch(f"/api/entries/{entry['id']}", json={"note": "x", "reason": "Notiz ergänzt"})
    assert r.status_code == 200
    month = client.get("/api/months/2026-09").json()
    assert month["status"] == "nachtraeglich_korrigiert"
    assert month["abschluss"]["hash_history"][0]["hash"] == data["hash"]


def test_close_refused_with_open_items_then_forced(client, year):
    for d in SEPT_SCHOOL_DAYS[1:]:
        _entry(client, d, 8, 60)
    r = client.post("/api/months/2026-09/close", json={})
    assert r.status_code == 409
    body = r.json()
    assert body["can_force"] is True
    assert body["open_items"] and body["open_items"][0]["kind"] == "schultag_ohne_eintrag"
    r = client.post("/api/months/2026-09/close", json={"force": True})
    assert r.status_code == 422
    r = client.post("/api/months/2026-09/close", json={"force": True, "note": "Wandertag"})
    assert r.status_code == 200
    assert r.json()["forced_items"]


def test_close_running_month_not_forceable(client, year):
    today = date.today()
    ym = f"{today.year:04d}-{today.month:02d}"
    import calendar as pycal

    if today.day == pycal.monthrange(today.year, today.month)[1] or not (date(2026, 8, 1) <= today <= date(2027, 7, 31)):
        pytest.skip("depends on the real date being inside the example period and not on a month's last day")
    r = client.post(f"/api/months/{ym}/close", json={"force": True, "note": "x"})
    assert r.status_code == 409
    assert r.json()["can_force"] is False


def test_stored_report_missing_404(client, september):
    r = client.get("/api/months/2026-09/report", params={"stored": True})
    assert r.status_code == 404


def test_patch_mehrarbeit(client, year):
    for i in range(4):
        _entry(client, date(2026, 10, 5) + timedelta(days=i), 8, 45, "U", flags=["vertretung_angeordnet"])
    r = client.patch("/api/months/2026-10/mehrarbeit", json={"status": "beantragt", "note": "per Mail"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "beantragt" and data["ustd_ausgleichsfaehig"] == 4.0 and data["note"] == "per Mail"
    assert data["frist"] == "2027-01-31"
    r = client.patch("/api/months/2026-10/mehrarbeit", json={"status": "irgendwas"})
    assert r.status_code == 422
    r = client.patch("/api/months/2026-10/mehrarbeit", json={"unbekannt": 1})
    assert r.status_code == 422
    month = client.get("/api/months/2026-10").json()
    assert month["mehrarbeit"]["status"] == "beantragt"


def test_year_report(client, september):
    r = client.get("/api/reports/year", params={"period": "2026/27"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "Jahresbericht 2026/27" in r.text and "<svg" in r.text
    assert "Content-Security-Policy" in r.headers
    r = client.get("/api/reports/year", params={"period": "2026/27", "entries": False, "download": True})
    assert "Vollständige Eintragsliste" not in r.text
    assert 'filename="Jahresbericht_2026-27.html"' in r.headers["content-disposition"]
    assert client.get("/api/reports/year", params={"period": "1999/00"}).status_code == 404


# ------------------------------------------------------------------ review fix: Content-Disposition


def test_year_report_filename_with_unicode_and_quotes_is_safe(client):
    from app.api.months import content_disposition

    header = content_disposition("inline", 'Jahresbericht_Jahr "x"; filename=evil.exe – Test.html')
    header.encode("latin-1")  # must be encodable
    fallback = header.split('filename="', 1)[1].split('"', 1)[0]
    assert '"' not in fallback and ";" not in fallback and " " not in fallback
    assert "filename*=UTF-8''Jahresbericht_Jahr%20%22x%22%3B%20filename%3Devil.exe%20%E2%80%93%20Test.html" in header
    r = client.post("/api/calendar/periods",
                    json={"label": "Schuljahr 2030 – Test", "start": "2030-08-01", "end": "2031-07-31"})
    assert r.status_code == 201, r.text
    r = client.get("/api/reports/year", params={"period": r.json()["id"], "download": "true"})
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith('attachment; filename="Jahresbericht_Schuljahr_2030_Test')
