"""Regression tests from the browser review of 'Einstellungen' and 'Kalender' (area kalender-einstellungen-stundenplan)."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import Period, Settings
from app.db.session import SessionLocal


def test_error_rerender_keeps_decimal_comma_in_number_input(client):
    # type="number" inputs render empty for "20,5" – the re-rendered value must use a decimal point.
    r = client.post("/einstellungen/upz", data={"upz_voll": "28", "upz_ist": "20,5", "anrechnungsstunden": "x",
                                                "geburtsdatum": ""})
    assert r.status_code == 422
    assert 'name="upz_ist" type="number" value="20.5"' in r.text
    assert "Anrechnungsstunden: Zahl erwartet" in r.text


def test_zeitraster_row_with_only_prefilled_number_is_ignored(client):
    # "Weitere Stunde" pre-fills the number; saving without filling the row must not fail.
    r = client.post("/einstellungen/zeitraster", data={
        "zr_slot_no": ["1", "2"], "zr_label": ["", ""],
        "zr_start": ["08:00", ""], "zr_end": ["08:45", ""],
    }, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as s:
        assert [slot["slot_no"] for slot in s.get(Settings, 1).zeitraster] == [1]


def test_manual_mode_hides_misleading_shortfall_hint(client):
    client.post("/kalender/perioden", data={"label": "2026/27"})
    with SessionLocal() as s:
        pid = s.scalars(select(Period.id)).first()
    for name, start, end in (("Sommerferien", "2026-08-03", "2026-09-14"), ("Herbstferien", "2026-11-02", "2026-11-06")):
        client.post("/kalender/ferien", data={"periode": pid, "name": name, "start": start, "end": end})
    client.post("/kalender/urlaubsmodus", data={"urlaubs_modus": "manuell"})
    r = client.get("/kalender/einrichtung?schritt=4")
    assert r.status_code == 200
    assert "Noch zuzuordnen" in r.text
    # enough candidate days exist, so "konnten nicht zugeordnet werden" would be wrong in manual mode
    assert "konnten nicht zugeordnet werden" not in r.text


def test_ics_preview_and_holiday_tables_fit_phone_width(client):
    client.post("/kalender/perioden", data={"label": "2026/27"})
    r = client.get("/kalender/einrichtung?schritt=3")
    assert "<th>Art</th>" not in r.text and "Name · Art" in r.text
