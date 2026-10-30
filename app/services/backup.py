"""Backups (Konzept §14.4, DEPLOY.md "Daten, Backup"): consistent SQLite snapshots + scheduler.

- :func:`create_backup` – snapshot via the SQLite online backup API into a temporary file next to the
  target (consistent and WAL-safe: committed WAL content is included, writers are not blocked for
  long), ``PRAGMA quick_check`` on the copy, gzip to ``DATA_DIR/backups/YYYY-MM-DD.sqlite.gz``
  (local date; a second backup on the same day gets ``_HHMMSS``), atomic rename, a ``backup_log`` row
  and the retention cleanup.
- :func:`cleanup_backups` – deletes snapshot files older than ``BACKUP_RETENTION_DAYS`` (by the date in
  the file name; the newest file is always kept; ``0`` disables the cleanup).
- :func:`list_backups` – ``backup_log`` rows plus the files on disk.
- :func:`start_scheduler` / :func:`stop_scheduler` – APScheduler ``BackgroundScheduler`` (Europe/Berlin)
  with the ``BACKUP_CRON`` job and, if ``app.services.timetable.generate_today_suggestions`` exists,
  the 00:05 timetable suggestions. ``app.main`` calls both from the lifespan.

The scheduler is disabled when ``DEPUTAT_SCHEDULER`` is ``0/false/off/no`` and automatically while
pytest runs (``PYTEST_CURRENT_TEST``; ``DEPUTAT_SCHEDULER=1`` forces it on). ``BACKUP_CRON=off``
disables only the backup job.
"""

from __future__ import annotations

import gzip
import importlib
import inspect
import logging
import os
import re
import shutil
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_config
from app.db.models import BackupLog
from app.util import LOCAL_TZ, now_utc, to_local, today_local

log = logging.getLogger(__name__)

BACKUP_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:_(\d{6}))?(?:_[a-z0-9-]+)?\.sqlite\.gz$")
SCHEDULER_ENV = "DEPUTAT_SCHEDULER"
TIMETABLE_JOB_HOUR, TIMETABLE_JOB_MINUTE = 0, 5

_backup_lock = threading.Lock()


class BackupError(RuntimeError):
    pass


@dataclass
class BackupResult:
    ok: bool
    file: str | None  # file name inside the backups directory
    path: Path | None
    size_bytes: int
    message: str | None = None
    deleted: list[str] = field(default_factory=list)
    log_id: int | None = None


# =========================================================================== snapshot


def _db_path() -> Path:
    path = get_config().db_path
    if path is None:
        raise BackupError("Backups sind nur für eine SQLite-Datei möglich.")
    if not path.exists():
        raise BackupError(f"Datenbankdatei nicht gefunden: {path}")
    return path


def _target_name(now: datetime, suffix: str | None, directory: Path) -> str:
    local = to_local(now)
    tail = f"_{suffix}" if suffix else ""
    name = f"{local:%Y-%m-%d}{tail}.sqlite.gz"
    if (directory / name).exists():
        name = f"{local:%Y-%m-%d}_{local:%H%M%S}{tail}.sqlite.gz"
    return name


