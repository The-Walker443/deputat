"""API tests for /api/timetable (versions, slots, copy)."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import Category
from app.db.session import SessionLocal


def cat_id(code: str) -> int:
    with SessionLocal() as s:
        return s.scalar(select(Category.id).where(Category.code == code))


def create(client, **body):
    body.setdefault("valid_from", "2026-09-15")
    return client.post("/api/timetable", json=body)


def test_create_list_get_version(client):
    r = create(client, label="1. Halbjahr", valid_to="2027-02-12", slots=[
        {"weekday": 1, "slot_no": 1, "klasse": "3b", "fach": "Deutsch"},
        {"weekday": 1, "slot_no": 21, "kind": "aufsicht", "start_time": "09:30", "end_time": "09:50"},
    ])
    assert r.status_code == 201, r.text
    tt = r.json()
    assert tt["label"] == "1. Halbjahr" and tt["valid_to"] == "2027-02-12" and tt["slot_count"] == 2
    first = tt["slots"][0]
    assert (first["start_time"], first["end_time"], first["category_code"]) == ("08:00", "08:45", "U")
    assert tt["slots"][1]["category_code"] == "AV"

    listing = client.get("/api/timetable").json()
    assert [t["id"] for t in listing["timetables"]] == [tt["id"]]
    assert "slots" not in listing["timetables"][0]
    assert listing["zeitraster"][0]["slot_no"] == 1
    assert {k["value"] for k in listing["kinds"]} >= {"unterricht", "frei"}

    r = client.get(f"/api/timetable/{tt['id']}")
    assert r.status_code == 200 and len(r.json()["slots"]) == 2
    assert client.get("/api/timetable/unknown").status_code == 404


def test_current_and_for_date(client):
    tt = create(client, valid_from="2000-01-01").json()
    assert client.get("/api/timetable/current").json()["id"] == tt["id"]
    assert client.get("/api/timetable").json()["current_id"] == tt["id"]
    assert client.get("/api/timetable/for-date/1999-12-31").json()["timetable"] is None
    assert client.get("/api/timetable/for-date/2000-01-03").json()["timetable"]["id"] == tt["id"]


def test_current_without_version_is_404(client):
    r = client.get("/api/timetable/current")
    assert r.status_code == 404 and "Stundenplan" in r.json()["detail"]


def test_overlap_and_validation_errors(client):
    create(client)
    r = create(client, valid_from="2027-02-15")
    assert r.status_code == 409 and "überschneidet" in r.json()["detail"]
    r = create(client, valid_from="2027-02-15", close_previous=True)
    assert r.status_code == 201
    r = create(client, valid_from="2030-01-01", close_previous=True, slots=[{"weekday": 7, "slot_no": 1}])
    assert r.status_code == 422 and "Wochentag" in r.json()["detail"]
    last = client.get("/api/timetable").json()["timetables"][-1]
    assert last["valid_from"] == "2027-02-15" and last["valid_to"] is None  # rolled back, not closed
    r = create(client, valid_from="2031-01-01", bogus=1)
    assert r.status_code == 422  # pydantic: extra field


def test_patch_and_delete_version(client):
    tt = create(client).json()
    r = client.patch(f"/api/timetable/{tt['id']}", json={"label": "Neu", "valid_to": "2027-02-12"})
    assert r.status_code == 200 and r.json()["label"] == "Neu" and r.json()["valid_to"] == "2027-02-12"
    r = client.patch(f"/api/timetable/{tt['id']}", json={"valid_to": "2026-01-01"})
    assert r.status_code == 422
    assert client.delete(f"/api/timetable/{tt['id']}").status_code == 204
    assert client.get(f"/api/timetable/{tt['id']}").status_code == 404
    assert client.delete(f"/api/timetable/{tt['id']}").status_code == 404


def test_copy_version(client):
    tt = create(client, slots=[{"weekday": 2, "slot_no": 3, "klasse": "4a"}]).json()
    r = client.post(f"/api/timetable/{tt['id']}/copy", json={"valid_from": "2027-02-15", "label": "2. HJ"})
    assert r.status_code == 201, r.text
    copy = r.json()
    assert copy["label"] == "2. HJ" and copy["slots"][0]["klasse"] == "4a"
    assert copy["slots"][0]["id"] != tt["slots"][0]["id"]
    assert client.get(f"/api/timetable/{tt['id']}").json()["valid_to"] == "2027-02-14"
    r = client.post(f"/api/timetable/{tt['id']}/copy", json={"valid_from": "2027-03-01", "close_source": False})
    assert r.status_code == 409


def test_slot_crud(client):
    tt = create(client).json()
    r = client.post(f"/api/timetable/{tt['id']}/slots", json={"weekday": 3, "slot_no": 2, "klasse": "2a"})
    assert r.status_code == 201, r.text
    slot = r.json()
    assert slot["start_time"] == "08:45" and slot["weekday_name"] == "Mittwoch"

    r = client.post(f"/api/timetable/{tt['id']}/slots", json={"weekday": 3, "slot_no": 2})
    assert r.status_code == 409

    r = client.patch(f"/api/timetable/slots/{slot['id']}", json={"kind": "sprechstunde", "klasse": None})
    assert r.status_code == 200 and r.json()["category_code"] == "E" and r.json()["klasse"] is None
    r = client.patch(f"/api/timetable/slots/{slot['id']}", json={"category_id": cat_id("KO")})
    assert r.json()["category_code"] == "KO"
    r = client.patch(f"/api/timetable/slots/{slot['id']}", json={"start_time": "9 Uhr"})
    assert r.status_code == 422

    listing = client.get(f"/api/timetable/{tt['id']}/slots").json()
    assert [s["id"] for s in listing["slots"]] == [slot["id"]]
    assert client.delete(f"/api/timetable/slots/{slot['id']}").status_code == 204
    assert client.get(f"/api/timetable/{tt['id']}/slots").json()["slots"] == []
    assert client.patch("/api/timetable/slots/nope", json={"slot_no": 1}).status_code == 404
    assert client.get("/api/timetable/nope/slots").status_code == 404


def test_replace_slots(client):
    tt = create(client, slots=[{"weekday": 1, "slot_no": 1}, {"weekday": 1, "slot_no": 2}]).json()
    keep = next(s["id"] for s in tt["slots"] if s["slot_no"] == 1)
    r = client.put(f"/api/timetable/{tt['id']}/slots", json={"slots": [
        {"weekday": 1, "slot_no": 1, "klasse": "1a"},
        {"weekday": 5, "slot_no": 6, "kind": "konferenz_fix"},
    ]})
    assert r.status_code == 200, r.text
    slots = r.json()["slots"]
    assert [(s["weekday"], s["slot_no"]) for s in slots] == [(1, 1), (5, 6)]
    assert slots[0]["id"] == keep and slots[0]["klasse"] == "1a"
    r = client.put(f"/api/timetable/{tt['id']}/slots", json={"slots": [
        {"weekday": 1, "slot_no": 1}, {"weekday": 1, "slot_no": 9, "start_time": "08:15", "end_time": "08:30"},
    ]})
    assert r.status_code == 409
    assert len(client.get(f"/api/timetable/{tt['id']}/slots").json()["slots"]) == 2  # unchanged


def test_csrf_is_required_when_enforced(client, monkeypatch):
    from app.config import reset_config

    monkeypatch.setenv("CSRF_ENFORCE", "true")
    reset_config()
    try:
        r = create(client)
        assert r.status_code == 403
    finally:
        monkeypatch.setenv("CSRF_ENFORCE", "false")
        reset_config()
