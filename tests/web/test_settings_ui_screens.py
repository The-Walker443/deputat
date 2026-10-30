"""Web tests for the screen 'Einstellungen' (app/web/settings.py): sections, categories, sensor token."""

from __future__ import annotations

import re
from datetime import date

from sqlalchemy import select

from app.db.models import Category, Settings
from app.db.session import SessionLocal


def _settings() -> Settings:
    with SessionLocal() as s:
        row = s.get(Settings, 1)
        s.expunge(row)
        return row


def _cat(code: str) -> Category:
    with SessionLocal() as s:
        row = s.scalars(select(Category).where(Category.code == code)).one()
        s.expunge(row)
        return row


def test_page_renders_all_sections(client):
    r = client.get("/einstellungen")
    assert r.status_code == 200
    for text in ("UPZ Vollzeit", "UPZ tatsächlich", "Anrechnungsstunden", "Geburtsdatum", "Altersermäßigung",
                 "Wochen-Soll Vollzeit", "Urlaubstage", "Länge einer Unterrichtsstunde", "Kontoperiode",
                 "Urlaubsmodus", "Große Pausen zählen", "Krankheit neutralisiert", "Übertrag ins neue Jahr",
                 "Mariä Himmelfahrt", "Klassenfahrt-Bewertung", "Kappung", "Prognose-Basis", "Klassen", "Fächer",
                 "Zeitraster der Schule", "Kategorien", "Sensor-API", "Datenschutz", 'href="/export"',
                 "Rechenregeln v"):
        assert text in r.text, text
    assert "settings.js" in r.text and "admin.css" in r.text
    assert 'name="zr_start"' in r.text and 'value="08:00"' in r.text


