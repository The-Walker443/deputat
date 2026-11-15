"""JSON API of the day types on "Erfassen": PUT /api/days/{date}/typ, typ fields in GET /api/weeks/{ref}."""

from __future__ import annotations

import re
from datetime import date

import pytest

H = 3600
MON = date(2026, 11, 9)  # KW 46, a school week in 2026/27
REF = "2026-W46"


@pytest.fixture
def year(client, no_ferien_catalog):
    assert client.post("/api/calendar/periods", json={"label": "2026/27"}).status_code == 201


def put_typ(client, d: date, **body):
    return client.put(f"/api/days/{d.isoformat()}/typ", json=body)


def entries(client, d: date) -> list[dict]:
    return client.get("/api/entries", params={"date_from": d.isoformat(), "date_to": d.isoformat()}).json()["entries"]


def test_week_carries_the_day_type(client, year):
    week = client.get(f"/api/weeks/{REF}").json()
    mon = week["days"][0]
    assert {"typ", "typ_label", "typ_style", "typ_options", "typ_changeable", "typ_fixed", "krank", "kf_cap",
            "kf_capped", "calendar_url"} <= set(mon)
    assert mon["typ"] == "unterricht" and mon["typ_label"] == "Unterricht"
    assert week["kf_cap"] == 10 * H


def test_put_typ_switches_the_tagessumme(client, year):
    res = put_typ(client, MON, typ="fortbildung", client_action_id="t-1")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["action"] == "geaendert" and body["client_action_id"] == "t-1" and body["removed_seconds"] == 0
    assert body["day"]["typ"] == "fortbildung" and len(body["days"]) == 7
    assert client.put(f"/api/days/{MON}/total", json={"value": "7,5"}).status_code == 200
    assert [(e["category_code"], e["duration_seconds"], e["tagessumme"]) for e in entries(client, MON)] == [
        ("F", 27000, True)]
    put_typ(client, MON, typ="klassenfahrt")
    (entry,) = entries(client, MON)
    assert entry["category_code"] == "SV" and "klassenfahrt" in entry["flags"]
    assert put_typ(client, MON, typ="klassenfahrt").json()["action"] == "unveraendert"
    put_typ(client, MON, typ="unterricht")
    (entry,) = entries(client, MON)
    assert entry["category_code"] == "AZ" and "klassenfahrt" not in entry["flags"]
    # the calendar shows Fortbildung as an absence, not the other types
    put_typ(client, MON, typ="fortbildung")
    override = client.get(f"/api/calendar/overrides/{MON}").json()
    assert override["absence_type"] == "fortbildung"


def test_put_typ_krank_removes_the_value(client, year):
    client.put(f"/api/days/{MON}/total", json={"value": "8"})
    res = put_typ(client, MON, typ="krank")
    assert res.status_code == 200
    body = res.json()
    assert body["removed_seconds"] == 8 * H and body["day"]["krank"] is True and body["day"]["ist"] == 0
    assert body["day"]["ist_konto"] == body["day"]["soll"] == 8 * H
    assert entries(client, MON) == []


@pytest.mark.parametrize("body", [{"typ": "urlaub"}, {"typ": ""}, {}, {"typ": "krank", "extra": 1},
                                  {"typ": "krank", "at": "gestern"}])
def test_put_typ_rejects_invalid_input(client, year, body):
    res = put_typ(client, MON, **body)
    assert res.status_code == 422, res.text
    assert isinstance(res.json()["detail"], (str, list))


def test_put_typ_krank_on_a_weekend_is_422(client, year):
    res = put_typ(client, date(2026, 11, 14), typ="krank")
    assert res.status_code == 422 and "Wochenende" in res.json()["detail"]


def test_put_typ_fixed_absence_is_409(client, year):
    assert client.put(f"/api/calendar/overrides/{MON}", json={"absence_type": "elternzeit"}).status_code == 200
    res = put_typ(client, MON, typ="fortbildung")
    assert res.status_code == 409 and "Elternzeit" in res.json()["detail"]


