"""A running timer only occupies the time that has passed (audit: 'Tag wie geplant' blocked the timer).

Planned later entries (confirmed lessons, an Elternabend entered in advance) must not block a timer start;
the timer is capped at the next entry when it is stopped / paused / switched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.db.models import Category
from app.services import entries as entries_svc
from app.services import timer as svc

BERLIN = ZoneInfo("Europe/Berlin")


def L(y, m, d, h=0, mi=0) -> datetime:  # noqa: N802
    return datetime(y, m, d, h, mi, tzinfo=BERLIN)


def utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None)


def cat(db, code: str) -> int:
    return db.scalar(select(Category.id).where(Category.code == code))


def manual(db, start, end, code="U", now=None):
    return entries_svc.create_entry(db, {"start_at": start, "end_at": end, "category_id": cat(db, code)},
                                    source="manuell", now=now)[0]


def test_start_before_a_later_entry_is_allowed(db):
    manual(db, L(2026, 10, 20, 8, 45), L(2026, 10, 20, 9, 30), now=utc(L(2026, 10, 20, 8, 6)))
    manual(db, L(2026, 10, 20, 19), L(2026, 10, 20, 21), code="KO", now=utc(L(2026, 10, 20, 8, 6)))
    st = svc.start(db, cat(db, "VN"), started_at=L(2026, 10, 20, 8, 8), now=L(2026, 10, 20, 8, 8))
    assert st.running is not None


def test_start_inside_an_entry_is_still_rejected(db):
    manual(db, L(2026, 10, 20, 8), L(2026, 10, 20, 8, 45), now=utc(L(2026, 10, 20, 8, 6)))
    with pytest.raises(entries_svc.EntryConflict):
        svc.start(db, cat(db, "VN"), started_at=L(2026, 10, 20, 8, 8), now=L(2026, 10, 20, 8, 8))


def test_stop_is_capped_at_the_next_entry_with_a_notice(db):
    manual(db, L(2026, 10, 20, 8, 45), L(2026, 10, 20, 9, 30), now=utc(L(2026, 10, 20, 8, 6)))
    svc.start(db, cat(db, "VN"), started_at=L(2026, 10, 20, 8, 8), now=L(2026, 10, 20, 8, 8))
    st = svc.stop(db, L(2026, 10, 20, 10), now=L(2026, 10, 20, 10))
    assert st.running is None
    assert len(st.stopped) == 1
    assert st.stopped[0].end_at == utc(L(2026, 10, 20, 8, 45))
    assert any("08:45" in n for n in st.notices)


def test_switch_and_pause_are_capped_too(db):
    manual(db, L(2026, 10, 20, 8, 45), L(2026, 10, 20, 9, 30), now=utc(L(2026, 10, 20, 8, 6)))
    svc.start(db, cat(db, "VN"), started_at=L(2026, 10, 20, 8, 8), now=L(2026, 10, 20, 8, 8))
    st = svc.switch(db, cat(db, "K"), started_at=L(2026, 10, 20, 9, 30), now=L(2026, 10, 20, 9, 30))
    assert st.stopped[0].end_at == utc(L(2026, 10, 20, 8, 45))
    assert st.running is not None
    manual(db, L(2026, 10, 20, 11), L(2026, 10, 20, 12), now=utc(L(2026, 10, 20, 9, 31)))
    st = svc.pause(db, L(2026, 10, 20, 11, 30), now=L(2026, 10, 20, 11, 30))
    assert st.stopped[0].end_at == utc(L(2026, 10, 20, 11))


def test_manual_future_entry_while_timer_runs(db):
    svc.start(db, cat(db, "VN"), started_at=L(2026, 10, 20, 8, 8), now=L(2026, 10, 20, 8, 8))
    # An Elternabend entered in advance does not collide with the running timer ...
    manual(db, L(2026, 10, 20, 19), L(2026, 10, 20, 21), code="KO", now=utc(L(2026, 10, 20, 9)))
    # ... but an entry inside the time that has already passed does.
    with pytest.raises(entries_svc.EntryConflict):
        manual(db, L(2026, 10, 20, 8, 30), L(2026, 10, 20, 8, 50), code="K", now=utc(L(2026, 10, 20, 9)))


def test_api_returns_notices(client, db):
    manual(db, L(2026, 10, 20, 8, 45), L(2026, 10, 20, 9, 30), now=utc(L(2026, 10, 20, 8, 6)))
    svc.start(db, cat(db, "VN"), started_at=L(2026, 10, 20, 8, 8), now=L(2026, 10, 20, 8, 8))
    resp = client.post("/api/timer/stop", json={"stopped_at": "2026-10-20T10:00:00+01:00"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["notices"] and "08:45" in data["notices"][0]
