"""/api/calendar/*: periods, ferien, ICS preview/commit, holidays, days, overrides, leave plan."""

from __future__ import annotations

from pathlib import Path

import pytest

EXAMPLE_ICS = (Path(__file__).resolve().parents[2] / "examples" / "ferien_bayern_BEISPIEL.ics").read_text("utf-8")


@pytest.fixture
def period(client) -> dict:
    res = client.post("/api/calendar/periods", json={"label": "2026/27"})
    assert res.status_code == 201, res.text
    return res.json()


@pytest.fixture
def year(client, period) -> dict:
    res = client.post(f"/api/calendar/periods/{period['id']}/ics/commit", json={"content": EXAMPLE_ICS})
    assert res.status_code == 200, res.text
    return period


def test_no_period_hint(client):
    assert client.get("/api/calendar/periods").json() == []
    res = client.get("/api/calendar/periods/current")
    assert res.status_code == 404
    assert "Bitte zuerst ein Schuljahr anlegen" in res.json()["detail"]
    assert "label" in client.get("/api/calendar/periods/suggestion").json()


def test_period_crud(client, period):
    assert period["start"] == "2026-08-01" and period["end"] == "2027-07-31" and period["tage"] == 365
    assert client.get(f"/api/calendar/periods/{period['id']}").json()["label"] == "2026/27"
    assert [p["id"] for p in client.get("/api/calendar/periods").json()] == [period["id"]]

    res = client.post("/api/calendar/periods", json={"label": "2026/27"})
    assert res.status_code == 422 and "überschneidet" in res.json()["detail"]
    assert client.post("/api/calendar/periods", json={"label": "Quatsch"}).status_code == 422
    assert client.post("/api/calendar/periods", json={"label": "2028/29", "foo": 1}).status_code == 422

    custom = client.post("/api/calendar/periods", json={"start": "2030-01-01", "end": "2030-12-31", "kind": "kalenderjahr"})
    assert custom.status_code == 201 and custom.json()["label"] == "2030"

    assert client.delete(f"/api/calendar/periods/{custom.json()['id']}").status_code == 204
    assert client.get(f"/api/calendar/periods/{custom.json()['id']}").status_code == 404


def test_days_and_holidays(client, year):
    pid = year["id"]
    days = client.get(f"/api/calendar/periods/{pid}/days", params={"start": "2026-11-16", "end": "2026-11-22"}).json()
    assert [d["date"] for d in days][0] == "2026-11-16" and len(days) == 7
    bb = days[2]
    assert (bb["day_type"], bb["holiday_name"], bb["soll_seconds"], bb["soll_hm"]) == (
        "unterrichtsfrei", "Buß- und Bettag", 28800, "8:00"
    )
    holidays = client.get(f"/api/calendar/periods/{pid}/holidays").json()
    names = {h["name"] for h in holidays}
    assert {"Mariä Himmelfahrt", "Karfreitag", "Buß- und Bettag"} <= names
    assert all(h["source"] == "berechnet" for h in holidays)

    res = client.post(f"/api/calendar/periods/{pid}/rematerialize")
    assert res.json() == {"period_id": pid, "tage": 365, "urlaubstage": 30}
    assert client.get(f"/api/calendar/periods/{pid}/days", params={"start": "2026-11-16", "end": "2026-11-22"}).json() == days


def test_ics_preview_and_commit(client, period):
    pid = period["id"]
    res = client.post(f"/api/calendar/periods/{pid}/ics/preview", json={"content": EXAMPLE_ICS})
    assert res.status_code == 200
    body = res.json()
    assert body["importierbar"] == 6
    assert {i["status"] for i in body["items"]} == {"neu", "feiertag", "ausserhalb"}
    assert client.get(f"/api/calendar/periods/{pid}/ferien").json() == []

    res = client.post(
        f"/api/calendar/periods/{pid}/ics/commit", json={"content": EXAMPLE_ICS, "selected": [0, 7], "quelle_url": "x"}
    )
    body = res.json()
    assert [f["name"] for f in body["created"]] == ["BEISPIEL – Sommerferien 2026"]
    assert body["created"][0]["source"] == "ics" and body["created"][0]["werktage"] == 31
    assert body["skipped"][0]["index"] == 7

    bad = client.post(f"/api/calendar/periods/{pid}/ics/preview", json={"content": "nix"})
    assert bad.status_code == 422 and "ICS" in bad.json()["detail"]
    assert client.post(f"/api/calendar/periods/{pid}/ics/preview", json={}).status_code == 422


