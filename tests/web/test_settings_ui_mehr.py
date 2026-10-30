"""Web tests for the tab 'Mehr' (template mehr.html, routed by app/web/pages.py)."""

from __future__ import annotations


def test_mehr_menu_links_all_areas(client):
    r = client.get("/mehr")
    assert r.status_code == 200
    for href in ("/eintraege", "/kalender", "/stundenplan", "/monate", "/import", "/export", "/schreiben",
                 "/einstellungen"):
        assert f'href="{href}"' in r.text, href
    assert "Export &amp; Backup" in r.text
    assert "Rechenregeln" in r.text and "Referenz-Soll" in r.text
    assert "/logout" not in r.text and "Abmelden" not in r.text
