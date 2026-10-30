"""POST/PATCH /api/entries: provenance cannot be faked; invalid list filters are rejected."""

from __future__ import annotations

import pytest

BODY = {"category_id": 1, "start_at": "2026-10-27T07:00:00", "end_at": "2026-10-27T08:00:00"}


@pytest.mark.parametrize("source", ["import", "stundenplan", "timer"])
def test_create_rejects_non_manual_source(client, source):
    r = client.post("/api/entries", json={**BODY, "source": source})
    assert r.status_code == 400
    assert client.get("/api/entries").json()["count"] == 0


def test_overlap_cannot_be_bypassed_via_source(client):
    assert client.post("/api/entries", json=BODY).status_code == 201
    assert client.post("/api/entries", json=BODY).status_code == 409
    assert client.post("/api/entries", json={**BODY, "source": "import"}).status_code == 400
    assert client.get("/api/entries").json()["count"] == 1


def test_create_rejects_non_active_status(client):
    assert client.post("/api/entries", json={**BODY, "status": "vorschlag"}).status_code == 400
    assert client.post("/api/entries", json={**BODY, "status": "active"}).status_code == 201


def test_patch_cannot_turn_entry_into_suggestion(client):
    entry = client.post("/api/entries", json=BODY).json()["entry"]
    r = client.patch(f"/api/entries/{entry['id']}", json={"status": "vorschlag"})
    assert r.status_code == 400
    assert client.get(f"/api/entries/{entry['id']}").json()["status"] == "active"


@pytest.mark.parametrize("param", [{"status": "foo"}, {"source": "foo"}])
def test_list_rejects_unknown_filter_values(client, param):
    r = client.get("/api/entries", params=param)
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)


def test_extreme_dates_are_rejected_not_500(client):
    r = client.post("/api/entries", json={"category_id": 1, "day": "9999-12-31", "duration_seconds": 60})
    assert r.status_code == 400 and "2000" in r.json()["detail"]
