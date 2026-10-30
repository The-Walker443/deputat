"""API tests for /api/entries (status codes, payloads, revisions, locks)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.models import Category, MonthClose
from app.db.session import SessionLocal

BERLIN = ZoneInfo("Europe/Berlin")


def cat_id(code: str) -> int:
    with SessionLocal() as s:
        return s.scalar(select(Category.id).where(Category.code == code))


def recent_local(days_ago: int, hour: int, minute: int = 0) -> datetime:
    d = datetime.now(BERLIN).date() - timedelta(days=days_ago)
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=BERLIN)


def post_entry(client, start, end, code="K", **extra):
    body = {"start_at": start.isoformat(), "end_at": end.isoformat(), "category_id": cat_id(code), **extra}
    return client.post("/api/entries", json=body)


def test_create_get_list(client):
    r = post_entry(client, recent_local(1, 9), recent_local(1, 10, 30), klasse="3b", location="schule")
    assert r.status_code == 201, r.text
    entry = r.json()["entry"]
    assert entry["duration_seconds"] == 5400 and entry["duration_hm"] == "1:30"
    assert entry["category_code"] == "K" and entry["klasse"] == "3b"
    assert entry["start_at"].endswith("Z")

    r = client.get(f"/api/entries/{entry['id']}")
    assert r.status_code == 200 and r.json()["id"] == entry["id"]

    day = entry["day"]
    r = client.get("/api/entries", params={"date_from": day, "date_to": day})
    assert r.status_code == 200
    assert [e["id"] for e in r.json()["entries"]] == [entry["id"]]
    assert client.get("/api/entries", params={"category_id": cat_id("VN")}).json()["count"] == 0


def test_create_duration_only_with_string_duration(client):
    day = recent_local(1, 0).date().isoformat()
    r = client.post("/api/entries", json={"day": day, "duration": "2,5", "category_id": cat_id("K")})
    assert r.status_code == 201, r.text
    entry = r.json()["entry"]
    assert entry["has_bounds"] is False and entry["duration_seconds"] == 9000
    assert "ohne_zeitgrenzen" in entry["flags"]


def test_create_over_midnight_returns_two_entries(client):
    r = post_entry(client, recent_local(2, 22, 30), recent_local(1, 1, 15))
    assert r.status_code == 201
    parts = r.json()["entries"]
    assert len(parts) == 2
    assert sum(p["duration_seconds"] for p in parts) == 2 * 3600 + 45 * 60


def test_validation_and_not_found_status_codes(client):
    assert post_entry(client, recent_local(1, 10), recent_local(1, 9)).status_code == 400
    r = post_entry(client, recent_local(1, 9), recent_local(1, 10), code="S")
    assert r.status_code == 400 and "Notiz" in r.json()["detail"]
    assert post_entry(client, recent_local(1, 9), recent_local(1, 10), bogus=1).status_code == 422
    assert post_entry(client, recent_local(1, 9), recent_local(1, 10), source="timer").status_code == 400
    assert client.get("/api/entries/does-not-exist").status_code == 404
    assert client.patch("/api/entries/does-not-exist", json={"note": "x"}).status_code == 404
    assert client.delete("/api/entries/does-not-exist").status_code == 404
    assert client.get("/api/entries/does-not-exist/revisions").status_code == 404


def test_overlap_is_409(client):
    assert post_entry(client, recent_local(1, 9), recent_local(1, 10)).status_code == 201
    r = post_entry(client, recent_local(1, 9, 30), recent_local(1, 10, 30))
    assert r.status_code == 409
    assert "Überschneidung" in r.json()["detail"]


def test_patch_creates_revision_and_revisions_endpoint(client):
    entry = post_entry(client, recent_local(1, 9), recent_local(1, 10)).json()["entry"]
    r = client.patch(f"/api/entries/{entry['id']}", json={"end_at": recent_local(1, 10, 30).isoformat()})
    assert r.status_code == 200, r.text
    assert r.json()["entry"]["duration_seconds"] == 5400
    r = client.patch(f"/api/entries/{entry['id']}", json={"note": "Hefte", "reason": "ergänzt"})
    assert r.status_code == 200

    revs = client.get(f"/api/entries/{entry['id']}/revisions").json()["revisions"]
    assert [r["revision_no"] for r in revs] == [1, 2]
    assert revs[0]["snapshot"]["duration_seconds"] == 3600
    assert revs[1]["change_reason"] == "ergänzt" and revs[1]["kind"] == "geaendert"


def test_patch_cannot_remove_nachtraeglich(client):
    entry = post_entry(client, recent_local(5, 9), recent_local(5, 10)).json()["entry"]
    assert "nachtraeglich" in entry["flags"]
    r = client.patch(f"/api/entries/{entry['id']}", json={"flags": []})
    assert r.status_code == 200 and "nachtraeglich" in r.json()["entry"]["flags"]


def test_delete_and_restore(client):
    entry = post_entry(client, recent_local(1, 9), recent_local(1, 10)).json()["entry"]
    r = client.delete(f"/api/entries/{entry['id']}", params={"reason": "doppelt"})
    assert r.status_code == 200 and r.json()["entry"]["deleted_at"]
    assert client.delete(f"/api/entries/{entry['id']}").status_code == 409
    assert client.get("/api/entries").json()["count"] == 0
    assert client.get("/api/entries", params={"include_deleted": True}).json()["count"] == 1
    revs = client.get(f"/api/entries/{entry['id']}/revisions").json()["revisions"]
    assert revs[-1]["kind"] == "geloescht" and revs[-1]["change_reason"] == "doppelt"

    r = client.post(f"/api/entries/{entry['id']}/restore")
    assert r.status_code == 200 and r.json()["entry"]["deleted_at"] is None


def test_locked_month_returns_423_without_reason(client):
    entry = post_entry(client, recent_local(3, 9), recent_local(3, 10)).json()["entry"]
    ym = entry["day"][:7]
    from app.services.entries import month_entries_hash

    with SessionLocal() as s:
        s.add(MonthClose(year_month=ym, closed_at=datetime.now(UTC).replace(tzinfo=None),
                         entries_hash_sha256=month_entries_hash(s, ym), soll_seconds=0, ist_seconds=0,
                         saldo_seconds=0))
        s.commit()

    r = client.patch(f"/api/entries/{entry['id']}", json={"note": "x"})
    assert r.status_code == 423
    assert r.json()["detail"] == "Monat abgeschlossen – Änderung nur mit Begründung"
    assert client.delete(f"/api/entries/{entry['id']}").status_code == 423

    r = client.patch(f"/api/entries/{entry['id']}", json={"note": "x", "reason": "Nachtrag"})
    assert r.status_code == 200
    r = client.request("DELETE", f"/api/entries/{entry['id']}", json={"reason": "doppelt"})
    assert r.status_code == 200

    with SessionLocal() as s:
        mc = s.get(MonthClose, ym)
        assert mc.status == "nachtraeglich_korrigiert"
        assert len(mc.hash_history) == 2
