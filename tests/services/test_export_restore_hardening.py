"""Review fixes: JSON restore integrity (closed months, mandatory checksum, file references), path guards at the
point of use, bounded exports directory, Prüf-Hash in CSV/ICS."""

from __future__ import annotations

import csv
import io
import os
import time as _time
from datetime import date, datetime, time as dtime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Category, ImportBatch, MonthClose, TimeEntry
from app.services import entries as entries_svc
from app.services import export
from app.services import import_service
from app.services import months
from app.util import local_to_utc


def cat_id(db, code: str) -> int:
    return db.scalar(select(Category.id).where(Category.code == code))


def add(db, day: date, start: dtime, minutes: int, code: str = "VN"):
    start_at = local_to_utc(day, start)
    data = {"start_at": start_at, "end_at": start_at + timedelta(minutes=minutes), "category_id": cat_id(db, code)}
    return entries_svc.create_entry(db, data, source="manuell", now=local_to_utc(day, dtime(21)))[0]


def close(db, ym: str) -> MonthClose:
    for entry in entries_svc.month_hash_entries(db, ym):
        entry.locked = True
    mc = MonthClose(year_month=ym, closed_at=datetime(2026, 11, 1, 10),
                    entries_hash_sha256=entries_svc.month_entries_hash(db, ym),
                    soll_seconds=0, ist_seconds=0, saldo_seconds=0)
    db.add(mc)
    db.commit()
    return mc


def resign(backup: dict) -> None:
    backup["sha256"] = export._checksum(backup["tables"])


# ------------------------------------------------------------------ closed months


def test_restore_rejects_tampered_entries_of_a_closed_month(db):
    entry = add(db, date(2026, 10, 12), dtime(9), 60)
    close(db, "2026-10")
    backup = export.export_json(db)
    row = next(r for r in backup["tables"]["time_entry"] if r["id"] == entry.id)
    row["end_at"] = row["start_at"].replace("T07:", "T13:")
    row["duration_seconds"] = 7 * 3600
    resign(backup)  # the checksum is unkeyed: an attacker recomputes it
    with pytest.raises(export.ExportError, match="2026-10"):
        export.restore_json(db, backup, replace=True, safety_backup=False)
    db.expire_all()
    assert db.get(TimeEntry, entry.id).duration_seconds == 3600


def test_restore_of_untampered_backup_with_closed_month_works(db):
    add(db, date(2026, 10, 12), dtime(9), 60)
    close(db, "2026-10")
    backup = export.export_json(db)
    result = export.restore_json(db, backup, replace=True, safety_backup=False)
    assert result.replaced


def test_restore_requires_checksum(db):
    backup = export.export_json(db)
    backup.pop("sha256")
    with pytest.raises(export.ExportError, match="keine Prüfsumme"):
        export.validate_backup(db, backup)


# ------------------------------------------------------------------ file references in a backup


@pytest.mark.parametrize(
    ("table", "row", "message"),
    [
        ("import_batch", {"id": "..", "filename": "x.csv", "created_at": "2026-10-01T08:00:00"}, "Import-ID"),
        ("import_batch", {"id": "01a18b60-3e38-7266-be45-0d7b4159038f", "filename": "x.csv",
                          "created_at": "2026-10-01T08:00:00", "stored_file": "/proc/self/environ"},
         "stored_file"),
        ("month_close", {"year_month": "2026-09", "closed_at": "2026-10-01T08:00:00",
                         "entries_hash_sha256": "0" * 64, "soll_seconds": 0, "ist_seconds": 0,
                         "saldo_seconds": 0, "report_file": ".app_secret"}, "report_file"),
    ],
)
def test_restore_rejects_path_bearing_values(db, table, row, message):
    backup = export.export_json(db)
    backup["tables"][table].append(row)
    resign(backup)
    with pytest.raises(export.ExportError, match=message):
        export.validate_backup(db, backup)


