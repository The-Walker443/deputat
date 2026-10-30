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
    assert "Die amtlichen Ferientermine wurden übernommen." in r.text
    assert "Bayerisches Kultusministerium</a>, Stand 30.10.2026" in r.text
    assert 'href="https://www.verkuendung-bayern.de/baymbl/2022-747/"' in r.text
    assert r.text.count('class="badge badge-success">amtlich<') == 6
    assert "Andere Termine importieren (ICS-Datei)" in r.text  # ICS import stays as optional alternative
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


@pytest.mark.usefixtures("no_ferien_catalog")  # builds its own ferien (no catalogue)
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


@pytest.mark.usefixtures("no_ferien_catalog")  # builds its own ferien (no catalogue)
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


# --------------------------------------------------------------------------- official ferien (catalogue)


def test_period_creation_flashes_official_ferien(client):
    r = client.post("/kalender/perioden", data={"label": "2026/27"}, follow_redirects=True)
    assert "6 amtliche Ferienabschnitte übernommen (Bayerisches Kultusministerium, Stand 30.10.2026)" in r.text
    assert "Noch keine Ferien erfasst" not in r.text
    r = client.get(f"/kalender/ferien?periode={_period_id()}")
    assert r.text.count(">amtlich</span>") == 6
    assert "Amtliche Ferientermine – Quelle:" in r.text
    assert "Andere Termine importieren (ICS-Datei)" in r.text and "Durch amtliche Termine ersetzen" not in r.text


def test_uncovered_year_shows_hint_and_fallbacks(client):
    r = client.post("/kalender/perioden", data={"label": "2023/24", "wizard": "1"}, follow_redirects=True)
    hint = "Für dieses Schuljahr sind keine amtlichen Termine hinterlegt – bitte per ICS importieren oder manuell erfassen"
    assert hint in r.text
    assert "Aus ICS-Datei importieren" in r.text and "Ferien von Hand eintragen" in r.text
    assert "Amtliche Ferien übernehmen" not in r.text
    r = client.get("/kalender")
    assert "Noch keine Ferien erfasst" in r.text and hint in r.text


