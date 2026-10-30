"""API tests for /api/timer (status codes, switch, pause/resume, offline idempotency)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import Category
from app.db.session import SessionLocal


def cat_id(code: str) -> int:
    with SessionLocal() as s:
        return s.scalar(select(Category.id).where(Category.code == code))


def ago(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).replace(microsecond=0).isoformat()


def test_status_idle(client):
    for path in ("/api/timer/status", "/api/timer"):
        r = client.get(path)
        assert r.status_code == 200
        body = r.json()
        assert body["running"] is None and body["paused"] is None and body["warnings"] == []


def test_start_status_stop(client):
    r = client.post("/api/timer/start", json={"category_id": cat_id("K"), "klasse": "3b", "started_at": ago(30)})
    assert r.status_code == 200, r.text
    running = r.json()["running"]
    assert running["running"] is True and running["klasse"] == "3b" and running["source"] == "timer"
    assert running["location"] in ("schule", "zuhause")

    st = client.get("/api/timer/status").json()
    assert st["running"]["id"] == running["id"]
    assert 29 * 60 <= st["elapsed_seconds"] <= 31 * 60

    r = client.post("/api/timer/stop")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is None
    assert body["stopped"][0]["id"] == running["id"]
    assert body["stopped"][0]["duration_seconds"] >= 29 * 60


def test_stop_without_timer_is_409(client):
    r = client.post("/api/timer/stop", json={})
    assert r.status_code == 409
    assert r.json()["detail"] == "Es läuft kein Timer."
    assert client.post("/api/timer/pause").status_code == 409
    assert client.post("/api/timer/resume").status_code == 409


def test_switch_and_conflicting_offline_start(client):
    client.post("/api/timer/start", json={"category_id": cat_id("K"), "started_at": ago(60)})
    r = client.post("/api/timer/switch", json={"category_id": cat_id("VN"), "at": ago(20)})
    assert r.status_code == 200
    body = r.json()
    assert body["stopped"][0]["category_code"] == "K"
    assert body["running"]["category_code"] == "VN"

    r = client.post("/api/timer/start", json={"category_id": cat_id("E"), "started_at": ago(40)})
    assert r.status_code == 409
    assert client.get("/api/timer").json()["running"]["category_code"] == "VN"


def test_pause_resume(client):
    client.post("/api/timer/start", json={"category_id": cat_id("K"), "fach": "Deutsch", "started_at": ago(50)})
    r = client.post("/api/timer/pause", json={"at": ago(30)})
    assert r.status_code == 200
    assert r.json()["paused"]["category_id"] == cat_id("K")
    assert r.json()["running"] is None

    r = client.post("/api/timer/resume", json={"at": ago(10)})
    assert r.status_code == 200
    body = r.json()
    assert body["paused"] is None
    assert body["running"]["category_code"] == "K" and body["running"]["fach"] == "Deutsch"


def test_idempotent_client_action_ids(client):
    payload = {"category_id": cat_id("K"), "started_at": ago(15), "client_action_id": "dev-1"}
    first = client.post("/api/timer/start", json=payload).json()
    second = client.post("/api/timer/start", json=payload).json()
    assert first["duplicate"] is False and second["duplicate"] is True
    assert second["running"]["id"] == first["running"]["id"]

    stop = {"stopped_at": ago(5), "client_action_id": "dev-2"}
    assert client.post("/api/timer/stop", json=stop).status_code == 200
    r = client.post("/api/timer/stop", json=stop)
    assert r.status_code == 200 and r.json()["duplicate"] is True
    assert client.get("/api/entries").json()["count"] == 1


def test_validation_errors(client):
    assert client.post("/api/timer/start", json={"category_id": 99999}).status_code == 400
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert client.post("/api/timer/start", json={"category_id": cat_id("K"), "started_at": future}).status_code == 400
    assert client.post("/api/timer/start", json={"category_id": cat_id("K"), "location": "mond"}).status_code == 400
    assert client.post("/api/timer/start", json={}).status_code == 422
