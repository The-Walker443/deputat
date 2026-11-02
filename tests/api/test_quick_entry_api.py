"""JSON API of the "Erfassen" screen: PUT /api/days/{date}/total, GET /api/weeks/{ref}."""

from __future__ import annotations

import re
from datetime import date, time as dtime, timedelta

import pytest

from app.util import local_to_utc, today_local

H = 3600


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


@pytest.fixture
def day() -> date:
    """Today on a weekday, else this week's Monday: a weekday that is never in the future."""
    today = today_local()
    return today if today.weekday() < 5 else _monday(today)


def put(client, d: date, **body):
    return client.put(f"/api/days/{d.isoformat()}/total", json=body)


@pytest.mark.parametrize(("value", "seconds"), [("7,5", 27000), ("7.5", 27000), ("7:30", 27000), ("7", 25200),
                                                ("0", 0), ("", 0)])
def test_put_value_formats(client, day, value, seconds):
    res = put(client, day, value=value, client_action_id="c-1")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["day"]["date"] == day.isoformat() and data["day"]["ist"] == seconds
    assert data["client_action_id"] == "c-1"
    assert data["ref"] == f"{day.isocalendar()[0]}-W{day.isocalendar()[1]:02d}"
    assert len(data["days"]) == 7


@pytest.mark.parametrize("body", [{"value": "abc"}, {"value": "25"}, {"value": "-2"}, {"seconds": -1},
                                  {"seconds": 24 * H + 1}, {"seconds": "acht"}, {}, {"seconds": 3600, "value": "1"},
                                  {"seconds": 3600, "extra": 1}])
def test_put_rejects_invalid_input(client, day, body):
    res = put(client, day, **body)
    assert res.status_code == 422, res.text
    assert isinstance(res.json()["detail"], str)


def test_put_seconds_overwrite_and_clear(client, day):
    assert put(client, day, seconds=8 * H).json()["action"] == "angelegt"
    res = put(client, day, seconds=9 * H)
    assert res.json()["action"] == "geaendert" and res.json()["day"]["tagessumme_seconds"] == 9 * H
    again = put(client, day, seconds=9 * H)  # idempotent replay (offline queue)
    assert again.status_code == 200 and again.json()["action"] == "unveraendert"
    entries = client.get("/api/entries", params={"date_from": day.isoformat(), "date_to": day.isoformat()}).json()
    assert [(e["category_code"], e["duration_seconds"], e["has_bounds"], e["source"]) for e in entries["entries"]] == [
        ("AZ", 9 * H, False, "manuell")]
    assert put(client, day, value="").json()["day"]["ist"] == 0
    assert client.get("/api/entries", params={"date_from": day.isoformat(),
                                              "date_to": day.isoformat()}).json()["entries"] == []


def test_put_below_detail_is_409_with_link(client, day):
    from app.db.models import Category
    from app.db.session import SessionLocal
    from app.services import entries as entries_svc

    with SessionLocal() as s:
        cid = s.query(Category).filter_by(code="U", parent_id=None).one().id
        a = local_to_utc(day, dtime(8))
        entries_svc.create_entry(s, {"start_at": a, "end_at": a + timedelta(hours=5), "category_id": cid},
                                 source="manuell")
    res = put(client, day, value="4")
    assert res.status_code == 409
    body = res.json()
    assert body["detail"] == "Für diesen Tag sind bereits 5:00 h einzeln erfasst – bitte mindestens 5:00 eingeben."
    assert body["detail_seconds"] == 5 * H and body["min_seconds"] == 5 * H
    assert body["entries_url"] == f"/eintraege?von={day}&bis={day}"
    ok = put(client, day, value="6,5").json()
    assert ok["day"]["ist"] == 6.5 * H and ok["day"]["tagessumme_seconds"] == 1.5 * H
    assert ok["day"]["has_detail_entries"] is True


def test_put_closed_month_is_423_without_reason(client):
    from app.db.models import MonthClose
    from app.db.session import SessionLocal
    from app.util import now_utc

    d = date(2026, 9, 15)
    with SessionLocal() as s:
        s.add(MonthClose(year_month="2026-09", closed_at=now_utc(), entries_hash_sha256="x", soll_seconds=0,
                         ist_seconds=0, saldo_seconds=0, hash_history=[]))
        s.commit()
    assert put(client, d, value="5").status_code == 423
    week = client.get("/api/weeks/2026-W38").json()
    assert week["days"][1]["locked"] is True
    res = put(client, d, value="5", reason="Nachtrag aus Kalender")
    assert res.status_code == 200 and res.json()["day"]["nachtraeglich"] is True


def test_get_week(client):
    res = client.get("/api/weeks/2026-W45")
    assert res.status_code == 200
    week = res.json()
    assert week["label"] == "KW 45 · 02.–08.11.2026"
    assert [d["date"] for d in week["days"]][0] == "2026-11-02"
    keys = {"ist", "soll", "day_type", "absence", "locked", "has_detail_entries", "tagessumme_seconds", "badges"}
    assert keys <= set(week["days"][0])
    assert client.get("/api/weeks/2026-W99").status_code == 404
    assert client.get("/api/weeks/kw45").status_code == 404


def test_put_needs_csrf(settings_env, client, monkeypatch, day):
    from fastapi.testclient import TestClient

    from app.config import reset_config
    from app.main import create_app

    monkeypatch.setenv("CSRF_ENFORCE", "true")
    reset_config()
    with TestClient(create_app()) as strict:
        assert strict.put(f"/api/days/{day}/total", json={"value": "3"}).status_code == 403
        token = re.search(r'<meta name="csrf-token" content="([^"]+)">', strict.get("/erfassen").text).group(1)
        res = strict.put(f"/api/days/{day}/total", json={"value": "3"}, headers={"X-CSRF-Token": token})
        assert res.status_code == 200 and res.json()["day"]["ist"] == 3 * H


@pytest.mark.parametrize(("value", "seconds"), [("7,30", 27000), ("7.30", 27000), ("7 30", 27000), ("7,25", 26100)])
def test_put_two_digit_minutes(client, day, value, seconds):
    res = put(client, day, value=value)
    assert res.status_code == 200, res.text
    assert res.json()["day"]["ist"] == seconds


def test_put_hhmm_hint(client, day):
    res = put(client, day, value="730")
    assert res.status_code == 422 and "Meintest du 7:30?" in res.json()["detail"]


def test_put_with_device_time(client, day):
    from app.util import now_utc

    at = (now_utc() - timedelta(minutes=1)).isoformat() + "Z"
    res = put(client, day, seconds=2 * H, at=at, client_action_id="x")
    assert res.status_code == 200, res.text
    assert res.json()["action"] == "angelegt" and res.json()["day"]["ist"] == 2 * H
    future = (now_utc() + timedelta(hours=2)).isoformat() + "Z"
    res = put(client, day, seconds=3 * H, at=future)
    assert res.status_code == 422 and "Zukunft" in res.json()["detail"]
    assert put(client, day, seconds=3 * H, at="gestern").status_code == 422


def test_put_respects_the_dst_day_length(client):
    spring = date(2026, 3, 29)  # 23-hour day
    res = put(client, spring, value="24")
    assert res.status_code == 422 and "23 Stunden" in res.json()["detail"]
    assert put(client, spring, seconds=24 * H).status_code == 422
    autumn = date(2026, 10, 25)  # 25-hour day: 24 h fit (a single entry holds at most 24 h)
    assert put(client, autumn, value="24").status_code == 200
