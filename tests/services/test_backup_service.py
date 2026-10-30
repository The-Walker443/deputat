"""services/backup.py: consistent gzip'ed SQLite snapshots, backup_log, retention, scheduler wiring."""

from __future__ import annotations

import gzip
import sqlite3
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.config import reset_config
from app.db.models import BackupLog, Settings
from app.services import backup


def _open_snapshot(path, tmp_path) -> sqlite3.Connection:
    raw = tmp_path / "restored.sqlite"
    with gzip.open(path, "rb") as fin:
        raw.write_bytes(fin.read())
    return sqlite3.connect(raw)


def test_backup_is_valid_gzip_sqlite_with_data(db, settings_env, tmp_path):
    db.get(Settings, 1).name = "Snapshot-Test"
    db.commit()
    now = datetime(2026, 10, 28, 23, 30)  # 00:30 local on 29.10.
    result = backup.create_backup(db, kind="manuell", now=now)
    assert result.ok and result.file == "2026-10-29.sqlite.gz"
    assert result.path == settings_env.backups_dir / "2026-10-29.sqlite.gz"
    assert result.size_bytes == result.path.stat().st_size > 0
    with gzip.open(result.path, "rb") as fh:
        assert fh.read(16) == b"SQLite format 3\x00"
    conn = _open_snapshot(result.path, tmp_path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT name FROM settings WHERE id = 1").fetchone()[0] == "Snapshot-Test"
        assert conn.execute("SELECT count(*) FROM category").fetchone()[0] > 10
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()
    row = db.get(BackupLog, result.log_id)
    assert row.ok and row.kind == "manuell" and row.file == result.file and row.size_bytes == result.size_bytes
    leftovers = [p.name for p in settings_env.backups_dir.iterdir() if p.name.startswith(".") or p.suffix == ".part"]
    assert leftovers == []


def test_backup_includes_uncommitted_wal_content_of_other_connections(db, settings_env, tmp_path):
    """Committed data still in the WAL file is part of the snapshot (WAL-safe)."""
    db.get(Settings, 1).schule = "WAL-Schule"
    db.commit()  # stays in the -wal file until a checkpoint
    result = backup.create_backup(db, now=datetime(2026, 10, 29, 12))
    conn = _open_snapshot(result.path, tmp_path)
    try:
        assert conn.execute("SELECT schule FROM settings").fetchone()[0] == "WAL-Schule"
    finally:
        conn.close()


def test_second_backup_same_day_gets_time_suffix(db, settings_env):
    first = backup.create_backup(db, now=datetime(2026, 10, 29, 10, 0, 0))
    second = backup.create_backup(db, now=datetime(2026, 10, 29, 10, 15, 30))
    assert first.file == "2026-10-29.sqlite.gz"
    assert second.file == "2026-10-29_111530.sqlite.gz"
    assert {f["name"] for f in backup.backup_files()} == {first.file, second.file}


def test_failed_backup_is_logged(db, settings_env, monkeypatch):
    def boom(*_a, **_k):
        raise OSError("Platte voll")

    monkeypatch.setattr(backup, "_snapshot", boom)
    result = backup.create_backup(db, now=datetime(2026, 10, 29, 10))
    assert not result.ok and "Platte voll" in result.message
    row = db.scalars(select(BackupLog).order_by(BackupLog.id.desc())).first()
    assert row is not None and not row.ok and "Platte voll" in row.message
    assert list(settings_env.backups_dir.glob("*.gz")) == []


def _touch(directory, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(b"x")


def test_retention_deletes_old_files_only(settings_env):
    d = settings_env.backups_dir
    today = date(2026, 10, 29)
    for delta in (0, 1, 14, 15, 30):
        _touch(d, f"{today - timedelta(days=delta):%Y-%m-%d}.sqlite.gz")
    _touch(d, "2026-09-01_120000.sqlite.gz")
    _touch(d, "2026-09-01_vor-wiederherstellung.sqlite.gz")
    _touch(d, "notizen.txt")  # foreign files are never touched
    deleted = backup.cleanup_backups(retention_days=14, today=today)
    assert sorted(deleted) == sorted([
        "2026-10-14.sqlite.gz", "2026-09-29.sqlite.gz", "2026-09-01_120000.sqlite.gz",
        "2026-09-01_vor-wiederherstellung.sqlite.gz",
    ])
    remaining = sorted(p.name for p in d.iterdir())
    assert remaining == ["2026-10-15.sqlite.gz", "2026-10-28.sqlite.gz", "2026-10-29.sqlite.gz", "notizen.txt"]


def test_retention_keeps_newest_and_can_be_disabled(settings_env):
    d = settings_env.backups_dir
    _touch(d, "2025-01-01.sqlite.gz")
    _touch(d, "2025-01-02.sqlite.gz")
    assert backup.cleanup_backups(retention_days=0, today=date(2026, 10, 29)) == []
    assert backup.cleanup_backups(retention_days=14, today=date(2026, 10, 29)) == ["2025-01-01.sqlite.gz"]
    assert [p.name for p in d.iterdir()] == ["2025-01-02.sqlite.gz"]


def test_create_backup_runs_retention_from_config(db, settings_env, monkeypatch):
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "3")
    reset_config()
    from app.config import get_config

    _touch(get_config().backups_dir, "2026-10-01.sqlite.gz")
    result = backup.create_backup(db, now=datetime(2026, 10, 29, 10))
    assert result.deleted == ["2026-10-01.sqlite.gz"]
    assert "1 alte Sicherung" in db.get(BackupLog, result.log_id).message


def test_list_backups_and_file_lookup(db, settings_env):
    result = backup.create_backup(db, now=datetime(2026, 10, 29, 10))
    listing = backup.list_backups(db)
    assert listing["log"][0]["file"] == result.file and listing["log"][0]["vorhanden"]
    assert listing["files"][0]["name"] == result.file
    assert listing["retention_days"] == 14 and listing["cron"] == "30 0 * * *"
    assert backup.backup_file_path(result.file) == result.path
    for bad in ("../deputat.sqlite", "deputat.sqlite", "2026-10-29.sqlite.gz/../x", ""):
        assert backup.backup_file_path(bad) is None
    assert backup.backup_file_path("2020-01-01.sqlite.gz") is None  # valid name, missing file


# =========================================================================== scheduler


def test_scheduler_disabled_under_pytest_and_by_env(monkeypatch):
    assert backup.scheduler_enabled() is False  # PYTEST_CURRENT_TEST is set
    monkeypatch.setenv(backup.SCHEDULER_ENV, "1")
    assert backup.scheduler_enabled() is True
    monkeypatch.setenv(backup.SCHEDULER_ENV, "off")
    assert backup.scheduler_enabled() is False
    app = SimpleNamespace(state=SimpleNamespace())
    assert backup.start_scheduler(app) is None


def test_build_scheduler_uses_backup_cron_in_berlin_time(settings_env, monkeypatch):
    scheduler = backup.build_scheduler()
    job = scheduler.get_job("backup")
    assert job is not None
    assert str(job.trigger.timezone) == "Europe/Berlin"
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["minute"] == "30" and fields["hour"] == "0"

    monkeypatch.setenv("BACKUP_CRON", "off")
    reset_config()
    assert backup.build_scheduler() is None or backup.build_scheduler().get_job("backup") is None

    monkeypatch.setenv("BACKUP_CRON", "kaputt")
    reset_config()
    assert backup.build_scheduler() is None or backup.build_scheduler().get_job("backup") is None


def test_timetable_job_scheduled_when_service_exists(settings_env, monkeypatch):
    calls = []
    fake = SimpleNamespace(generate_today_suggestions=lambda db: calls.append(db))
    monkeypatch.setattr(backup, "_timetable_job", lambda: fake.generate_today_suggestions)
    scheduler = backup.build_scheduler()
    job = scheduler.get_job("timetable_suggestions")
    assert job is not None
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert (fields["hour"], fields["minute"]) == ("0", "5")


def test_start_and_stop_scheduler(settings_env, monkeypatch):
    monkeypatch.setenv(backup.SCHEDULER_ENV, "1")
    app = SimpleNamespace(state=SimpleNamespace())
    scheduler = backup.start_scheduler(app)
    try:
        assert scheduler is not None and scheduler.running and app.state.scheduler is scheduler
        assert backup.start_scheduler(app) is scheduler  # idempotent
    finally:
        backup.stop_scheduler(app)
    assert app.state.scheduler is None
    backup.stop_scheduler(app)  # no-op


def test_run_scheduled_backup_writes_auto_log(db, settings_env):
    backup.run_scheduled_backup()
    row = db.scalars(select(BackupLog).order_by(BackupLog.id.desc())).first()
    assert row is not None and row.kind == "auto" and row.ok