def test_valid_stored_file_from_windows_or_linux_is_accepted(db):
    batch_id = "01a18b60-3e38-7266-be45-0d7b4159038f"
    for stored in (f"/data/imports/{batch_id}/original.csv", f"C:\\data\\imports\\{batch_id}\\original.xlsx"):
        backup = export.export_json(db)
        backup["tables"]["import_batch"].append(
            {"id": batch_id, "filename": "x.csv", "created_at": "2026-10-01T08:00:00", "stored_file": stored})
        resign(backup)
        export.validate_backup(db, backup)


def test_import_paths_never_leave_the_imports_folder(db, settings_env):
    with pytest.raises(import_service.ImportNotFound):
        import_service._batch_dir("..")
    batch = ImportBatch(id="01a18b60-3e38-7266-be45-0d7b4159038f", filename="x.csv",
                        stored_file=str(settings_env.data_dir / ".app_secret"), mapping_json={"file_type": "csv"})
    path = import_service._stored_path(batch)
    assert path == settings_env.imports_dir / batch.id / "original.csv"
    batch.mapping_json = {"file_type": "../../x"}
    with pytest.raises(import_service.ImportNotFound):
        import_service._stored_path(batch)


def test_undo_with_traversal_id_does_not_touch_data_dir(db, settings_env):
    settings_env.backups_dir.mkdir(parents=True, exist_ok=True)
    marker = settings_env.backups_dir / "keep.txt"
    marker.write_text("x")
    db.add(ImportBatch(id="..", filename="x.csv", mapping_json={"stage": "hochgeladen"}))
    db.commit()
    with pytest.raises(import_service.ImportServiceError):
        import_service.undo(db, "..")
    assert marker.exists()


def test_report_path_only_serves_month_reports(settings_env):
    mc = MonthClose(year_month="2026-09", report_file=".app_secret")
    assert months.report_path(mc) is None
    mc.report_file = "reports/../.app_secret"
    assert months.report_path(mc) is None
    mc.report_file = "reports/2026-09.html"
    assert months.report_path(mc) == (settings_env.reports_dir / "2026-09.html").resolve()


# ------------------------------------------------------------------ exports directory


def test_exports_directory_is_bounded(settings_env, monkeypatch):
    monkeypatch.setattr(export, "MAX_STORED_EXPORTS", 3)
    settings_env.exports_dir.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        path = settings_env.exports_dir / f"e{i}.csv"
        path.write_bytes(b"x")
        os.utime(path, (_time.time() - 100 + i, _time.time() - 100 + i))
    export.cleanup_exports()
    assert sorted(p.name for p in settings_env.exports_dir.iterdir()) == ["e2.csv", "e3.csv", "e4.csv"]


def test_old_exports_are_removed(settings_env):
    old = export.store_export("old.json", b"x")
    past = _time.time() - (settings_env.backup_retention_days + 1) * 86400
    os.utime(old, (past, past))
    assert export.cleanup_exports() == ["old.json"]


# ------------------------------------------------------------------ Prüf-Hash


def test_csv_and_ics_carry_the_check_hash(db):
    add(db, date(2026, 10, 12), dtime(9), 60)
    add(db, date(2026, 10, 13), dtime(9), 45, "K")
    entries = list(db.scalars(select(TimeEntry).order_by(TimeEntry.start_at)))
    expected = export.exported_entries_hash(entries)
    rows = list(csv.reader(io.StringIO(export.export_csv(db).decode("utf-8-sig"), newline=""), delimiter=";"))
    assert rows[-1] == [export.CSV_HASH_LABEL, "SHA-256 v1", expected, "2 Einträge"]
    ics = export.export_ics(db).decode("utf-8")
    assert f"X-DEPUTAT-PRUEF-HASH:sha256-v1:{expected}" in ics.replace("\r\n ", "")
    # Changing an entry changes the hash.
    entries_svc.update_entry(db, entries[0].id, {"note": "x"})
    assert export.exported_entries_hash(entries) != expected
