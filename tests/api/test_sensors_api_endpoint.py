"""GET /api/sensors (Konzept §16, Phase 3): Bearer token mandatory, compact values for Home Assistant."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Category, TimeEntry
from app.db.session import SessionLocal
from app.services import calendar_service as cal
from app.services import settings_service
from app.util import local_to_utc, today_local

KEYS = {"saldo_h", "heute_ist_h", "woche_ist_h", "eu_mittel_h", "eu_ampel", "timer_running", "timer_category",
        "prognose_ende_h", "updated_at"}


def make_token() -> str:
    with SessionLocal() as s:
        return settings_service.generate_api_token(s)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def add_entry(day, start: time, minutes: int, *, running: bool = False) -> None:
    with SessionLocal() as s:
        start_at = local_to_utc(day, start)
        if running:
            start_at = datetime.now(UTC).replace(tzinfo=None, microsecond=0) - timedelta(minutes=minutes)
        s.add(TimeEntry(
            start_at=start_at,
            end_at=None if running else start_at + timedelta(minutes=minutes),
            duration_seconds=0 if running else minutes * 60,
            has_bounds=True, day=day,
            category_id=s.scalar(select(Category.id).where(Category.code == "U")),
            status="active", source="manuell", flags=[],
        ))
        s.commit()


def test_404_without_configured_token(client):
    r = client.get("/api/sensors")
    assert r.status_code == 404
    r = client.get("/api/sensors", headers=auth("irgendwas"))
    assert r.status_code == 404


def test_401_with_missing_or_wrong_token(client):
    make_token()
    r = client.get("/api/sensors")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"
    assert client.get("/api/sensors", headers=auth("falsch")).status_code == 401
    assert client.get("/api/sensors", headers={"Authorization": "Basic abc"}).status_code == 401


def test_token_revoked_gives_404(client):
    token = make_token()
    assert client.get("/api/sensors", headers=auth(token)).status_code == 200
    with SessionLocal() as s:
        settings_service.clear_api_token(s)
    assert client.get("/api/sensors", headers=auth(token)).status_code == 404


def test_values_without_period(client):
    token = make_token()
    today = today_local()
    add_entry(today, time(0, 0), 90)
    r = client.get("/api/sensors", headers=auth(token))
    assert r.status_code == 200
    data = r.json()
    assert set(data) == KEYS
    assert data["saldo_h"] is None and data["prognose_ende_h"] is None and data["eu_mittel_h"] is None
    assert data["timer_running"] is False and data["timer_category"] is None
    assert data["heute_ist_h"] == 1.5
    assert data["woche_ist_h"] >= 1.5
    assert data["updated_at"].endswith("Z")
    assert r.headers["cache-control"] == "no-store"


def test_values_with_period_and_running_timer(client):
    token = make_token()
    today = today_local()
    with SessionLocal() as s:
        cal.create_period(s)
        s.commit()
    add_entry(today, time(0, 0), 30, running=True)
    r = client.get("/api/sensors", headers=auth(token))
    assert r.status_code == 200
    data = r.json()
    assert set(data) == KEYS
    assert data["timer_running"] is True
    assert data["timer_category"] == "Unterricht"
    assert data["heute_ist_h"] == pytest.approx(0.5, abs=0.05)
    assert data["woche_ist_h"] >= data["heute_ist_h"]
    assert isinstance(data["saldo_h"], float) and isinstance(data["prognose_ende_h"], float)
    assert isinstance(data["eu_mittel_h"], float)
    assert data["eu_ampel"] in {"gruen", "gelb", "rot"}


def test_sensor_values_function(db):
    from app.api.sensors import sensor_values

    now = local_to_utc(datetime(2026, 10, 14).date(), time(12))
    cal.create_period(db, "2026/27")
    db.commit()
    day = now.date()
    for d in (day - timedelta(days=2), day - timedelta(days=1), day):
        start = local_to_utc(d, time(8))
        db.add(TimeEntry(start_at=start, end_at=start + timedelta(hours=10), duration_seconds=36000, has_bounds=True,
                         day=d, category_id=db.scalar(select(Category.id).where(Category.code == "U")),
                         status="active", source="manuell", flags=[]))
    db.commit()
    values = sensor_values(db, now)
    # sensor_values uses the real "today" of ``now``; values are rounded hours
    assert values["heute_ist_h"] == 10.0
    assert values["woche_ist_h"] == 30.0
    assert values["timer_running"] is False
    assert values["updated_at"] == now.isoformat() + "Z"


def test_csrf_not_needed_and_no_cors(client):
    token = make_token()
    r = client.get("/api/sensors", headers={**auth(token), "Origin": "https://evil.example"})
    assert r.status_code == 200
    assert not any(k.lower().startswith("access-control-") for k in r.headers)