def _snapshot(source: Path, target: Path) -> None:
    """Consistent copy via the online backup API; the copy is switched to rollback-journal mode."""
    src = sqlite3.connect(str(source), timeout=30)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst, pages=1024, sleep=0.005)
            dst.execute("PRAGMA journal_mode=DELETE")
            result = dst.execute("PRAGMA quick_check").fetchone()
            if not result or result[0] != "ok":
                raise BackupError(f"Integritätsprüfung der Kopie fehlgeschlagen: {result!r}")
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def _gzip(source: Path, target: Path) -> None:
    tmp = target.with_name(target.name + ".part")
    with open(source, "rb") as fin, gzip.open(tmp, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
    tmp.replace(target)


def _write_log(db: Session | None, entry: BackupLog) -> int | None:
    from app.db.session import SessionLocal

    own = db is None
    session = SessionLocal() if own else db
    try:
        session.add(entry)
        session.commit()
        return entry.id
    except Exception:  # logging must never mask the backup result
        session.rollback()
        log.exception("backup_log row could not be written")
        return None
    finally:
        if own:
            session.close()


def create_backup(
    db: Session | None = None,
    *,
    kind: str = "manuell",
    suffix: str | None = None,
    now: datetime | None = None,
    write_log: bool = True,
    cleanup: bool = True,
) -> BackupResult:
    """Create a gzip'ed SQLite snapshot in ``DATA_DIR/backups`` (see module docstring).

    Never raises for I/O problems: the result has ``ok=False`` and a German ``message`` (also logged in
    ``backup_log``). ``write_log=False`` skips the ``backup_log`` row (used before a restore, which replaces
    that table anyway).
    """
    now = (now or now_utc()).replace(microsecond=0)
    cfg = get_config()
    directory = cfg.backups_dir
    result: BackupResult
    with _backup_lock:
        tmp: Path | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            source = _db_path()
            name = _target_name(now, suffix, directory)
            target = directory / name
            tmp = directory / f".{name}.tmp.sqlite"
            if tmp.exists():
                tmp.unlink()
            _snapshot(source, tmp)
            _gzip(tmp, target)
            result = BackupResult(ok=True, file=name, path=target, size_bytes=target.stat().st_size)
            _logger().info("Backup written: %s (%d bytes)", target, result.size_bytes)
        except Exception as exc:  # noqa: BLE001 - reported to the user and the log table
            _logger().exception("Backup failed")
            result = BackupResult(ok=False, file=None, path=None, size_bytes=0,
                                  message=f"Backup fehlgeschlagen: {exc}")
        finally:
            if tmp is not None:
                for leftover in (tmp, tmp.with_name(tmp.name + "-journal")):
                    try:
                        leftover.unlink(missing_ok=True)
                    except OSError:
                        pass
        if result.ok and cleanup:
            result.deleted = cleanup_backups(today=to_local(now).date())
    if write_log:
        message = result.message
        if result.ok and result.deleted:
            message = f"{len(result.deleted)} alte Sicherung(en) gelöscht"
        result.log_id = _write_log(db, BackupLog(
            created_at=now, file=result.file or "", size_bytes=result.size_bytes, ok=result.ok,
            kind=kind[:16], message=message,
        ))
    return result


def _logger() -> logging.Logger:
    return logging.getLogger(__name__)


# =========================================================================== retention / listing


def backup_files() -> list[dict[str, Any]]:
    """Snapshot files in the backups directory, newest first."""
    directory = get_config().backups_dir
    if not directory.exists():
        return []
    files = []
    for path in directory.iterdir():
        match = BACKUP_NAME_RE.match(path.name)
        if not match or not path.is_file():
            continue
        stat = path.stat()
        files.append({
            "name": path.name,
            "date": date.fromisoformat(match.group(1)),
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, LOCAL_TZ),
        })
    files.sort(key=lambda f: (f["date"], f["name"]), reverse=True)
    return files


def cleanup_backups(*, retention_days: int | None = None, today: date | None = None) -> list[str]:
    """Delete snapshots older than ``retention_days`` (file-name date < today − days). Returns the names.

    The newest snapshot is always kept, so a long outage never leaves the directory empty.
    """
    days = get_config().backup_retention_days if retention_days is None else retention_days
    if days <= 0:
        return []
    today = today or today_local()
    cutoff = today - timedelta(days=days)
    files = backup_files()
    deleted: list[str] = []
    for info in files[1:]:  # files[0] is the newest
        if info["date"] < cutoff:
            try:
                (get_config().backups_dir / info["name"]).unlink()
                deleted.append(info["name"])
            except OSError:
                _logger().warning("Could not delete old backup %s", info["name"])
    if deleted:
        _logger().info("Retention: deleted %d old backup(s)", len(deleted))
    return deleted


def backup_file_path(name: str) -> Path | None:
    """Path of a snapshot file by name (strict name check, never outside the backups directory)."""
    if not BACKUP_NAME_RE.match(name or ""):
        return None
    path = get_config().backups_dir / name
    return path if path.is_file() else None


