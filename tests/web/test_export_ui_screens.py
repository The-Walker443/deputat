"""Web tests for 'Export & Backup' (app/web/months.py: /export, /export/backup, /export/wiederherstellen)."""

from __future__ import annotations

import re
from html import unescape

from sqlalchemy import func, select

from app.db.models import Period
from app.db.session import SessionLocal


def text_of(html: str) -> str:
    return unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)))


def test_export_page_downloads_and_hints(client):
    r = client.get("/export")
    assert r.status_code == 200
    t = text_of(r.text)
    for href in ('action="/api/export/csv"', 'href="/api/export/json"', 'action="/api/export/ics"'):
        assert href in r.text
    assert "ohne Anmeldung" in t and "Heimnetz" in t
    assert "extern sichern" in t
    assert "ersetzt ALLE vorhandenen Daten" in t
    assert 'name="bestaetigung"' in r.text and 'name="verstanden"' in r.text
    assert 'enctype="multipart/form-data"' in r.text
    assert "Backup jetzt" in t and "Noch keine Sicherungsdatei" in t


def test_export_links_work(client):
    assert client.get("/api/export/csv").status_code == 200
    assert client.get("/api/export/ics").status_code == 200
    r = client.get("/api/export/json")
    assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment")


def test_backup_now_lists_file(client, settings_env):
    r = client.post("/export/backup", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/export#backups"
    r = client.get("/export")
    files = list((settings_env.data_dir / "backups").glob("*.sqlite.gz"))
    assert files
    assert files[0].name in r.text and f'href="/api/backup/files/{files[0].name}"' in r.text
    assert "Backup erstellt" in r.text  # flash
    assert "Protokoll der letzten Läufe" in r.text
    assert client.get(f"/api/backup/files/{files[0].name}").status_code == 200


def test_restore_refused_without_confirmation(client):
    backup = client.get("/api/export/json").content
    r = client.post("/export/wiederherstellen", files={"datei": ("b.json", backup, "application/json")},
                    data={"verstanden": "1", "bestaetigung": "falsch"})
    assert r.status_code == 422 and "Es wurde nichts geändert" in text_of(r.text)
    r = client.post("/export/wiederherstellen", data={"verstanden": "1", "bestaetigung": "ERSETZEN"})
    assert r.status_code == 422 and "JSON-Sicherung auswählen" in r.text


def test_restore_invalid_file(client):
    r = client.post("/export/wiederherstellen", files={"datei": ("b.json", b"{kaputt", "application/json")},
                    data={"verstanden": "1", "bestaetigung": "ERSETZEN"})
    assert r.status_code in (400, 422) and "Ungültiges JSON" in r.text


def test_restore_replaces_data(client):
    backup = client.get("/api/export/json").content  # empty state (no period)
    r = client.post("/api/calendar/periods", json={"label": "2026/27"})
    assert r.status_code == 201
    with SessionLocal() as s:
        assert s.scalar(select(func.count(Period.id))) == 1
    r = client.post("/export/wiederherstellen", files={"datei": ("b.json", backup, "application/json")},
                    data={"verstanden": "1", "bestaetigung": "ersetzen"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    with SessionLocal() as s:
        assert s.scalar(select(func.count(Period.id))) == 0
    r = client.get("/export")
    assert "Sicherung wiederhergestellt" in r.text and "Sicherheitskopie" in r.text
