"""API tests for /api/import (multipart preview, validate, commit, undo, mapping memory)."""

from __future__ import annotations

import io
from datetime import datetime, time

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.db.models import MonthClose, TimeEntry
from app.db.session import SessionLocal

CSV = "Datum;Von;Bis;Dauer;Tätigkeit;Klasse\n01.10.2026;;;2,5;Korrektur;3b\n02.10.2026;08:00;09:30;;Unterricht;\n"


@pytest.fixture(autouse=True)
def _school_year(request):
    """Rows outside every period are not imported by default: create the school year 2026/27."""
    if "client" in request.fixturenames:
        from app.db.session import SessionLocal
        from app.services import calendar_service as cal

        request.getfixturevalue("client")
        with SessionLocal() as s:
            cal.create_period(s, "2026/27")
            s.commit()


def upload(client, content: bytes | str, filename: str = "zeiten.csv", **form):
    if isinstance(content, str):
        content = content.encode("cp1252")
    return client.post("/api/import/preview", files={"file": (filename, content, "application/octet-stream")},
                       data=form)


def xlsx_bytes(sheets: dict[str, list[list]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def batch_entries(batch_id: str, include_deleted: bool = False) -> list[TimeEntry]:
    with SessionLocal() as s:
        q = select(TimeEntry).where(TimeEntry.import_batch_id == batch_id)
        if not include_deleted:
            q = q.where(TimeEntry.deleted_at.is_(None))
        return list(s.scalars(q))


def test_full_wizard_flow(client):
    r = upload(client, CSV)
    assert r.status_code == 201, r.text
    preview = r.json()
    assert preview["file_type"] == "csv" and preview["encoding"] == "cp1252" and preview["delimiter"] == ";"
    assert preview["headers"][:4] == ["Datum", "Von", "Bis", "Dauer"]
    assert preview["mapping"]["kategorie"] == 4
    assert preview["rows"][0]["cells"][3] == "2,5"
    assert any(c["code"] == "K" for c in preview["categories"])
    batch_id = preview["batch_id"]

    r = client.post(f"/api/import/{batch_id}/validate", json={"mapping": preview["mapping"]})
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["summary"]["ok"] == 2 and report["can_commit"] is True
    assert report["rows"][0]["duration_hm"] == "2:30" and report["rows"][0]["category_code"] == "K"

    r = client.post("/api/import/commit", json={"batch_id": batch_id})
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["imported_rows"] == 2 and result["entries_created"] == 2
    assert result["batch"]["stage"] == "importiert"

    entries = client.get("/api/entries", params={"source": "import"}).json()["entries"]
    unbounded = next(e for e in entries if not e["has_bounds"])
    assert unbounded["duration_seconds"] == 9000 and "ohne_zeitgrenzen" in unbounded["flags"]
    assert unbounded["import_batch_id"] == batch_id

    r = client.get("/api/import")
    assert r.status_code == 200
    (listed,) = r.json()["batches"]
    assert listed["id"] == batch_id and listed["entries_active"] == 2 and listed["rows_imported"] == 2

    r = client.post("/api/import/commit", json={"batch_id": batch_id})
    assert r.status_code == 409

    r = client.delete(f"/api/import/{batch_id}")
    assert r.status_code == 200, r.text
    assert r.json()["entries_deleted"] == 2
    assert batch_entries(batch_id) == []
    assert len(batch_entries(batch_id, include_deleted=True)) == 2
    assert client.delete(f"/api/import/{batch_id}").status_code == 409


def test_commit_with_mapping_and_options_in_body(client):
    batch_id = upload(client, "Datum;Dauer;Was\n01.10.2026;1;Hefte\n01.10.2026;1;Hefte\n").json()["batch_id"]
    body = {"batch_id": batch_id, "mapping": {"datum": "Datum", "dauer": "Dauer", "kategorie": "C"},
            "options": {"category_map": {"Hefte": "K.2"}, "include_rows": [3]}}
    r = client.post("/api/import/commit", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["imported_rows"] == 2 and r.json()["mappings_remembered"] == 1

    mappings = client.get("/api/import/mappings").json()
    assert mappings["mappings"][0]["source_text"] == "Hefte" and mappings["mappings"][0]["category_code"] == "K.2"


def test_validate_reports_unmapped_and_mapping_errors(client):
    batch_id = upload(client, "Datum;Dauer;Tätigkeit\n01.10.2026;1;Elterngespräch\n").json()["batch_id"]
    r = client.post(f"/api/import/{batch_id}/validate", json={"mapping": {"datum": 0, "dauer": 1, "kategorie": 2}})
    assert r.status_code == 200
    report = r.json()
    assert report["unmapped_categories"] == [{"text": "Elterngespräch", "count": 1, "rows": ["2"]}]
    assert report["can_commit"] is False

    r = client.post(f"/api/import/{batch_id}/validate", json={"mapping": {"dauer": 1}})
    assert r.status_code == 422 and "Datum" in r.json()["detail"]
    r = client.post(f"/api/import/{batch_id}/validate",
                    json={"mapping": {"datum": 0, "dauer": 1}, "options": {"default_category": "XX"}})
    assert r.status_code == 422
    r = client.post(f"/api/import/{batch_id}/validate", json={"mapping": {"datum": 0}, "options": {"bogus": 1}})
    assert r.status_code == 422  # pydantic rejects unknown option keys
    r = client.post("/api/import/commit", json={"batch_id": batch_id, "mapping": {"datum": 0, "dauer": 1,
                                                                                    "kategorie": 2}})
    assert r.status_code == 422 and "Keine Zeilen" in r.json()["detail"]


def test_xlsx_upload_with_sheet_selection(client):
    content = xlsx_bytes({
        "Info": [["Hinweis"]],
        "Daten": [["Datum", "Von", "Bis", "Kategorie"], [datetime(2026, 10, 6), time(14), time(15, 30), "KO"]],
    })
    r = upload(client, content, "zeiten.xlsx", sheet="Daten")
    assert r.status_code == 201, r.text
    preview = r.json()
    assert preview["sheets"] == ["Info", "Daten"] and preview["sheet"] == "Daten"
    batch_id = preview["batch_id"]

    r = client.get(f"/api/import/{batch_id}", params={"sheet": "Info"})
    assert r.status_code == 200
    assert r.json()["preview"]["sheet"] == "Info" and r.json()["batch"]["file_type"] == "xlsx"
    assert client.get(f"/api/import/{batch_id}", params={"sheet": "Nope"}).status_code == 422

    r = client.post(f"/api/import/{batch_id}/validate", json={"mapping": preview["mapping"], "sheet": "Daten"})
    assert r.status_code == 200
    row = r.json()["rows"][0]
    assert row["ref"] == "Daten!2" and row["von"] == "14:00" and row["duration_seconds"] == 5400
    r = client.post("/api/import/commit", json={"batch_id": batch_id})
    assert r.status_code == 200 and r.json()["imported_rows"] == 1


def test_upload_errors(client, monkeypatch):
    assert upload(client, b"").status_code == 422
    r = upload(client, b"\xd0\xcf\x11\xe0" + b"\x00" * 64, "alt.xls")
    assert r.status_code == 422 and "xlsx" in r.json()["detail"].lower()
    from app.services import import_service

    monkeypatch.setattr(import_service, "MAX_UPLOAD_BYTES", 16)
    r = upload(client, CSV)
    assert r.status_code == 413
    assert client.post("/api/import/preview").status_code == 422  # no file


def test_unknown_batch_is_404(client):
    assert client.get("/api/import/does-not-exist").status_code == 404
    assert client.post("/api/import/does-not-exist/validate", json={"mapping": {"datum": 0}}).status_code == 404
    assert client.post("/api/import/commit", json={"batch_id": "does-not-exist"}).status_code == 404
    assert client.delete("/api/import/does-not-exist").status_code == 404


def test_discard_uploaded_batch(client):
    batch_id = upload(client, CSV).json()["batch_id"]
    r = client.delete(f"/api/import/{batch_id}")
    assert r.status_code == 200 and r.json()["discarded"] is True
    assert client.get(f"/api/import/{batch_id}").status_code == 404


def test_undo_locked_month_needs_reason(client):
    batch_id = upload(client, "Datum;Dauer;Kategorie\n01.09.2026;1;K\n").json()["batch_id"]
    assert client.post("/api/import/commit", json={"batch_id": batch_id, "mapping": {"datum": 0, "dauer": 1,
                                                                                       "kategorie": 2}}).status_code == 200
    with SessionLocal() as s:
        s.add(MonthClose(year_month="2026-09", entries_hash_sha256="0" * 64, soll_seconds=0, ist_seconds=0,
                         saldo_seconds=0))
        s.commit()
    r = client.delete(f"/api/import/{batch_id}")
    assert r.status_code == 423 and "1 Eintrag" in r.json()["detail"]
    assert client.get(f"/api/import/{batch_id}").json()["batch"]["entries_locked"] == 1
    r = client.request("DELETE", f"/api/import/{batch_id}", json={"reason": "Falsche Datei"})
    assert r.status_code == 200 and r.json()["entries_deleted"] == 1


def test_undo_reason_via_query(client):
    batch_id = upload(client, "Datum;Dauer;Kategorie\n01.09.2026;1;K\n").json()["batch_id"]
    client.post("/api/import/commit", json={"batch_id": batch_id, "mapping": {"datum": 0, "dauer": 1, "kategorie": 2}})
    with SessionLocal() as s:
        s.add(MonthClose(year_month="2026-09", entries_hash_sha256="0" * 64, soll_seconds=0, ist_seconds=0,
                         saldo_seconds=0))
        s.commit()
    r = client.delete(f"/api/import/{batch_id}", params={"reason": "Doppelt"})
    assert r.status_code == 200, r.text


def test_mapping_endpoints(client):
    r = client.put("/api/import/mappings", json={"source_text": "Korrigieren", "category_code": "k"})
    assert r.status_code == 200 and r.json()["mapping"]["category_code"] == "K"
    r = client.put("/api/import/mappings", json={"source_text": "x", "category_code": "NOPE"})
    assert r.status_code == 422
    r = client.get("/api/import/mappings")
    assert r.json()["count"] == 1 and r.json()["mappings"][0]["category_name"].startswith("Korrektur")
    assert client.delete("/api/import/mappings", params={"source_text": "korrigieren"}).status_code == 200
    assert client.delete("/api/import/mappings", params={"source_text": "korrigieren"}).status_code == 404
    assert client.get("/api/import/mappings").json()["count"] == 0