def list_backups(db: Session, *, limit: int = 100) -> dict[str, Any]:
    rows = db.scalars(select(BackupLog).order_by(BackupLog.created_at.desc(), BackupLog.id.desc()).limit(limit))
    on_disk = {f["name"] for f in backup_files()}
    cfg = get_config()
    return {
        "log": [
            {
                "id": r.id,
                "created_at": r.created_at,
                "file": r.file,
                "size_bytes": r.size_bytes,
                "ok": r.ok,
                "kind": r.kind,
                "message": r.message,
                "vorhanden": bool(r.file) and r.file in on_disk,
            }
            for r in rows
        ],
        "files": backup_files(),
        "retention_days": cfg.backup_retention_days,
        "cron": cfg.backup_cron,
        "directory": str(cfg.backups_dir),
    }


# =========================================================================== scheduler


def scheduler_enabled() -> bool:
    value = (os.environ.get(SCHEDULER_ENV) or "").strip().lower()
    if value in ("0", "false", "off", "no"):
        return False
    if value in ("1", "true", "on", "yes"):
        return True
    return "PYTEST_CURRENT_TEST" not in os.environ


def run_scheduled_backup() -> None:
    """Scheduler job: nightly backup (never raises)."""
    try:
        result = create_backup(kind="auto")
        if not result.ok:
            _logger().error("Scheduled backup failed: %s", result.message)
    except Exception:  # pragma: no cover - create_backup already catches
        _logger().exception("Scheduled backup crashed")


def _timetable_job():
    """The timetable suggestion generator if that service exists (guarded import)."""
    try:
        module = importlib.import_module("app.services.timetable")
    except ImportError:
        return None
    fn = getattr(module, "generate_today_suggestions", None)
    return fn if callable(fn) else None


def run_timetable_suggestions() -> None:
    """Scheduler job 00:05: create today's timetable suggestions (never raises)."""
    fn = _timetable_job()
    if fn is None:
        return
    from app.db.session import SessionLocal

    try:
        params = [p for p in inspect.signature(fn).parameters.values()
                  if p.default is inspect.Parameter.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        if params:
            with SessionLocal() as db:
                fn(db)
        else:
            fn()
    except Exception:
        _logger().exception("Timetable suggestions failed")


def build_scheduler():
    """A configured (not started) BackgroundScheduler, or None if nothing is to be scheduled."""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    cfg = get_config()
    scheduler = BackgroundScheduler(timezone=LOCAL_TZ, job_defaults={
        "coalesce": True, "max_instances": 1, "misfire_grace_time": 3600,
    })
    jobs = 0
    cron = (cfg.backup_cron or "").strip()
    if cron and cron.lower() not in ("off", "none", "-"):
        try:
            trigger = CronTrigger.from_crontab(cron, timezone=LOCAL_TZ)
        except ValueError:
            _logger().error("Invalid BACKUP_CRON %r – nightly backup disabled", cron)
        else:
            scheduler.add_job(run_scheduled_backup, trigger, id="backup", replace_existing=True)
            jobs += 1
    if _timetable_job() is not None:
        scheduler.add_job(
            run_timetable_suggestions,
            CronTrigger(hour=TIMETABLE_JOB_HOUR, minute=TIMETABLE_JOB_MINUTE, timezone=LOCAL_TZ),
            id="timetable_suggestions",
            replace_existing=True,
        )
        jobs += 1
    return scheduler if jobs else None


def start_scheduler(app: Any) -> Any:
    """Start the background scheduler (called by ``app.main`` on startup). Returns it (or None)."""
    if not scheduler_enabled():
        _logger().info("Scheduler disabled")
        return None
    existing = getattr(app.state, "scheduler", None)
    if existing is not None and getattr(existing, "running", False):
        return existing
    scheduler = build_scheduler()
    if scheduler is None:
        return None
    scheduler.start()
    app.state.scheduler = scheduler
    for job in scheduler.get_jobs():
        _logger().info("Scheduled job %s, next run %s", job.id, job.next_run_time)
    return scheduler


def stop_scheduler(app: Any) -> None:
    """Shut the scheduler down (called by ``app.main`` on shutdown)."""
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None and getattr(scheduler, "running", False):
        scheduler.shutdown(wait=False)
    if scheduler is not None:
        app.state.scheduler = None
