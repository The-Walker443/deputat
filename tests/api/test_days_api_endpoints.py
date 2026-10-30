"""API tests for /api/days (day view, suggestions, confirmation, open days).

The client runs with the real clock, so the tests create the period containing today (without ferien,
every weekday except holidays is a school day) and work on recent past school days.
"""

from __future__ import annotations

from datetime import date, time, timedelta

import pytest
from sqlalchemy import select

from app.db.models import CalendarDay, Category, MonthClose
from app.db.session import SessionLocal
from app.services import entries as entries_svc
from app.util import local_to_utc, today_local


def cat_id(code: str) -> int:
    with SessionLocal() as s:
        return s.scalar(select(Category.id).where(Category.code == code))


def school_days(n: int) -> list[date]:
    """The ``n`` most recent school days up to and including today (newest first)."""
    with SessionLocal() as s:
        rows = s.scalars(
            select(CalendarDay.date).where(
                CalendarDay.date <= today_local(), CalendarDay.day_type == "schultag",
                CalendarDay.absence_type.is_(None),
            ).order_by(CalendarDay.date.desc()).limit(n)
        ).all()
    return list(rows)


@pytest.fixture
def setup(client):
    """Current period + a timetable with two lessons and a free slot on every weekday."""
    r = client.post("/api/calendar/periods", json={})
    assert r.status_code in (200, 201), r.text
    days = school_days(3)
    if len(days) < 3:
        pytest.skip("too few school days in the current period so far")
    slots = []
    for weekday in range(1, 6):
        slots += [
            {"weekday": weekday, "slot_no": 1, "klasse": "3b", "fach": "Deutsch"},
            {"weekday": weekday, "slot_no": 2, "klasse": "3b", "fach": "Mathematik"},
            {"weekday": weekday, "slot_no": 3, "kind": "frei"},
        ]
    tt = client.post("/api/timetable", json={"valid_from": (min(days) - timedelta(days=7)).isoformat(),
                                              "slots": slots})
    assert tt.status_code == 201, tt.text
    return {"days": days, "timetable": tt.json()}


def slot_id(setup, d: date, slot_no: int) -> str:
    return next(s["id"] for s in setup["timetable"]["slots"]
                if s["weekday"] == d.isoweekday() and s["slot_no"] == slot_no)


def test_generate_and_day_view(client, setup):
    d = setup["days"][1]
    r = client.post(f"/api/days/{d}/suggestions")
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["created"]) == 2 and data["skipped_reason"] is None
    assert all(e["status"] == "vorschlag" for e in data["created"])
    assert client.post(f"/api/days/{d}/suggestions").json()["created"] == []

    view = client.get(f"/api/days/{d}").json()
    assert view["day_type"] == "schultag" and view["soll_hm"] == "8:00" and view["ist"] == 0
    assert len(view["vorschlaege"]) == 2 and view["entries"] == []
    assert [s["slot_no"] for s in view["slots"]] == [1, 2, 3]
    assert view["confirmed"] is False and view["can_confirm"] is True
    assert view["timetable"]["id"] == setup["timetable"]["id"]