def test_save_upz_with_decimal_comma(client):
    r = client.post("/einstellungen/upz", data={
        "upz_voll": "28", "upz_ist": "20,5", "anrechnungsstunden": "1", "anrechnungs_grund": "Mentorat",
        "geburtsdatum": "", "altersermaessigung": "0",
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/einstellungen#upz"
    s = _settings()
    assert (s.upz_ist, s.anrechnungsstunden, s.anrechnungs_grund) == (20.5, 1.0, "Mentorat")
    assert s.altersermaessigung_manuell is False


def test_altersermaessigung_computed_and_override(client):
    client.post("/einstellungen/upz", data={"upz_voll": "28", "upz_ist": "25", "anrechnungsstunden": "0",
                                            "geburtsdatum": "1960-01-15", "altersermaessigung": "0"})
    s = _settings()
    assert s.geburtsdatum == date(1960, 1, 15) and s.altersermaessigung == 3.0
    client.post("/einstellungen/upz", data={"upz_voll": "28", "upz_ist": "25", "anrechnungsstunden": "0",
                                            "geburtsdatum": "1960-01-15", "altersermaessigung_manuell": "1",
                                            "altersermaessigung": "1,5"})
    s = _settings()
    assert s.altersermaessigung_manuell is True and s.altersermaessigung == 1.5


def test_validation_error_rerenders_with_values(client):
    r = client.post("/einstellungen/upz", data={"upz_voll": "28", "upz_ist": "30", "anrechnungsstunden": "0",
                                                "geburtsdatum": ""})
    assert r.status_code == 422
    assert "Nicht gespeichert" in r.text
    assert "darf die Vollzeit-UPZ nicht übersteigen" in r.text
    assert re.search(r'name="upz_ist" type="number" value="30"', r.text)
    assert 'aria-invalid="true"' in r.text
    assert _settings().upz_ist == 28.0
    r = client.post("/einstellungen/konto", data={"wochen_soll_voll_h": "abc"})
    assert r.status_code == 422 and "Zahl erwartet" in r.text


def test_save_konto_and_regeln(client):
    r = client.post("/einstellungen/konto", data={
        "wochen_soll_voll_h": "40", "urlaubstage": "30", "unterrichtsstunde_min": "45",
        "urlaubs_modus": "proportional", "kontoperiode": "schuljahr", "periode_start_md": "1.8.",
        "uebertrag_verrechnen": "1", "prognose_schultage": "25",
    }, follow_redirects=False)
    assert r.status_code == 303
    s = _settings()
    assert (s.urlaubs_modus, s.periode_start_md, s.uebertrag_verrechnen, s.prognose_schultage) == (
        "proportional", "08-01", True, 25)
    client.post("/einstellungen/regeln", data={"grosse_pause_zaehlt": "1", "klassenfahrt_variante": "A",
                                               "klassenfahrt_cap_h": "11"})
    s = _settings()
    assert s.grosse_pause_zaehlt is True and s.krank_neutralisiert is False
    assert (s.klassenfahrt_variante, s.klassenfahrt_cap_h) == ("A", 11.0)


def test_save_lists_profile_and_holidays(client):
    client.post("/einstellungen/listen", data={"klassen": "3b\n4a, 4a\n", "faecher": "Deutsch\nHSU"})
    client.post("/einstellungen/profil", data={"name": "M. Muster", "schule": "GS Musterstadt", "schulart": "GS"})
    client.post("/einstellungen/feiertage", data={"regionale_feiertage": ["friedensfest"]})
    s = _settings()
    assert s.klassen == ["3b", "4a"] and s.faecher == ["Deutsch", "HSU"]
    assert (s.name, s.schule) == ("M. Muster", "GS Musterstadt")
    assert s.regionale_feiertage == ["friedensfest"]


def test_save_zeitraster(client):
    r = client.post("/einstellungen/zeitraster", data={
        "zr_slot_no": ["1", "2", ""], "zr_label": ["", "Zweite", ""],
        "zr_start": ["07:50", "08:35", ""], "zr_end": ["08:35", "09:20", ""],
    }, follow_redirects=False)
    assert r.status_code == 303
    assert _settings().zeitraster == [
        {"slot_no": 1, "start": "07:50", "end": "08:35", "label": "1. Stunde"},
        {"slot_no": 2, "start": "08:35", "end": "09:20", "label": "Zweite"},
    ]
    r = client.post("/einstellungen/zeitraster", data={"zr_slot_no": ["1"], "zr_label": [""],
                                                       "zr_start": ["09:00"], "zr_end": ["08:00"]})
    assert r.status_code == 422 and "Ende muss nach dem Beginn" in r.text
    assert 'value="09:00"' in r.text  # entered values survive the error


def test_unknown_section_404(client):
    assert client.post("/einstellungen/gibtsnicht", data={}).status_code == 404


def test_sensor_token_lifecycle(client):
    r = client.get("/einstellungen")
    assert "kein Token" in r.text and "&lt;DEIN_TOKEN&gt;" in r.text
    r = client.post("/einstellungen/api-token")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    token = re.search(r"data-token>([^<]+)</output>", r.text).group(1)
    assert len(token) > 30
    assert f"Bearer {token}" in r.text and "/api/sensors" in r.text and "value_json.saldo_h" in r.text
    stored = _settings().api_token_hash
    assert stored and token not in stored
    r = client.get("/einstellungen")
    assert "Token eingerichtet" in r.text and token not in r.text
    r = client.post("/einstellungen/api-token")
    assert "bisherige Token ist ab sofort ungültig" in r.text
    r = client.post("/einstellungen/api-token/loeschen", follow_redirects=False)
    assert r.status_code == 303
    assert _settings().api_token_hash is None


def test_categories_page_and_edit(client):
    r = client.get("/einstellungen/kategorien")
    assert r.status_code == 200
    assert "Unterricht" in r.text and "Vor-/Nachbereitung" in r.text and "Regelunterricht" in r.text
    k = _cat("K")
    r = client.post(f"/einstellungen/kategorien/{k.id}", data={"name": "Korrekturen", "color": "#aa00ff"},
                    follow_redirects=False)
    assert r.status_code == 303
    k2 = _cat("K")
    assert (k2.code, k2.name, k2.color, k2.active) == ("K", "Korrekturen", "#AA00FF", False)
    r = client.post(f"/einstellungen/kategorien/{k.id}", data={"name": "Unterricht", "active": "1"},
                    follow_redirects=True)
    assert "schon" in r.text
    r = client.post(f"/einstellungen/kategorien/{k.id}/unterkategorie", data={"name": "Lernstandserhebung"})
    assert r.status_code == 200
    with SessionLocal() as s:
        sub = s.scalars(select(Category).where(Category.name == "Lernstandserhebung")).one()
        assert sub.code.startswith("K.") and sub.parent_id == k.id
    client.post(f"/einstellungen/kategorien/{k.id}/verschieben", data={"richtung": "up"})
    with SessionLocal() as s:
        mains = s.scalars(select(Category.code).where(Category.parent_id.is_(None)).order_by(Category.sort)).all()
        assert mains.index("K") < mains.index("VN")
    assert client.post("/einstellungen/kategorien/99999", data={"name": "x"}).status_code == 404