def test_put_typ_closed_month_is_423_without_reason(client, year):
    from app.db.models import MonthClose
    from app.db.session import SessionLocal
    from app.util import now_utc

    d = date(2026, 9, 15)
    with SessionLocal() as s:
        s.add(MonthClose(year_month="2026-09", closed_at=now_utc(), entries_hash_sha256="x", soll_seconds=0,
                         ist_seconds=0, saldo_seconds=0, hash_history=[]))
        s.commit()
    assert put_typ(client, d, typ="krank").status_code == 423
    assert put_typ(client, d, typ="klassenfahrt").status_code == 423
    res = put_typ(client, d, typ="krank", reason="Krankmeldung nachgetragen")
    assert res.status_code == 200, res.text
    assert res.json()["day"]["krank"] is True


def test_put_typ_needs_csrf(settings_env, client, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import reset_config
    from app.main import create_app

    monkeypatch.setenv("CSRF_ENFORCE", "true")
    reset_config()
    with TestClient(create_app()) as strict:
        assert strict.put(f"/api/days/{MON}/typ", json={"typ": "klassenfahrt"}).status_code == 403
        token = re.search(r'<meta name="csrf-token" content="([^"]+)">', strict.get("/erfassen").text).group(1)
        res = strict.put(f"/api/days/{MON}/typ", json={"typ": "klassenfahrt"}, headers={"X-CSRF-Token": token})
        assert res.status_code == 200 and res.json()["day"]["typ"] == "klassenfahrt"


# --------------------------------------------------------------------------- review fixes (regressions)


def test_krank_via_the_calendar_api_removes_the_tagessumme(client, year):
    client.put(f"/api/days/{MON}/total", json={"value": "8"})
    assert client.put(f"/api/calendar/overrides/{MON}", json={"absence_type": "krank"}).status_code == 200
    assert entries(client, MON) == []
    mon = client.get(f"/api/weeks/{REF}").json()["days"][0]
    assert mon["krank"] and mon["ist"] == 0 and mon["ist_konto"] == mon["soll"]  # counted once, as credited soll
    # a sick note over a range as well
    tue = date(2026, 11, 10)
    client.put(f"/api/days/{tue}/total", json={"value": "6"})
    res = client.post("/api/calendar/overrides/range",
                      json={"start": tue.isoformat(), "end": tue.isoformat(), "absence_type": "krank"})
    assert res.status_code == 200, res.text
    assert entries(client, tue) == []


def test_krank_via_the_calendar_api_in_a_closed_month_is_423_without_reason(client, year):
    from app.db.models import MonthClose
    from app.db.session import SessionLocal
    from app.util import now_utc

    d = date(2026, 9, 15)
    client.put(f"/api/days/{d}/total", json={"value": "8"})
    with SessionLocal() as s:
        s.add(MonthClose(year_month="2026-09", closed_at=now_utc(), entries_hash_sha256="x", soll_seconds=0,
                         ist_seconds=0, saldo_seconds=0, hash_history=[]))
        s.commit()
    assert client.put(f"/api/calendar/overrides/{d}", json={"absence_type": "krank"}).status_code == 423
    assert len(entries(client, d)) == 1


def test_sent_at_header_corrects_a_slow_device_clock(client, year):
    from datetime import timedelta

    from app.util import now_utc

    assert put_typ(client, MON, typ="klassenfahrt").json()["action"] == "geaendert"  # another device, right now
    slow = (now_utc() - timedelta(minutes=3)).isoformat() + "Z"  # this device's clock is 3 min slow
    res = client.put(f"/api/days/{MON.isoformat()}/typ", json={"typ": "unterricht", "at": slow},
                     headers={"X-Sent-At": slow})
    assert res.status_code == 200 and res.json()["action"] == "geaendert"  # live request, not "veraltet"
    # without the header the same request would have counted as delayed and older -> dropped
    put_typ(client, MON, typ="klassenfahrt")
    assert put_typ(client, MON, typ="unterricht", at=slow).json()["action"] == "veraltet"