def test_confirm_tag_wie_geplant(client, setup):
    d = setup["days"][1]
    r = client.post(f"/api/days/{d}/confirm", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["first_confirmation"] is True and len(body["result"]["active"]) == 2
    assert body["day"]["confirmed"] is True and body["day"]["ist"] == 2 * 45 * 60
    assert body["day"]["vorschlaege"] == []
    entry_id = body["result"]["active"][0]["id"]
    revs = client.get(f"/api/entries/{entry_id}/revisions").json()["revisions"]
    assert len(revs) == 1 and revs[0]["snapshot"]["status"] == "vorschlag"
    # empty body works too
    assert client.post(f"/api/days/{d}/confirm").status_code == 200


def test_confirm_with_deviations_and_extra(client, setup):
    d = setup["days"][1]
    r = client.post(f"/api/days/{d}/confirm", json={
        "deviations": [
            {"slot_id": slot_id(setup, d, 1), "type": "entfallen"},
            {"slot_id": slot_id(setup, d, 2), "type": "vertretung", "klasse": "4a", "art": "genehmigt"},
            {"slot_id": slot_id(setup, d, 3), "type": "vertretung", "klasse": "2b"},
        ],
        "extra": [{"start": "11:35", "end": "12:20", "klasse": "1a"}],
    })
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert len(result["verworfen"]) == 1
    flags = sorted(f for e in result["active"] for f in e["flags"])
    assert flags == ["vertretung_angeordnet", "vertretung_genehmigt"]
    assert result["extra"][0]["flags"] == ["vertretung_angeordnet"]
    view = r.json()["day"]
    assert view["ist"] == 3 * 45 * 60 and len(view["verworfen"]) == 1
    items = view["confirmation"]["deviations"]["items"]
    assert items[slot_id(setup, d, 2)]["deviation"]["art"] == "genehmigt"


def test_zeit_angepasst_and_revision(client, setup):
    d = setup["days"][1]
    client.post(f"/api/days/{d}/suggestions")
    r = client.post(f"/api/days/{d}/confirm", json={"deviations": [
        {"slot_id": slot_id(setup, d, 2), "type": "zeit_angepasst", "start": "08:45", "end": "09:45"},
    ]})
    assert r.status_code == 200, r.text
    entry = next(e for e in r.json()["result"]["active"] if e["timetable_slot_id"] == slot_id(setup, d, 2))
    assert entry["duration_seconds"] == 3600
    revs = client.get(f"/api/entries/{entry['id']}/revisions").json()["revisions"]
    assert revs[-1]["snapshot"]["duration_seconds"] == 2700


def test_confirm_errors(client, setup):
    d = setup["days"][1]
    future = today_local() + timedelta(days=7)
    r = client.post(f"/api/days/{future}/confirm", json={})
    assert r.status_code == 422 and "Zukünftige" in r.json()["detail"]
    r = client.post(f"/api/days/{d}/confirm", json={"deviations": [{"slot_id": "x", "type": "entfallen"}]})
    assert r.status_code == 422
    r = client.post(f"/api/days/{d}/confirm", json={"deviations": [
        {"slot_id": slot_id(setup, d, 1), "type": "zeit_angepasst", "start": "8 Uhr", "end": "9 Uhr"}]})
    assert r.status_code == 422 and "HH:MM" in r.json()["detail"]
    r = client.post(f"/api/days/{d}/confirm", json={"deviations": [{"type": "entfallen", "foo": 1}]})
    assert r.status_code == 422
    assert client.get(f"/api/days/{d}").json()["confirmed"] is False


def test_confirm_conflict_with_manual_entry_is_409(client, setup):
    d = setup["days"][1]
    client.post(f"/api/days/{d}/suggestions")
    with SessionLocal() as s:
        entries_svc.create_entry(s, {"start_at": local_to_utc(d, time(8, 15)), "end_at": local_to_utc(d, time(8, 30)),
                                     "category_id": cat_id("KV")}, source="manuell")
    view = client.get(f"/api/days/{d}").json()
    assert [c["start"] for c in view["conflicts"]] == ["08:00"]
    r = client.post(f"/api/days/{d}/confirm", json={})
    assert r.status_code == 409 and "1. Stunde" in r.json()["detail"]
    r = client.post(f"/api/days/{d}/confirm", json={"deviations": [
        {"entry_id": view["conflicts"][0]["entry_id"], "type": "entfallen"}]})
    assert r.status_code == 200, r.text


def test_closed_month_needs_reason(client, setup):
    d = setup["days"][1]
    with SessionLocal() as s:
        ym = f"{d.year:04d}-{d.month:02d}"
        s.add(MonthClose(year_month=ym, entries_hash_sha256=entries_svc.month_entries_hash(s, ym),
                         soll_seconds=0, ist_seconds=0, saldo_seconds=0))
        s.commit()
    assert client.post(f"/api/days/{d}/suggestions").json()["skipped_reason"] == "Der Monat ist abgeschlossen."
    r = client.post(f"/api/days/{d}/confirm", json={})
    assert r.status_code == 423
    r = client.post(f"/api/days/{d}/confirm", json={"reason": "Nachtrag"})
    assert r.status_code == 200 and len(r.json()["result"]["active"]) == 2


def test_open_days(client, setup):
    newest, middle, oldest = setup["days"]
    client.post(f"/api/days/{middle}/confirm", json={})
    r = client.get("/api/days/open")
    assert r.status_code == 200, r.text
    data = r.json()
    dates = {x["date"] for x in data["days"]}
    assert newest.isoformat() in dates and oldest.isoformat() in dates
    assert middle.isoformat() not in dates
    entry = next(x for x in data["days"] if x["date"] == oldest.isoformat())
    assert entry["unbestaetigt"] and entry["ohne_eintraege"]
    assert data["count"] == len(data["days"]) and data["period"]["label"]
    r = client.get("/api/days/open", params={"until": oldest.isoformat()})
    assert r.json()["until"] == oldest.isoformat()
    assert max(x["date"] for x in r.json()["days"]) == oldest.isoformat()


def test_open_days_without_period_is_404(client):
    r = client.get("/api/days/open")
    assert r.status_code == 404 and "Schuljahr" in r.json()["detail"]


def test_day_view_outside_period_and_bad_date(client):
    r = client.get("/api/days/2001-01-01")
    assert r.status_code == 200 and r.json()["can_confirm"] is False
    assert "kein Schuljahr" in r.json()["suggestions_hinweis"]
    assert client.get("/api/days/kein-datum").status_code == 422


def test_suggestions_on_weekend(client, setup):
    d = today_local()
    while d.isoweekday() < 6:
        d -= timedelta(days=1)
    r = client.post(f"/api/days/{d}/suggestions")
    assert r.status_code == 200 and r.json()["created"] == [] and "Wochenende" in r.json()["skipped_reason"]
