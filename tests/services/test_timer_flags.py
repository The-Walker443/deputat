"""Timer start/switch with Kennzeichen (klassenfahrt, nachtbereitschaft, Vertretung only for Unterricht)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.db.models import Category
from app.services import timer as svc
from app.services.entries import EntryValidationError

BERLIN = ZoneInfo("Europe/Berlin")


def L(y, m, d, h=0, mi=0) -> datetime:  # noqa: N802
    return datetime(y, m, d, h, mi, tzinfo=BERLIN)


def cat(db, code: str) -> int:
    return db.scalar(select(Category.id).where(Category.code == code))


def test_start_with_klassenfahrt_and_nachtbereitschaft_on_any_category(db):
    st = svc.start(db, cat(db, "K"), flags=["nachtbereitschaft", "klassenfahrt"], now=L(2026, 11, 3, 9))
    assert {"klassenfahrt", "nachtbereitschaft"} <= set(st.running.flags)


def test_vertretung_flags_only_for_unterricht(db):
    st = svc.start(db, cat(db, "U"), flags=["vertretung_angeordnet"], now=L(2026, 11, 3, 8))
    assert "vertretung_angeordnet" in st.running.flags
    with pytest.raises(EntryValidationError, match="nur bei Unterricht"):
        svc.switch(db, cat(db, "K"), flags=["vertretung_genehmigt"], started_at=L(2026, 11, 3, 9),
                   now=L(2026, 11, 3, 9))
    # nothing changed: the Unterricht timer is still running
    assert svc.status(db, now=L(2026, 11, 3, 9)).running.category_id == cat(db, "U")


def test_vertretung_allowed_for_unterricht_subcategory(db):
    sub_id = db.scalar(select(Category.id).where(Category.parent_id == cat(db, "U")))
    if sub_id is None:
        pytest.skip("no subcategory of U in the seed")
    st = svc.start(db, cat(db, "U"), subcategory_id=sub_id, flags=["vertretung_genehmigt"], now=L(2026, 11, 3, 8))
    assert "vertretung_genehmigt" in st.running.flags


@pytest.mark.parametrize("flags", [["nachtraeglich"], ["ferienarbeit"], ["gibt_es_nicht"], 5])
def test_other_flags_are_rejected(db, flags):
    with pytest.raises(EntryValidationError):
        svc.start(db, cat(db, "K"), flags=flags, now=L(2026, 11, 3, 9))
    assert svc.status(db, now=L(2026, 11, 3, 9)).running is None


def test_pause_and_resume_keep_flags(db):
    svc.start(db, cat(db, "U"), flags=["klassenfahrt", "vertretung_angeordnet"], now=L(2026, 11, 3, 8))
    st = svc.pause(db, now=L(2026, 11, 3, 9))
    assert st.paused["flags"] == ["klassenfahrt", "vertretung_angeordnet"]
    st = svc.resume(db, now=L(2026, 11, 3, 9, 30))
    assert {"klassenfahrt", "vertretung_angeordnet"} <= set(st.running.flags)


def test_api_start_with_flags(client):
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        k, u = cat(s, "K"), cat(s, "U")
    r = client.post("/api/timer/start", json={"category_id": k, "flags": ["klassenfahrt"]})
    assert r.status_code == 200, r.text
    assert "klassenfahrt" in r.json()["running"]["flags"]
    r = client.post("/api/timer/switch", json={"category_id": k, "flags": ["vertretung_angeordnet"]})
    assert r.status_code == 400 and "nur bei Unterricht" in r.json()["detail"]
    r = client.post("/api/timer/switch", json={"category_id": u, "flags": ["vertretung_genehmigt"]})
    assert r.status_code == 200 and "vertretung_genehmigt" in r.json()["running"]["flags"]
    r = client.post("/api/timer/switch", json={"category_id": u, "flags": ["korrigiert"]})
    assert r.status_code == 400


def test_heute_offers_flag_toggles(client):
    html = client.get("/heute").text
    for flag in ("klassenfahrt", "nachtbereitschaft", "vertretung_angeordnet", "vertretung_genehmigt"):
        assert f'data-timer-flag="{flag}"' in html
    assert 'data-flag-unterricht' in html
    assert 'data-unterricht="1"' in html  # the Unterricht chip is marked for the client-side rule
