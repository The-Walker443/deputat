"""Web tests for the screen 'Stundenplan' (app/web/timetable.py): versions, weekly grid, slot editor."""

from __future__ import annotations

import re
from datetime import date, time

from sqlalchemy import select

from app.db.models import Category, Settings, Timetable, TimetableSlot
from app.db.session import SessionLocal


def _tt_id(label: str | None = None) -> str:
    with SessionLocal() as s:
        q = select(Timetable.id).order_by(Timetable.valid_from)
        if label:
            q = q.where(Timetable.label == label)
        return s.scalars(q).first()


def _create(client, label: str = "1. Halbjahr", valid_from: str = "2026-09-15", valid_to: str = "") -> str:
    r = client.post("/stundenplan/versionen", data={"label": label, "valid_from": valid_from, "valid_to": valid_to},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    return _tt_id(label)


def _slots(tt_id: str) -> list[TimetableSlot]:
    with SessionLocal() as s:
        rows = s.scalars(select(TimetableSlot).where(TimetableSlot.timetable_id == tt_id)
                         .order_by(TimetableSlot.weekday, TimetableSlot.slot_no)).all()
        for r in rows:
            s.expunge(r)
        return rows


def test_empty_state(client):
    r = client.get("/stundenplan")
    assert r.status_code == 200
    assert "Noch kein Stundenplan" in r.text
    assert 'action="/stundenplan/versionen"' in r.text
    assert "timetable.js" in r.text and "admin.css" in r.text


def test_create_version_and_grid(client):
    tt = _create(client)
    r = client.get("/stundenplan")
    assert r.status_code == 200
    assert "1. Halbjahr" in r.text and "Wochenraster" in r.text
    # 6 Zeitraster rows x 5 weekdays, all empty
    assert r.text.count("tt-cell is-empty") == 30
    assert f'hx-get="/stundenplan/{tt}/stunde?tag=1&amp;nr=1"' in r.text


def test_slot_editor_add_update_clear(client):
    tt = _create(client)
    r = client.get(f"/stundenplan/{tt}/stunde?tag=2&nr=3", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "<html" not in r.text
    assert "Dienstag" in r.text and 'value="09:50"' in r.text and "3b" in r.text
    r = client.post(f"/stundenplan/{tt}/stunde", data={"tag": "2", "nr": "3", "art": "unterricht", "klasse": "3b",
                                                       "fach": "Deutsch", "start": "09:50", "end": "10:35"},
                    follow_redirects=False)
    assert r.status_code == 303
    [slot] = _slots(tt)
    assert (slot.weekday, slot.slot_no, slot.kind, slot.klasse, slot.fach) == (2, 3, "unterricht", "3b", "Deutsch")
    assert (slot.start_time, slot.end_time) == (time(9, 50), time(10, 35))
    with SessionLocal() as s:
        assert s.get(Category, slot.category_id).code == "U"
    r = client.get("/stundenplan")
    assert "tt-cell-main\">3b" in r.text and "Deutsch" in r.text
    # change to a supervision with explicit category
    with SessionLocal() as s:
        av = s.scalar(select(Category.id).where(Category.code == "AV"))
    client.post(f"/stundenplan/{tt}/stunde", data={"tag": "2", "nr": "3", "art": "aufsicht", "category_id": str(av),
                                                   "start": "09:30", "end": "09:50"})
    [slot] = _slots(tt)
    assert slot.kind == "aufsicht" and slot.category_id == av and slot.start_time == time(9, 30)
    client.post(f"/stundenplan/{tt}/stunde", data={"tag": "2", "nr": "3", "aktion": "loeschen"})
    assert _slots(tt) == []


def test_slot_error_rerenders(client):
    tt = _create(client)
    client.post(f"/stundenplan/{tt}/stunde", data={"tag": "1", "nr": "1", "art": "unterricht", "klasse": "1a",
                                                   "start": "08:00", "end": "08:45"})
    r = client.post(f"/stundenplan/{tt}/stunde", data={"tag": "1", "nr": "2", "art": "unterricht", "klasse": "1a",
                                                       "start": "08:30", "end": "09:15"})
    assert r.status_code == 422
    assert "überschneiden sich" in r.text
    assert 'value="08:30"' in r.text
    r = client.post(f"/stundenplan/{tt}/stunde", data={"tag": "1", "nr": "2", "art": "unterricht", "klasse": "9z"})
    assert r.status_code == 422 and "Unbekannte Klasse" in r.text
    assert client.get(f"/stundenplan/{tt}/stunde?tag=7&nr=1").status_code == 404
    assert client.get("/stundenplan/nope/stunde?tag=1&nr=1").status_code == 404


def test_extra_row_and_raster_sync(client):
    tt = _create(client)
    client.post(f"/stundenplan/{tt}/stunde", data={"tag": "1", "nr": "0", "art": "aufsicht",
                                                   "start": "07:30", "end": "07:50"})
    client.post(f"/stundenplan/{tt}/stunde", data={"tag": "1", "nr": "1", "art": "unterricht", "klasse": "1a"})
    r = client.get("/stundenplan")
    assert "Aufsicht" in r.text
    with SessionLocal() as s:
        settings = s.get(Settings, 1)
        settings.zeitraster = [{"slot_no": 1, "start": "07:55", "end": "08:40", "label": "1. Stunde"}]
        s.commit()
    r = client.post(f"/stundenplan/versionen/{tt}/zeitraster", follow_redirects=False)
    assert r.status_code == 303
    by_no = {s.slot_no: s for s in _slots(tt)}
    assert by_no[1].start_time == time(7, 55)
    assert by_no[0].start_time == time(7, 30)  # not in the raster -> unchanged


def test_copy_update_delete_versions(client):
    tt = _create(client)
    client.post(f"/stundenplan/{tt}/stunde", data={"tag": "3", "nr": "1", "art": "unterricht", "klasse": "2a"})
    r = client.post(f"/stundenplan/versionen/{tt}/kopieren",
                    data={"label": "2. Halbjahr", "valid_from": "2027-02-15", "vorherige_beenden": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    copy = _tt_id("2. Halbjahr")
    assert len(_slots(copy)) == 1
    with SessionLocal() as s:
        assert s.get(Timetable, tt).valid_to == date(2027, 2, 14)
    r = client.get(f"/stundenplan?version={copy}")
    assert "2. Halbjahr" in r.text and "künftig" in r.text
    client.post(f"/stundenplan/versionen/{copy}", data={"label": "2. HJ", "valid_from": "2027-02-15"})
    with SessionLocal() as s:
        assert s.get(Timetable, copy).label == "2. HJ"
    r = client.post("/stundenplan/versionen", data={"label": "Kollision", "valid_from": "2026-10-01"},
                    follow_redirects=True)
    assert "überschneidet sich" in r.text
    client.post(f"/stundenplan/versionen/{copy}/loeschen")
    with SessionLocal() as s:
        assert s.get(Timetable, copy) is None
    assert client.get("/stundenplan?version=nope").status_code == 404


def test_grid_shows_selected_version_default(client):
    _create(client, "Alt", "2025-09-01", "2025-12-31")
    r = client.get("/stundenplan")
    # no version valid today -> the latest one is shown
    assert re.search(r'id="tt-title">Alt<', r.text)
