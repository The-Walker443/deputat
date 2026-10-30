"""services/reminders.py: in-app hints (Konzept §3.2 Nr. 15)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import select

from app.db.models import BackupLog, Category, MonthClose, TimeEntry
from app.services import calendar_service as cal
from app.services import reminders
from app.services import timetable as tts
from app.util import local_to_utc

WED = date(2026, 10, 14)
NOW = local_to_utc(WED, time(12, 0))  # Wednesday noon (local)
LONG_AGO = datetime(2026, 8, 1, 8, 0)


def cat(db, code: str) -> int:
    return db.scalar(select(Category.id).where(Category.code == code))


def add(db, day: date, hours: float = 1.0, *, created_at: datetime = LONG_AGO, running: bool = False,
        start: datetime | None = None) -> TimeEntry:
    start_at = start or local_to_utc(day, time(8))
    entry = TimeEntry(
        start_at=start_at,
        end_at=None if running else start_at + timedelta(hours=hours),
        duration_seconds=0 if running else int(hours * 3600),
        has_bounds=True, day=day, category_id=cat(db, "U"), status="active", source="manuell", flags=[],
        created_at=created_at,
    )
    db.add(entry)
    db.commit()
    return entry


def keys(hints) -> list[str]:
    return [h["key"].split(":")[0] for h in hints]


def by_prefix(hints, prefix: str):
    return next((h for h in hints if h["key"].startswith(prefix)), None)


@pytest.fixture
def year(db):
    period = cal.create_period(db, "2026/27")
    db.commit()
    return period


@pytest.fixture
def backed_up(db):
    db.add(BackupLog(created_at=NOW - timedelta(hours=10), file="x.sqlite.gz", ok=True, kind="auto"))
    db.commit()


def test_no_period(db):
    hints = reminders.collect(db, NOW)
    assert keys(hints) == ["keine-periode"]
    assert hints[0]["link"] == "/kalender/einrichtung"
    assert set(hints[0]) == {"key", "level", "text", "link", "link_label"}


def test_quiet_when_everything_is_fine(db, year, backed_up):
    add(db, WED - timedelta(days=1))
    assert reminders.collect(db, NOW) == []


def test_timer_running_long(db, year, backed_up):
    add(db, WED - timedelta(days=1))
    entry = add(db, WED, running=True, start=NOW - timedelta(hours=7))
    hint = by_prefix(reminders.collect(db, NOW), "timer-lang")
    assert hint and hint["level"] == "warn" and hint["link"] == "/heute"
    assert entry.id in hint["key"] and "7:00 h" in hint["text"] and "Unterricht" in hint["text"]
    assert by_prefix(reminders.collect(db, NOW - timedelta(hours=2)), "timer-lang") is None


def test_school_day_without_entries(db, year, backed_up):
    hint = by_prefix(reminders.collect(db, NOW), "tag-ohne-eintrag")
    assert hint["key"] == "tag-ohne-eintrag:2026-10-13"
    assert "gestern" in hint["text"]
    assert hint["link"] == "/eintraege/neu?datum=2026-10-13"
    # Monday: reminds of Friday
    monday = local_to_utc(date(2026, 10, 19), time(9))
    add(db, date(2026, 10, 13))
    hint = by_prefix(reminders.collect(db, monday), "tag-ohne-eintrag")
    assert hint["key"] == "tag-ohne-eintrag:2026-10-16" and "16.10.2026" in hint["text"]
    # full-day absence (sick) -> nothing to record
    cal.set_override(db, date(2026, 10, 16), absence_type="krank")
    db.commit()
    assert by_prefix(reminders.collect(db, monday), "tag-ohne-eintrag") is None


def test_unconfirmed_timetable_days(db, year, backed_up):
    tts.create_timetable(db, valid_from=date(2026, 10, 5), slots=[
        {"weekday": wd, "slot_no": 1, "klasse": "3b"} for wd in range(1, 6)
    ])
    add(db, WED - timedelta(days=1))
    hint = by_prefix(reminders.collect(db, NOW), "unbestaetigt")
    assert hint is not None
    assert "7 Schultage" in hint["text"]  # 05.–09.10. and 12./13.10.
    assert hint["link"] == "/tage"  # list of open days with per-day confirmation


def test_month_close_due_after_the_5th(db, year, backed_up):
    add(db, date(2026, 9, 15))
    oct3 = local_to_utc(date(2026, 10, 5), time(12))
    assert by_prefix(reminders.collect(db, oct3), "monatsabschluss") is None
    hint = by_prefix(reminders.collect(db, NOW), "monatsabschluss")
    assert hint["key"] == "monatsabschluss:2026-09"
    assert hint["level"] == "warn" and hint["link"] == "/monate/2026-09"
    assert "September 2026" in hint["text"]
    # an older open month is always due
    add(db, date(2026, 8, 10))
    hint = by_prefix(reminders.collect(db, oct3), "monatsabschluss")
    assert hint["key"] == "monatsabschluss:2026-08"
    db.add_all([
        MonthClose(year_month=ym, entries_hash_sha256="0" * 64, soll_seconds=0, ist_seconds=0, saldo_seconds=0)
        for ym in ("2026-08", "2026-09")
    ])
    db.commit()
    assert by_prefix(reminders.collect(db, NOW), "monatsabschluss") is None


def test_backup_reminders(db, year):
    add(db, WED - timedelta(days=1))
    hint = by_prefix(reminders.collect(db, NOW), "backup-keins")
    assert hint and hint["level"] == "warn" and hint["link"] == "/export"

    db.add(BackupLog(created_at=NOW - timedelta(days=3), file="a.gz", ok=True, kind="auto"))
    db.commit()
    hint = by_prefix(reminders.collect(db, NOW), "backup-alt")
    assert hint and "älter als 2 Tage" in hint["text"]

    db.add(BackupLog(created_at=NOW - timedelta(hours=2), file="b.gz", ok=False, kind="auto", message="Platte voll"))
    db.commit()
    hints = reminders.collect(db, NOW)
    assert hints[0]["key"].startswith("backup-fehler") and hints[0]["level"] == "danger"
    assert "Platte voll" in hints[0]["text"]

    db.add(BackupLog(created_at=NOW - timedelta(hours=1), file="c.gz", ok=True, kind="manuell"))
    db.commit()
    assert not [h for h in reminders.collect(db, NOW) if h["key"].startswith("backup")]


def test_fresh_install_has_no_backup_nag(db, year):
    add(db, WED - timedelta(days=1), created_at=NOW - timedelta(hours=5))
    assert by_prefix(reminders.collect(db, NOW), "backup") is None


def test_failing_rule_is_skipped(db, year, backed_up, monkeypatch):
    def boom(*_args):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(reminders, "RULES", (boom, reminders._rule_missing_day))
    hints = reminders.collect(db, NOW)
    assert keys(hints) == ["tag-ohne-eintrag"]
