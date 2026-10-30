"""/api/settings and /api/settings/api-token."""

from __future__ import annotations

import pytest


def test_get_settings(client):
    body = client.get("/api/settings").json()
    assert body["urlaubs_modus"] == "sommerferien_zuerst"
    assert body["regionale_feiertage"] == ["mariae_himmelfahrt"]
    assert "friedensfest" in body["regionale_feiertage_optionen"]
    assert body["api_token_configured"] is False
    assert "api_token_hash" not in body and "password_hash" not in body
    assert body["teilzeitfaktor"] == 1.0


def test_patch_settings_and_rematerialize(client):
    period = client.post("/api/calendar/periods", json={"label": "2026/27"}).json()
    res = client.patch("/api/settings", json={"upz_ist": 21, "name": "Frau Muster", "geburtsdatum": "1970-03-01"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["upz_ist"] == 21 and body["name"] == "Frau Muster" and body["geburtsdatum"] == "1970-03-01"
    assert body["teilzeitfaktor"] == 0.75
    day = client.get(
        f"/api/calendar/periods/{period['id']}/days", params={"start": "2026-10-14", "end": "2026-10-14"}
    ).json()[0]
    assert day["soll_seconds"] == 6 * 3600

    assert client.patch("/api/settings", json={"geburtsdatum": None}).json()["geburtsdatum"] is None


@pytest.mark.parametrize(
    ("body", "text"),
    [
        ({"upz_ist": 30}, "UPZ"),
        ({"urlaubs_modus": "zufall"}, "Urlaubsmodus"),
        ({"regionale_feiertage": ["karneval"]}, "regionaler Feiertag"),
        ({"klassenfahrt_variante": "X"}, "Klassenfahrt"),
        ({"kontoperiode": "woche"}, "Kontoperiode"),
        ({"upz_voll": None}, "Zahl"),
        ({"password_hash": "x"}, None),
        ({"upz_ist": "viel"}, None),
    ],
)
def test_patch_settings_validation(client, body, text):
    res = client.patch("/api/settings", json=body)
    assert res.status_code == 422
    if text:
        assert text in res.json()["detail"]


def test_api_token_endpoints(client):
    assert client.get("/api/settings/api-token").json() == {"configured": False}
    res = client.post("/api/settings/api-token")
    assert res.status_code == 201
    token = res.json()["token"]
    assert len(token) >= 32 and "nur jetzt" in res.json()["hinweis"]
    assert client.get("/api/settings/api-token").json() == {"configured": True}
    assert client.get("/api/settings").json()["api_token_configured"] is True
    assert token not in client.get("/api/settings").text

    rotated = client.post("/api/settings/api-token").json()["token"]
    assert rotated != token

    assert client.delete("/api/settings/api-token").status_code == 204
    assert client.get("/api/settings/api-token").json() == {"configured": False}