def test_banner_offers_official_ferien_for_period_without_ferien(client):
    with SessionLocal() as s:
        from app.services import calendar_service as cal

        pid = cal.create_period(s, "2026/27", apply_catalog=False).id
    r = client.get("/kalender")
    assert "data-ferien-katalog" in r.text and "Amtliche Ferien übernehmen" in r.text
    assert 'action="/kalender/ferien/katalog"' in r.text and 'name="ersetzen"' not in r.text
    r = client.post("/kalender/ferien/katalog", data={"periode": pid, "next": f"/kalender?periode={pid}"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/kalender?periode={pid}"
    with SessionLocal() as s:
        assert {f.source for f in s.scalars(select(Ferien))} == {"katalog"}
        assert len(s.scalars(select(Ferien)).all()) == 6
    r = client.get("/kalender")
    assert "data-ferien-katalog" not in r.text
    r = client.post("/kalender/ferien/katalog", data={"periode": pid}, follow_redirects=True)
    assert "Die amtlichen Ferien sind bereits übernommen." in r.text


def test_banner_offers_replacing_example_ferien(client):
    with SessionLocal() as s:
        from app.services import calendar_service as cal

        period = cal.create_period(s, "2026/27", apply_catalog=False)
        cal.commit_ics(s, period, EXAMPLE_ICS.read_bytes())
        pid = period.id
    r = client.get("/kalender")
    assert "Die Ferien stammen aus der Beispieldatei" in r.text
    assert 'name="ersetzen" value="1"' in r.text and "data-confirm=" in r.text
    assert 'name="grund"' not in r.text  # no closed month in this period
    r = client.get(f"/kalender/einrichtung?periode={pid}&schritt=2")
    assert "Die Ferien stammen aus der Beispieldatei" in r.text

    r = client.post("/kalender/ferien/katalog", data={"periode": pid, "ersetzen": "1", "next": "/kalender"},
                    follow_redirects=True)
    assert "6 amtliche Ferienabschnitt(e) übernommen." in r.text
    assert "Die Ferien stammen aus der Beispieldatei" not in r.text
    with SessionLocal() as s:
        names = [f.name for f in s.scalars(select(Ferien).order_by(Ferien.start))]
    assert names[0] == "Sommerferien 2026" and not any("BEISPIEL" in n for n in names)


def test_replace_in_closed_month_asks_for_reason(client):
    from datetime import datetime

    from app.db.models import MonthClose
    from app.services import calendar_service as cal
    from app.services import entries as entries_svc

    with SessionLocal() as s:
        period = cal.create_period(s, "2026/27", apply_catalog=False)
        cal.commit_ics(s, period, EXAMPLE_ICS.read_bytes())
        herbst = s.scalars(select(Ferien).where(Ferien.kind == "herbst")).one()
        cal.update_ferien(s, herbst.id, start=date(2026, 10, 26), end=date(2026, 10, 30))
        s.add(MonthClose(year_month="2026-10", closed_at=datetime(2026, 11, 2, 10),
                         entries_hash_sha256=entries_svc.month_entries_hash(s, "2026-10"),
                         soll_seconds=0, ist_seconds=0, saldo_seconds=0))
        s.commit()
        pid = period.id
    r = client.get("/kalender")
    assert 'name="grund"' in r.text
    r = client.post("/kalender/ferien/katalog", data={"periode": pid, "ersetzen": "1", "next": "/kalender"},
                    follow_redirects=True)
    assert "abgeschlossenen Monat 2026-10" in r.text and "Begründung" in r.text
    with SessionLocal() as s:
        assert any("BEISPIEL" in f.name for f in s.scalars(select(Ferien)))
    r = client.post("/kalender/ferien/katalog",
                    data={"periode": pid, "ersetzen": "1", "grund": "Beispieldaten ersetzt", "next": "/kalender"},
                    follow_redirects=True)
    assert "Nachträglich korrigiert: 2026-10." in r.text
    with SessionLocal() as s:
        assert not any("BEISPIEL" in f.name for f in s.scalars(select(Ferien)))
        assert s.get(MonthClose, "2026-10").status == "nachtraeglich_korrigiert"


def _close_month(s, ym: str) -> None:
    from datetime import datetime

    from app.db.models import MonthClose
    from app.services import entries as entries_svc

    s.add(MonthClose(year_month=ym, closed_at=datetime(2026, 11, 2, 10),
                     entries_hash_sha256=entries_svc.month_entries_hash(s, ym),
                     soll_seconds=0, ist_seconds=0, saldo_seconds=0))
    s.commit()


def test_apply_into_closed_month_asks_for_reason(client):
    # No ferien yet, but November is closed: taking over the Herbstferien changes it -> reason field (no dead end).
    from app.db.models import MonthClose
    from app.services import calendar_service as cal

    with SessionLocal() as s:
        pid = cal.create_period(s, "2026/27", apply_catalog=False).id
        _close_month(s, "2026-11")
    for url in ("/kalender", f"/kalender/ferien?periode={pid}", f"/kalender/einrichtung?periode={pid}&schritt=2"):
        r = client.get(url)
        assert "Amtliche Ferien übernehmen" in r.text and 'type="hidden" name="ersetzen"' not in r.text, url
        assert 'name="grund"' in r.text and "betrifft den abgeschlossenen Monat 2026-11" in r.text, url

    r = client.post("/kalender/ferien/katalog", data={"periode": pid, "next": f"/kalender/ferien?periode={pid}"},
                    follow_redirects=True)
    assert "abgeschlossenen Monat 2026-11" in r.text and "Bitte im Feld „Begründung“ angeben" in r.text
    assert 'name="grund"' in r.text  # the field the message points to is there
    with SessionLocal() as s:
        assert s.scalars(select(Ferien)).all() == []
        assert s.get(MonthClose, "2026-11").status == "abgeschlossen"

    r = client.post("/kalender/ferien/katalog",
                    data={"periode": pid, "grund": "Amtliche Ferien nachgetragen", "next": "/kalender"},
                    follow_redirects=True)
    assert "6 amtliche Ferienabschnitt(e) übernommen." in r.text and "Nachträglich korrigiert: 2026-11." in r.text
    with SessionLocal() as s:
        assert len(s.scalars(select(Ferien)).all()) == 6
        assert s.get(MonthClose, "2026-11").status == "nachtraeglich_korrigiert"


def test_reason_field_for_closed_month_outside_the_period(client):
    # An ICS row of 2027/28 reaches into August 2028 (period 2028/29): replacing it changes that closed month.
    from app.services import calendar_service as cal

    with SessionLocal() as s:
        p2728 = cal.create_period(s, "2027/28", apply_catalog=False)
        cal.create_period(s, "2028/29", apply_catalog=False)
        cal.create_ferien(s, p2728, name="Sommerferien 2028", start=date(2028, 7, 31), end=date(2028, 9, 11),
                          source="ics")
        pid = p2728.id
        r = client.get(f"/kalender/ferien?periode={pid}")
        assert "Amtliche Termine übernehmen" in r.text and 'name="grund"' not in r.text
        _close_month(s, "2028-08")
    r = client.get(f"/kalender/ferien?periode={pid}")
    assert 'name="grund"' in r.text and "betrifft den abgeschlossenen Monat 2028-08" in r.text
