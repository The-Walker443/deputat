"""Calendar master data vs. closed months (review fix): overrides, ferien, ICS and leave changes that alter a
closed month need a reason (423 otherwise) and are recorded; a period with a closed month cannot be deleted."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import DayOverride, Ferien, MonthClose
from app.services import calendar_service as cal
from app.services import entries as entries_svc
from app.services import ledger

EXAMPLE_ICS = Path(__file__).resolve().parents[2] / "examples" / "ferien_bayern_BEISPIEL.ics"


@pytest.fixture
def year(db):
    period = cal.create_period(db, "2026/27")
    cal.commit_ics(db, period, EXAMPLE_ICS.read_bytes())
    db.add(MonthClose(year_month="2026-09", closed_at=datetime(2026, 10, 2, 10),
                      entries_hash_sha256=entries_svc.month_entries_hash(db, "2026-09"),
                      soll_seconds=0, ist_seconds=0, saldo_seconds=0))
    db.commit()
    return period


def _sept_soll(db, period) -> int:
    ctx = ledger.load_context(db, period, today=date(2026, 10, 20))
    return sum(r.soll for r in ctx.ledger if r.date.month == 9 and r.date.year == 2026)


def test_override_in_closed_month_needs_reason(db, year):
    soll = _sept_soll(db, year)
    with pytest.raises(cal.CalendarLocked, match="2026-09"):
        cal.set_override(db, date(2026, 9, 21), absence_type="dienstbefreiung")
    assert db.get(DayOverride, date(2026, 9, 21)) is None
    assert _sept_soll(db, year) == soll
    assert db.get(MonthClose, "2026-09").status == "abgeschlossen"


def test_override_with_reason_marks_month_corrected(db, year):
    cal.set_override(db, date(2026, 9, 21), absence_type="krank", reason="Krankmeldung nachgereicht")
    mc = db.get(MonthClose, "2026-09")
    assert mc.status == "nachtraeglich_korrigiert" and mc.corrected_at is not None
    (entry,) = mc.hash_history
    assert entry["kind"] == "kalender" and entry["reason"] == "Kalenderänderung: Krankmeldung nachgereicht"
    assert entry["hash"] == mc.entries_hash_sha256  # the entries did not change
    assert db.get(DayOverride, date(2026, 9, 21)).absence_type == "krank"


def test_open_month_changes_need_no_reason(db, year):
    cal.set_override(db, date(2026, 10, 21), absence_type="krank")
    cal.delete_override(db, date(2026, 10, 21))
    assert db.get(MonthClose, "2026-09").status == "abgeschlossen"


def test_note_only_override_is_not_a_soll_change(db, year):
    cal.set_override(db, date(2026, 9, 22), note="Elternabend")
    assert db.get(MonthClose, "2026-09").status == "abgeschlossen"


def test_range_ferien_delete_and_adopt_are_guarded(db, year):
    with pytest.raises(cal.CalendarLocked):
        cal.set_override_range(db, date(2026, 9, 21), date(2026, 9, 25), absence_type="krank")
    assert not db.scalars(select(DayOverride)).all()
    with pytest.raises(cal.CalendarLocked):
        cal.create_ferien(db, year, name="Extra", start=date(2026, 9, 28), end=date(2026, 9, 30))
    assert not db.scalars(select(Ferien).where(Ferien.name == "Extra")).all()
    sommer = next(f for f in cal.list_ferien(db, year) if f.start <= date(2026, 9, 1) <= f.end)
    with pytest.raises(cal.CalendarLocked):
        cal.delete_ferien(db, sommer.id)
    with pytest.raises(cal.CalendarLocked):
        cal.update_ferien(db, sommer.id, end=date(2026, 9, 7))
    assert db.get(Ferien, sommer.id).end == sommer.end
    # A change outside the closed month keeps working without a reason.
    cal.create_ferien(db, year, name="Brückentag", start=date(2026, 10, 30), end=date(2026, 10, 30))


def test_period_with_closed_month_cannot_be_deleted(db, year):
    with pytest.raises(cal.CalendarConflict, match="2026-09"):
        cal.delete_period(db, year.id)
    assert cal.get_period(db, year.id) is not None


def test_api_returns_423_without_reason_and_409_for_period_delete(client):
    period = client.post("/api/calendar/periods", json={"label": "2026/27"}).json()
    r = client.post("/api/entries", json={"category_id": 1, "day": "2026-09-15", "duration_seconds": 3600})
    assert r.status_code == 201
    r = client.post("/api/months/2026-09/close", json={"force": True, "note": "Test"})
    assert r.status_code == 200, r.text
    body = {"start": "2026-09-21", "end": "2026-09-25", "absence_type": "krank"}
    r = client.post("/api/calendar/overrides/range", json=body)
    assert r.status_code == 423 and "Begründung" in r.json()["detail"]
    r = client.post(f"/api/calendar/periods/{period['id']}/ferien",
                    json={"name": "X", "start": "2026-09-28", "end": "2026-09-30"})
    assert r.status_code == 423
    r = client.post("/api/calendar/overrides/range", json={**body, "reason": "AU nachgereicht"})
    assert r.status_code == 200
    assert client.get("/api/months/2026-09").json()["status"] == "nachtraeglich_korrigiert"
    assert client.delete(f"/api/calendar/periods/{period['id']}").status_code == 409
    assert client.get("/api/months/2026-09").status_code == 200
