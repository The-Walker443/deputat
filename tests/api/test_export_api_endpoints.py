"""API: /api/export/csv|json|ics, /api/export/restore and /api/backup."""

from __future__ import annotations

import gzip
import json

import pytest


@pytest.fixture
def with_entry(client):
    from sqlalchemy import select

    from app.db.models import Category
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        cat = db.scalar(select(Category.id).where(Category.code == "K"))
    r = client.post("/api/entries", json={
        "start_at": "2026-10-20T15:00:00+02:00", "end_at": "2026-10-20T16:30:00+02:00", "category_id": cat,
        "note": "Hefte",
    })
    assert r.status_code == 201, r.text
    return r.json()["entry"]


def test_csv_download_and_stored_copy(client, with_entry, settings_env):
    r = client.get("/api/export/csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"].startswith('attachment; filename="deputat_eintraege_')
    assert r.content.startswith(b"\xef\xbb\xbf")
    text = r.content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("Datum;Wochentag;Von;Bis")
    assert "1,50" in text and "Hefte" in text
    stored = list(settings_env.exports_dir.glob("deputat_eintraege_*.csv"))
    assert len(stored) == 1 and stored[0].read_bytes() == r.content


def test_csv_invalid_range(client):
    r = client.get("/api/export/csv", params={"date_from": "2026-10-02", "date_to": "2026-10-01"})
    assert r.status_code == 422


def test_ics_download(client, with_entry, settings_env):
    r = client.get("/api/export/ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert r.text.count("BEGIN:VEVENT") == 1 and f"UID:{with_entry['id']}@deputat" in r.text
    assert list(settings_env.exports_dir.glob("*.ics"))


def test_json_export_and_restore_roundtrip(client, with_entry, settings_env):
    r = client.get("/api/export/json")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    backup = r.content
    data = json.loads(backup)
    assert data["format"] == "deputat-backup" and data["counts"]["time_entry"] == 1
    assert list(settings_env.exports_dir.glob("deputat_backup_*.json"))

    # change data, then restore
    r = client.delete(f"/api/entries/{with_entry['id']}")
    assert r.status_code in (200, 204)

    files = {"file": ("backup.json", backup, "application/json")}
    r = client.post("/api/export/restore", files=files)
    assert r.status_code == 400 and "confirm=true" in r.json()["detail"]

    r = client.post("/api/export/restore", files=files, data={"validate_only": "true"})
    assert r.status_code == 200 and r.json()["restored"] is False and r.json()["counts"]["time_entry"] == 1

    r = client.post("/api/export/restore", files=files, data={"confirm": "true"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restored"] and body["replaced"] and body["sicherheitskopie"]
    entry = client.get(f"/api/entries/{with_entry['id']}").json()
    entry = entry.get("entry", entry)
    assert entry["deleted_at"] is None


def test_restore_raw_json_body_with_query_confirm(client, with_entry):
    backup = client.get("/api/export/json").content
    r = client.post("/api/export/restore", params={"confirm": True}, content=backup,
                    headers={"content-type": "application/json"})
    assert r.status_code == 200, r.text


def test_restore_rejects_invalid_files(client):
    r = client.post("/api/export/restore", params={"confirm": True}, content=b"{kaputt",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400 and "JSON" in r.json()["detail"]
    r = client.post("/api/export/restore", params={"confirm": True}, content=b'{"format": "x"}',
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
    r = client.post("/api/export/restore", files={"other": ("a.json", b"{}", "application/json")},
                    data={"confirm": "true"})
    assert r.status_code == 422
    r = client.post("/api/export/restore", params={"confirm": True})
    assert r.status_code == 422


def test_restore_requires_csrf_when_enforced(tmp_path, monkeypatch, _migrated_template):
    """With CSRF enforcement a cross-site upload without token is rejected."""
    from fastapi.testclient import TestClient

    from app.config import reset_config
    from app.main import create_app

    import sqlite3

    data_dir = tmp_path / "csrf"
    data_dir.mkdir()
    src, dst = sqlite3.connect(_migrated_template), sqlite3.connect(data_dir / "deputat.sqlite")
    src.backup(dst)
    src.close()
    dst.close()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("APP_SECRET", "csrf-test-secret-0123456789-0123456789")
    monkeypatch.setenv("CSRF_ENFORCE", "true")
    reset_config()
    try:
        with TestClient(create_app()) as c:
            r = c.post("/api/export/restore", params={"confirm": True}, content=b"{}",
                       headers={"content-type": "application/json"})
            assert r.status_code == 403
            r = c.post("/api/backup/run")
            assert r.status_code == 403
    finally:
        reset_config()


# =========================================================================== backup API


def test_backup_run_list_download(client, settings_env):
    r = client.post("/api/backup/run")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["file"].endswith(".sqlite.gz")
    r = client.get("/api/backup")
    assert r.status_code == 200
    listing = r.json()
    assert listing["log"][0]["file"] == body["file"] and listing["log"][0]["kind"] == "manuell"
    assert listing["files"][0]["name"] == body["file"]
    r = client.get(body["download_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
    assert gzip.decompress(r.content)[:16] == b"SQLite format 3\x00"


def test_backup_download_rejects_bad_names(client, settings_env):
    assert client.get("/api/backup/files/deputat.sqlite").status_code == 404
    assert client.get("/api/backup/files/..%2Fdeputat.sqlite").status_code == 404
    assert client.get("/api/backup/files/2020-01-01.sqlite.gz").status_code == 404


def test_backup_run_failure_returns_500(client, monkeypatch):
    from app.services import backup

    monkeypatch.setattr(backup, "_snapshot", lambda *a: (_ for _ in ()).throw(OSError("voll")))
    r = client.post("/api/backup/run")
    assert r.status_code == 500 and "voll" in r.json()["detail"]