def test_ferien_crud(client, period):
    pid = period["id"]
    res = client.post(
        f"/api/calendar/periods/{pid}/ferien", json={"name": "Herbstferien", "start": "2026-11-02", "end": "2026-11-06"}
    )
    assert res.status_code == 201
    f = res.json()
    assert (f["kind"], f["tage"], f["werktage"], f["source"]) == ("herbst", 5, 5, "manuell")

    res = client.patch(f"/api/calendar/ferien/{f['id']}", json={"end": "2026-11-04", "kind": "sonstige"})
    assert res.status_code == 200 and res.json()["end"] == "2026-11-04" and res.json()["kind"] == "sonstige"
    day = client.get(f"/api/calendar/periods/{pid}/days", params={"start": "2026-11-05", "end": "2026-11-05"}).json()[0]
    assert day["day_type"] == "schultag"

    bad = client.post(
        f"/api/calendar/periods/{pid}/ferien", json={"name": "X", "start": "2026-11-03", "end": "2026-11-02"}
    )
    assert bad.status_code == 422 and "vor dem Beginn" in bad.json()["detail"]
    assert client.post(f"/api/calendar/periods/{pid}/ferien", json={"name": "X"}).status_code == 422
    assert client.patch("/api/calendar/ferien/nope", json={"name": "Y"}).status_code == 404

    assert client.delete(f"/api/calendar/ferien/{f['id']}").status_code == 204
    assert client.get(f"/api/calendar/periods/{pid}/ferien").json() == []
    assert client.delete(f"/api/calendar/ferien/{f['id']}").status_code == 404


def test_overrides(client, year):
    pid = year["id"]
    res = client.put("/api/calendar/overrides/2026-10-14", json={"absence_type": "dienstbefreiung", "note": "PV"})
    assert res.status_code == 200 and res.json()["absence_type"] == "dienstbefreiung"
    day = client.get(f"/api/calendar/periods/{pid}/days", params={"start": "2026-10-14", "end": "2026-10-14"}).json()[0]
    assert (day["absence_type"], day["soll_seconds"]) == ("dienstbefreiung", 0)
    assert client.get("/api/calendar/overrides/2026-10-14").json()["note"] == "PV"

    res = client.put("/api/calendar/overrides/2026-10-15", json={"day_type": "unterrichtsfrei", "note": "Päd. Tag"})
    assert res.json()["day_type"] == "unterrichtsfrei"

    rng = client.post(
        "/api/calendar/overrides/range", json={"start": "2026-10-19", "end": "2026-10-25", "absence_type": "krank"}
    )
    assert rng.status_code == 200 and len(rng.json()) == 5
    listed = client.get("/api/calendar/overrides", params={"start": "2026-10-01", "end": "2026-10-31"}).json()
    assert len(listed) == 7

    assert client.put("/api/calendar/overrides/2026-10-14", json={}).json() is None  # cleared
    assert client.get("/api/calendar/overrides/2026-10-14").status_code == 404
    assert client.delete("/api/calendar/overrides/2026-10-15").status_code == 204
    assert client.delete("/api/calendar/overrides/2026-10-15").status_code == 404


@pytest.mark.parametrize(
    ("day", "body", "status", "text"),
    [
        ("2026-10-14", {"day_type": "feiertag"}, 422, "Tagestyp"),
        ("2026-10-14", {"absence_type": "urlaub"}, 422, "Urlaubszuordnung"),
        ("2026-10-14", {"urlaub_manuell": True}, 422, "kein Urlaubstag"),
        ("2031-10-14", {"absence_type": "krank"}, 422, "Schuljahr"),
        ("2026-13-01", {"absence_type": "krank"}, 422, None),
        ("2026-10-14", {"absence_type": "krank", "extra": 1}, 422, None),
    ],
)
def test_override_validation(client, year, day, body, status, text):
    res = client.put(f"/api/calendar/overrides/{day}", json=body)
    assert res.status_code == status
    if text:
        assert text in res.json()["detail"]


def test_leave_plan_and_adopt(client, year):
    pid = year["id"]
    plan = client.get(f"/api/calendar/periods/{pid}/leave-plan").json()
    assert (plan["mode"], plan["assigned"], plan["is_complete"]) == ("sommerferien_zuerst", 30, True)
    assert plan["days"][0] == "2026-08-03" and plan["days"][-1] == "2026-09-11"
    assert next(f for f in plan["per_ferien"] if f["kind"] == "sommer")["urlaubstage"] == 30

    prop = client.get(f"/api/calendar/periods/{pid}/leave-plan", params={"mode": "proportional"}).json()
    assert prop["mode"] == "proportional" and prop["assigned"] == 30
    assert client.get(f"/api/calendar/periods/{pid}/leave-plan", params={"mode": "x"}).status_code == 422

    manual = client.get(f"/api/calendar/periods/{pid}/leave-plan", params={"mode": "manuell"}).json()
    assert manual["noch_zuzuordnen"] == 30

    adopted = client.post(f"/api/calendar/periods/{pid}/leave-plan/adopt", json={"mode": "proportional"}).json()
    assert adopted["mode"] == "manuell" and adopted["days"] == prop["days"]
    assert client.post(f"/api/calendar/periods/{pid}/leave-plan/adopt", json={"mode": "manuell"}).status_code == 422
