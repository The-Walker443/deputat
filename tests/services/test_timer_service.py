"""Service tests for app/services/timer.py (Konzept §6.3 Pausen, §8.1, §8.4, T13)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.db.models import Category, Settings, TimeEntry, TimerState
from app.services import entries as entries_svc
from app.services import timer as svc

BERLIN = ZoneInfo("Europe/Berlin")
H = 3600


def L(y, m, d, h=0, mi=0, s=0) -> datetime:  # noqa: N802
    return datetime(y, m, d, h, mi, s, tzinfo=BERLIN)


def utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None)


def cat(db, code: str) -> int:
    return db.scalar(select(Category.id).where(Category.code == code))


def active_entries(db) -> list[TimeEntry]:
    return entries_svc.list_entries(db)


# --------------------------------------------------------------------------- start / stop / status


def test_start_and_stop(db):
    st = svc.start(db, cat(db, "K"), klasse="3b", now=L(2026, 11, 3, 9))
    assert st.running is not None and st.running.end_at is None
    assert st.running.source == "timer" and st.running.klasse == "3b"
    assert st.running.start_at == utc(L(2026, 11, 3, 9))

    st = svc.status(db, now=L(2026, 11, 3, 9, 45))
    assert st.elapsed_seconds == 45 * 60 and st.warnings == [] and st.paused is None

    st = svc.stop(db, now=L(2026, 11, 3, 10, 15))
    assert st.running is None
    (done,) = st.stopped
    assert done.duration_seconds == 75 * 60
    assert svc.status(db).running is None


def test_stop_with_device_time(db):
    svc.start(db, cat(db, "K"), started_at=L(2026, 11, 3, 9), now=L(2026, 11, 3, 9, 1))
    st = svc.stop(db, stopped_at="2026-11-03T10:00:00+01:00", now=L(2026, 11, 3, 12))
    assert st.stopped[0].duration_seconds == H


def test_stop_without_running_timer_is_conflict(db):
    with pytest.raises(svc.TimerNotRunning, match="Es läuft kein Timer"):
        svc.stop(db, now=L(2026, 11, 3, 9))


def test_stop_before_start_rejected(db):
    svc.start(db, cat(db, "K"), now=L(2026, 11, 3, 9))
    with pytest.raises(entries_svc.EntryValidationError):
        svc.stop(db, stopped_at=L(2026, 11, 3, 8), now=L(2026, 11, 3, 10))
    assert svc.status(db).running is not None


def test_zero_length_timer_is_discarded(db):
    svc.start(db, cat(db, "K"), now=L(2026, 11, 3, 9))
    st = svc.stop(db, now=L(2026, 11, 3, 9))
    assert st.stopped == [] and st.running is None
    assert active_entries(db) == []


def test_future_start_rejected(db):
    with pytest.raises(entries_svc.EntryValidationError, match="Zukunft"):
        svc.start(db, cat(db, "K"), started_at=L(2026, 11, 3, 10), now=L(2026, 11, 3, 9))


def test_unknown_category_rolls_back(db):
    with pytest.raises(entries_svc.EntryValidationError):
        svc.start(db, 9999, now=L(2026, 11, 3, 9))
    assert svc.status(db).running is None


def test_long_running_warning(db):
    svc.start(db, cat(db, "K"), now=L(2026, 11, 3, 8))
    assert svc.status(db, now=L(2026, 11, 3, 14)).warnings == []
    st = svc.status(db, now=L(2026, 11, 3, 14, 0, 1))
    assert st.warnings == [svc.WARNING_LONG_RUNNING]


# --------------------------------------------------------------------------- switch semantics


def test_start_while_running_switches(db):
    svc.start(db, cat(db, "K"), now=L(2026, 11, 3, 9))
    st = svc.switch(db, cat(db, "VN"), at=L(2026, 11, 3, 9, 30), now=L(2026, 11, 3, 9, 30))
    (first,) = st.stopped
    assert first.category_id == cat(db, "K") and first.duration_seconds == 30 * 60
    assert st.running.category_id == cat(db, "VN")
    assert st.running.start_at == first.end_at

    st = svc.start(db, cat(db, "E"), now=L(2026, 11, 3, 10))
    assert st.stopped[0].category_id == cat(db, "VN")
    assert len(entries_svc.list_entries(db)) == 3
    assert entries_svc.get_running_entry(db).category_id == cat(db, "E")


def test_offline_start_earlier_than_running_is_conflict(db):
    svc.start(db, cat(db, "K"), now=L(2026, 11, 3, 10, 5))
    with pytest.raises(svc.TimerConflict, match="nach diesem Zeitpunkt"):
        svc.start(db, cat(db, "VN"), started_at=L(2026, 11, 3, 10), now=L(2026, 11, 3, 10, 30))
    st = svc.status(db)
    assert st.running.category_id == cat(db, "K")  # untouched


def test_offline_start_later_than_running_is_switch(db):
    svc.start(db, cat(db, "K"), started_at=L(2026, 11, 3, 10), now=L(2026, 11, 3, 10))
    st = svc.start(db, cat(db, "VN"), started_at=L(2026, 11, 3, 10, 20), now=L(2026, 11, 3, 11))
    assert st.stopped[0].duration_seconds == 20 * 60
    assert st.running.start_at == utc(L(2026, 11, 3, 10, 20))


def test_start_overlapping_manual_entry_rejected(db):
    entries_svc.create_entry(db, {"start_at": L(2026, 11, 3, 11), "end_at": L(2026, 11, 3, 12),
                                  "category_id": cat(db, "K")}, source="manuell", now=utc(L(2026, 11, 3, 13)))
    with pytest.raises(entries_svc.EntryConflict):
        svc.start(db, cat(db, "VN"), started_at=L(2026, 11, 3, 10), now=L(2026, 11, 3, 13))
    assert svc.status(db).running is None


# --------------------------------------------------------------------------- pause / resume


def test_pause_and_resume_keep_category_and_payload(db):
    sub = db.scalar(select(Category).where(Category.code == "K.1"))
    svc.start(db, sub.id, klasse="3b", fach="Deutsch", note="Proben", now=L(2026, 11, 3, 15))
    st = svc.pause(db, now=L(2026, 11, 3, 15, 40))
    assert st.running is None
    assert st.stopped[0].duration_seconds == 40 * 60
    assert st.paused["category_id"] == cat(db, "K")
    assert st.paused["subcategory_id"] == sub.id
    assert st.paused["klasse"] == "3b" and st.paused["note"] == "Proben"
    assert db.get(TimerState, 1).paused_category_id == cat(db, "K")

    st = svc.resume(db, now=L(2026, 11, 3, 16))
    assert st.paused is None
    assert st.running.category_id == cat(db, "K") and st.running.subcategory_id == sub.id
    assert (st.running.klasse, st.running.fach, st.running.note) == ("3b", "Deutsch", "Proben")
    assert st.running.start_at == utc(L(2026, 11, 3, 16))
    assert db.get(TimerState, 1).paused_category_id is None
    # Pause is not an entry: two entries with a gap.
    entries = entries_svc.list_entries(db)
    assert len(entries) == 2 and entries[0].end_at < entries[1].start_at


def test_resume_without_pause_and_pause_without_timer(db):
    with pytest.raises(svc.TimerNotRunning, match="keinen pausierten"):
        svc.resume(db, now=L(2026, 11, 3, 9))
    with pytest.raises(svc.TimerNotRunning):
        svc.pause(db, now=L(2026, 11, 3, 9))


def test_stop_while_paused_ends_pause(db):
    svc.start(db, cat(db, "K"), now=L(2026, 11, 3, 9))
    svc.pause(db, now=L(2026, 11, 3, 10))
    st = svc.stop(db, now=L(2026, 11, 3, 10, 30))
    assert st.paused is None and st.stopped == []
    with pytest.raises(svc.TimerNotRunning):
        svc.stop(db, now=L(2026, 11, 3, 11))


def test_start_clears_pause(db):
    svc.start(db, cat(db, "K"), now=L(2026, 11, 3, 9))
    svc.pause(db, now=L(2026, 11, 3, 10))
    st = svc.start(db, cat(db, "VN"), now=L(2026, 11, 3, 10, 5))
    assert st.paused is None


def test_resume_recomputes_default_location_unless_chosen(db):
    # Started at school time without explicit location -> schule; resumed in the evening -> zuhause.
    svc.start(db, cat(db, "K"), now=L(2026, 11, 3, 13, 30))
    svc.pause(db, now=L(2026, 11, 3, 13, 50))
    st = svc.resume(db, now=L(2026, 11, 3, 19))
    assert st.running.location == "zuhause"
    svc.stop(db, now=L(2026, 11, 3, 20))
    # Explicit location is kept.
    svc.start(db, cat(db, "K"), location="extern", now=L(2026, 11, 4, 9))
    svc.pause(db, now=L(2026, 11, 4, 10))
    st = svc.resume(db, now=L(2026, 11, 4, 19))
    assert st.running.location == "extern"


# --------------------------------------------------------------------------- default location


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (L(2026, 11, 2, 7, 0), "schule"),  # Monday 07:00 inclusive
        (L(2026, 11, 2, 13, 59), "schule"),
        (L(2026, 11, 2, 14, 0), "zuhause"),  # 14:00 exclusive
        (L(2026, 11, 2, 6, 59), "zuhause"),
        (L(2026, 11, 7, 10, 0), "zuhause"),  # Saturday
    ],
)
def test_default_location_rule(db, at, expected):
    st = svc.start(db, cat(db, "K"), started_at=at, now=at)
    assert st.running.location == expected


def test_default_location_rule_from_settings(db):
    db.get(Settings, 1).ui_default_location_rule = {"weekdays": [6], "from": "08:00", "to": "12:00",
                                                    "inside": "extern", "outside": "unterwegs"}
    db.commit()
    assert svc.default_location(db, utc(L(2026, 11, 7, 9))) == "extern"
    assert svc.default_location(db, utc(L(2026, 11, 2, 9))) == "unterwegs"


# --------------------------------------------------------------------------- midnight / DST


def test_t13_timer_over_midnight_produces_two_entries(db):
    svc.start(db, cat(db, "K"), now=L(2026, 11, 3, 22, 30))
    st = svc.status(db, now=L(2026, 11, 4, 0, 10))
    assert st.crosses_midnight and svc.WARNING_MIDNIGHT in st.warnings
    st = svc.stop(db, now=L(2026, 11, 4, 1, 15))
    a, b = st.stopped
    assert (a.day, b.day) == (date(2026, 11, 3), date(2026, 11, 4))
    assert a.end_at == b.start_at == utc(L(2026, 11, 4))
    assert a.duration_seconds + b.duration_seconds == 2 * H + 45 * 60
    assert a.source == b.source == "timer" and a.category_id == b.category_id
    for part in (a, b):
        (rev,) = entries_svc.revisions(db, part.id)
        assert rev.revision_no == 0 and rev.snapshot_json["split"]["parts"] == [a.id, b.id]


def test_forgotten_timer_over_several_days_is_auto_ended_at_midnight(db):
    """Konzept §8.1: a forgotten timer ends at 24:00 of its start day, not after 27 h."""
    svc.start(db, cat(db, "K"), now=L(2026, 11, 3, 22))
    st = svc.stop(db, now=L(2026, 11, 5, 1))
    assert [e.duration_seconds for e in st.stopped] == [2 * H]
    assert st.auto_ended and svc.WARNING_AUTO_ENDED in st.warnings
    assert [e.day for e in active_entries(db)] == [date(2026, 11, 3)]


def test_forgotten_timer_next_morning_auto_ended(db):
    svc.start(db, cat(db, "K"), now=L(2026, 11, 3, 20))
    st = svc.stop(db, now=L(2026, 11, 4, 7, 30))
    assert [e.duration_seconds for e in st.stopped] == [4 * H] and st.auto_ended


def test_stop_shortly_after_midnight_is_split_not_capped(db):
    svc.start(db, cat(db, "K"), now=L(2026, 11, 3, 22))
    st = svc.stop(db, now=L(2026, 11, 4, 3, 59))
    assert [e.duration_seconds for e in st.stopped] == [2 * H, 3 * H + 59 * 60] and not st.auto_ended


def test_switch_and_pause_cap_a_forgotten_timer(db):
    svc.start(db, cat(db, "K"), now=L(2026, 11, 3, 22))
    st = svc.switch(db, cat(db, "U"), now=L(2026, 11, 4, 8))
    assert [e.duration_seconds for e in st.stopped] == [2 * H] and st.auto_ended
    assert st.running is not None and st.running.start_at == utc(L(2026, 11, 4, 8))
    st = svc.pause(db, now=L(2026, 11, 5, 9))
    assert [e.duration_seconds for e in st.stopped] == [16 * H] and st.auto_ended


def test_timer_start_rejects_implausible_year(db):
    with pytest.raises(entries_svc.EntryValidationError, match="zwischen 2000 und 2099"):
        svc.start(db, cat(db, "K"), started_at="0001-01-01T00:00:00", now=L(2026, 11, 3, 8))


@pytest.mark.parametrize(
    ("start", "stop", "expected"),
    [
        # DST end (25-hour day): 00:30 CEST -> 04:30 CET = 5 h
        (datetime(2026, 10, 24, 22, 30, tzinfo=UTC), datetime(2026, 10, 25, 3, 30, tzinfo=UTC), [5 * H]),
        # DST start (23-hour day): 01:30 CET -> 03:30 CEST = 1 h
        (datetime(2026, 3, 29, 0, 30, tzinfo=UTC), datetime(2026, 3, 29, 1, 30, tzinfo=UTC), [1 * H]),
        # 24.10. 23:00 CEST -> 25.10. 03:00 CET, split at local midnight (22:00 UTC)
        (datetime(2026, 10, 24, 21, 0, tzinfo=UTC), datetime(2026, 10, 25, 2, 0, tzinfo=UTC), [1 * H, 4 * H]),
    ],
)
def test_dst_day_timer(db, start, stop, expected):
    svc.start(db, cat(db, "K"), now=start)
    st = svc.stop(db, now=stop)
    assert [e.duration_seconds for e in st.stopped] == expected
    assert st.stopped[-1].day == stop.astimezone(BERLIN).date()


def test_full_dst_end_day_is_one_25h_piece(db):
    # The timer service caps forgotten timers; the split itself (entries service) still allows 25 h pieces.
    svc.start(db, cat(db, "K"), now=L(2026, 10, 24, 23))
    parts = entries_svc.finish_running_entry(db, utc(L(2026, 10, 26, 0, 30)), now=L(2026, 10, 26, 0, 30))
    assert [e.duration_seconds for e in parts] == [1 * H, 25 * H, 30 * 60]


# --------------------------------------------------------------------------- offline idempotency


def test_duplicate_client_action_ids_are_ignored(db):
    k = cat(db, "K")
    st1 = svc.start(db, k, started_at=L(2026, 11, 3, 9), client_action_id="a-1", now=L(2026, 11, 3, 9, 5))
    st2 = svc.start(db, k, started_at=L(2026, 11, 3, 9), client_action_id="a-1", now=L(2026, 11, 3, 9, 6))
    assert not st1.duplicate and st2.duplicate
    assert st2.running.id == st1.running.id
    assert len(entries_svc.list_entries(db)) == 1

    svc.stop(db, stopped_at=L(2026, 11, 3, 10), client_action_id="a-2", now=L(2026, 11, 3, 10, 1))
    st = svc.stop(db, stopped_at=L(2026, 11, 3, 10), client_action_id="a-2", now=L(2026, 11, 3, 10, 2))
    assert st.duplicate and st.running is None  # no 409 for a replayed stop

    svc.start(db, k, client_action_id="a-3", now=L(2026, 11, 3, 11))
    svc.pause(db, client_action_id="a-4", now=L(2026, 11, 3, 11, 30))
    assert svc.pause(db, client_action_id="a-4", now=L(2026, 11, 3, 11, 31)).duplicate
    svc.resume(db, client_action_id="a-5", now=L(2026, 11, 3, 12))
    assert svc.resume(db, client_action_id="a-5", now=L(2026, 11, 3, 12, 1)).duplicate
    assert len(entries_svc.list_entries(db)) == 3


def test_offline_queue_replay_in_order(db):
    k, vn = cat(db, "K"), cat(db, "VN")
    # Queue recorded on the device while offline, sent later in order.
    svc.start(db, k, started_at=L(2026, 11, 3, 8), client_action_id="q1", now=L(2026, 11, 3, 12))
    svc.switch(db, vn, started_at=L(2026, 11, 3, 9), client_action_id="q2", now=L(2026, 11, 3, 12))
    svc.stop(db, stopped_at=L(2026, 11, 3, 9, 30), client_action_id="q3", now=L(2026, 11, 3, 12))
    rows = entries_svc.list_entries(db)
    assert [(e.category_id, e.duration_seconds) for e in rows] == [(k, H), (vn, 30 * 60)]
    # Replaying the whole queue (e.g. after a lost response) changes nothing.
    for cid, fn in (("q1", lambda: svc.start(db, k, started_at=L(2026, 11, 3, 8), client_action_id="q1")),
                    ("q2", lambda: svc.switch(db, vn, started_at=L(2026, 11, 3, 9), client_action_id="q2")),
                    ("q3", lambda: svc.stop(db, stopped_at=L(2026, 11, 3, 9, 30), client_action_id="q3"))):
        assert fn().duplicate, cid
    assert len(entries_svc.list_entries(db)) == 2


def test_failed_action_is_not_remembered(db):
    with pytest.raises(svc.TimerNotRunning):
        svc.stop(db, client_action_id="x-1", now=L(2026, 11, 3, 9))
    svc.start(db, cat(db, "K"), now=L(2026, 11, 3, 9))
    st = svc.stop(db, client_action_id="x-1", now=L(2026, 11, 3, 10))
    assert not st.duplicate and st.stopped


def test_processed_action_list_is_bounded(db):
    k = cat(db, "K")
    base = L(2026, 11, 3, 0)
    for i in range(svc.MAX_REMEMBERED_ACTIONS + 5):
        svc.start(db, k, client_action_id=f"id-{i}", now=base + timedelta(minutes=i + 1))
    ids = db.get(TimerState, 1).paused_payload["processed_actions"]
    assert len(ids) == svc.MAX_REMEMBERED_ACTIONS and ids[-1] == f"id-{svc.MAX_REMEMBERED_ACTIONS + 4}"


def test_nachtraeglich_for_offline_start_far_in_the_past(db):
    st = svc.start(db, cat(db, "K"), started_at=L(2026, 11, 1, 9), now=L(2026, 11, 4, 9))
    assert "nachtraeglich" in st.running.flags
