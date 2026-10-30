"""Web tests for the Kalender screens (app/web/calendar.py): wizard, month view, day editor, ferien, ICS."""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import DayOverride, Ferien, Period, Settings
from app.db.session import SessionLocal

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_ICS = ROOT / "examples" / "ferien_bayern_BEISPIEL.ics"


def _period_id() -> str:
    with SessionLocal() as s:
        return s.scalars(select(Period.id).order_by(Period.start)).first()


def _create(client, label: str = "2026/27") -> str:
    r = client.post("/kalender/perioden", data={"label": label, "wizard": "1"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert "schritt=2" in r.headers["location"]
    return _period_id()


def _add_ferien(client, pid: str, name: str, start: str, end: str) -> None:
    r = client.post("/kalender/ferien", data={"periode": pid, "name": name, "start": start, "end": end})
    assert r.status_code == 200


def test_no_period_shows_wizard_step_1(client):
    r = client.get("/kalender")
    assert r.status_code == 200
    assert "Schuljahr anlegen" in r.text
    assert 'action="/kalender/perioden"' in r.text
    assert "admin.css" in r.text and "calendar.js" in r.text


def test_wizard_flow(client):
    pid = _create(client)
    r = client.get(f"/kalender/einrichtung?periode={pid}&schritt=2")
    assert r.status_code == 200
    assert "Aus ICS-Datei importieren" in r.text
    assert "BEISPIEL" in r.text and "Kultusministeriums" in r.text and "examples/" not in r.text
    r = client.get(f"/kalender/einrichtung?periode={pid}&schritt=3")
    assert "Mariä Himmelfahrt" in r.text and "Allerheiligen" in r.text
    r = client.post("/kalender/feiertage", data={"next": f"/kalender/einrichtung?periode={pid}&schritt=3"},
                    follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as s:
        assert s.get(Settings, 1).regionale_feiertage == []
    r = client.get(f"/kalender/einrichtung?periode={pid}&schritt=4")
    assert "Urlaubsmodus" in r.text and "Vergleich der Urlaubsvarianten" in r.text
    r = client.post("/kalender/urlaubsmodus", data={"urlaubs_modus": "proportional", "next": "/kalender"},
                    follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as s:
        assert s.get(Settings, 1).urlaubs_modus == "proportional"


def test_month_view_and_day_types(client):
    pid = _create(client)
    _add_ferien(client, pid, "Herbstferien", "2026-11-02", "2026-11-06")
    r = client.get(f"/kalender?periode={pid}&monat=2026-11")
    assert r.status_code == 200
    assert "November 2026" in r.text
    assert "cal-ferientag" in r.text and "cal-feiertag" in r.text and "cal-wochenende" in r.text
    assert "Allerheiligen" in r.text
    assert 'hx-get="/kalender/tag/2026-11-02"' in r.text
    # Buß- und Bettag 2026 = 18.11. (unterrichtsfrei)
    assert re.search(r'cal-unterrichtsfrei[^"]*" href="/kalender/tag/2026-11-18"', r.text)


def test_day_editor_partial_and_override(client):
    _create(client)
    r = client.get("/kalender/tag/2026-10-14", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<html" not in r.text
    assert "Mittwoch, 14.10.2026" in r.text
    r = client.get("/kalender/tag/2026-10-14")
    assert "<html" in r.text
    r = client.post("/kalender/tag/2026-10-14", data={"absence_type": "krank", "note": "Grippe"},
                    follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as s:
        ov = s.get(DayOverride, date(2026, 10, 14))
        assert ov.absence_type == "krank" and ov.note == "Grippe"
    r = client.get("/kalender?monat=2026-10")
    assert "abs-krank" in r.text
    r = client.post("/kalender/tag/2026-10-14", data={"aktion": "entfernen"}, follow_redirects=False)
    with SessionLocal() as s:
        assert s.get(DayOverride, date(2026, 10, 14)) is None


def test_manual_leave_counter(client):
    pid = _create(client)
    _add_ferien(client, pid, "Herbstferien", "2026-11-02", "2026-11-06")
    client.post("/kalender/urlaubsmodus", data={"urlaubs_modus": "manuell"})
    r = client.get("/kalender/tag/2026-11-03", headers={"HX-Request": "true"})
    assert "Urlaub (manuelle Zuordnung)" in r.text and "data-leave-remaining" in r.text
    r = client.post("/kalender/tag/2026-11-03", data={"urlaub_manuell": "1"}, follow_redirects=True)
    assert r.status_code == 200
    assert "Noch zuzuordnen" in r.text
    with SessionLocal() as s:
        assert s.get(DayOverride, date(2026, 11, 3)).urlaub_manuell is True


def test_ferien_crud(client):
    pid = _create(client)
    _add_ferien(client, pid, "Herbstferien", "2026-11-02", "2026-11-06")
    r = client.get(f"/kalender/ferien?periode={pid}")
    assert "Herbstferien" in r.text and "Werktage" in r.text
    with SessionLocal() as s:
        fid = s.scalars(select(Ferien.id)).one()
    client.post(f"/kalender/ferien/{fid}", data={"name": "Herbst", "start": "2026-11-02", "end": "2026-11-04"})
    with SessionLocal() as s:
        f = s.get(Ferien, fid)
        assert (f.name, f.end) == ("Herbst", date(2026, 11, 4))
    client.post(f"/kalender/ferien/{fid}/loeschen", data={})
    with SessionLocal() as s:
        assert s.get(Ferien, fid) is None


def test_ics_preview_and_commit(client):
    pid = _create(client)
    content = EXAMPLE_ICS.read_bytes()
    r = client.post("/kalender/ics/vorschau", data={"periode": pid, "wizard": "1"},
                    files={"datei": ("ferien_bayern_BEISPIEL.ics", content, "text/calendar")})
    assert r.status_code == 200
    assert "Beispieldatei" in r.text
    selected = re.findall(r'name="auswahl" value="(\d+)"[^>]*checked', r.text)
    assert selected
    b64 = re.search(r'name="inhalt" value="([^"]+)"', r.text).group(1)
    with SessionLocal() as s:
        assert s.scalars(select(Ferien)).first() is None  # preview writes nothing
    r = client.post("/kalender/ics/import",
                    data={"periode": pid, "inhalt": b64, "wizard": "1", "auswahl": selected, "geprueft": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "schritt=2" in r.headers["location"]
    with SessionLocal() as s:
        assert len(s.scalars(select(Ferien)).all()) == len(selected)


def test_period_switch_and_delete(client):
    _create(client, "2026/27")
    client.post("/kalender/perioden", data={"label": "2027/28"})
    r = client.get("/kalender")
    assert 'id="period-switch"' in r.text and "2027/28" in r.text
    with SessionLocal() as s:
        p2 = s.scalars(select(Period).where(Period.label == "2027/28")).one().id
    r = client.post(f"/kalender/perioden/{p2}/loeschen", follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as s:
        assert s.get(Period, p2) is None


def test_range_entry(client):
    _create(client)
    r = client.post("/kalender/zeitraum", data={"start": "2026-10-12", "end": "2026-10-16",
                                               "absence_type": "fortbildung", "next": "/kalender"},
                    follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as s:
        rows = s.scalars(select(DayOverride).where(DayOverride.absence_type == "fortbildung")).all()
        assert len(rows) == 5


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("rel", ["admin.js", "calendar.js", "settings.js", "timetable.js"])
def test_js_syntax(rel):
    result = subprocess.run(["node", "--check", str(ROOT / "app" / "static" / "js" / rel)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_no_inline_scripts_or_handlers():
    templates = ROOT / "app" / "web" / "templates"
    files = [*templates.glob("calendar*.html"), *templates.glob("settings*.html"), *templates.glob("timetable*.html"),
             templates / "mehr.html", *templates.glob("partials/calendar_*.html"),
             *templates.glob("partials/settings_*.html"), *templates.glob("partials/timetable_*.html")]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), path.name
        assert not re.search(r"\son[a-z]+\s*=", text), path.name
