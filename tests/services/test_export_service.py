"""services/export.py: CSV (BOM, separator, decimal comma), JSON full backup + restore roundtrip, ICS."""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db.models import BackupLog, Category, ImportBatch, MonthClose, Settings, TimeEntry, TimeEntryRevision
from app.services import calendar_service as cal
from app.services import entries as entries_svc
from app.services import export
from app.services import months
from app.util import local_to_utc

H = 3600
EXAMPLE_ICS = Path(__file__).resolve().parents[2] / "examples" / "ferien_bayern_BEISPIEL.ics"


def cat_id(db, code: str) -> int:
    return db.scalar(select(Category.id).where(Category.code == code))


def add(db, day: date, start: dtime, minutes: int, code: str = "VN", **extra):
    start_at = local_to_utc(day, start)
    data = {"start_at": start_at, "end_at": start_at + timedelta(minutes=minutes), "category_id": cat_id(db, code),
            **extra}
    return entries_svc.create_entry(db, data, source="manuell", now=local_to_utc(day, dtime(21)))[0]


@pytest.fixture
def data(db):
    """A realistic data set: period, ferien, entries (incl. split, deleted, import), revisions, a closed month."""
    period = cal.create_period(db, "2026/27")
    cal.commit_ics(db, period, EXAMPLE_ICS.read_bytes())
    for d in (date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17)):
        add(db, d, dtime(7, 30), 8 * 60, "U", klasse="3b", fach="Deutsch", location="schule")
    e = add(db, date(2026, 9, 18), dtime(14), 90, "K", note="Proben 3b")
    entries_svc.update_entry(db, e.id, {"duration_seconds": 2 * H}, reason="länger")
    gone = add(db, date(2026, 9, 21), dtime(14), 30, "E")
    entries_svc.delete_entry(db, gone.id, reason="doppelt")
    add(db, date(2026, 9, 22), dtime(22, 30), 165, "VN")  # split at midnight
    batch = ImportBatch(filename="alt.csv", created_at=datetime(2026, 10, 1, 8))
    db.add(batch)
    db.commit()
    entries_svc.create_entry(
        db, {"day": date(2026, 9, 24), "has_bounds": False, "duration_seconds": "2,5", "category_id": cat_id(db, "K"),
             "import_batch_id": batch.id, "note": "=SUMME(A1)"},
        source="import", now=datetime(2026, 10, 1, 8),
    )
    months.update_mehrarbeit(db, "2026-09", status="beantragt", note="n", today=date(2026, 10, 29))
    months.close_month(db, "2026-08", force=True, note="Ferien", today=date(2026, 10, 29))
    return period


# =========================================================================== CSV


def parse_csv(content: bytes) -> list[list[str]]:
    """Data rows (header + entries); asserts and strips the closing Prüf-Hash rows."""
    assert content.startswith(b"\xef\xbb\xbf")
    text = content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=";"))
    assert rows[-2] == [] and rows[-1][0] == export.CSV_HASH_LABEL and len(rows[-1][2]) == 64
    return rows[:-2]


def resign(backup: dict) -> None:
    """Recompute the (unkeyed) checksum after tampering, as an attacker could."""
    backup["sha256"] = export._checksum(backup["tables"])


def test_csv_bom_separator_decimal_comma(db, data):
    content = export.export_csv(db)
    assert b"\r\n" in content
    rows = parse_csv(content)
    header, body = rows[0], rows[1:]
    assert header[0] == "Datum" and "Dauer (h)" in header and len(header) == len(export.CSV_COLUMNS)
    assert all(len(r) == len(header) for r in body)
    idx = {name: i for i, name in enumerate(header)}
    first = body[0]
    assert first[idx["Datum"]] == "15.09.2026"
    assert first[idx["Von"]] == "07:30:00" and first[idx["Bis"]] == "15:30:00"
    assert first[idx["Dauer (h:mm)"]] == "8:00"
    assert first[idx["Dauer (h)"]] == "8,00"
    assert first[idx["Kategorie-Code"]] == "U" and first[idx["Klasse"]] == "3b" and first[idx["Ort"]] == "Schule"
    # the import entry: no times, decimal 2,50, marker, formula neutralised
    imp = next(r for r in body if r[idx["Quelle"]] == "Import")
    assert imp[idx["Von"]] == "" and imp[idx["Dauer (h)"]] == "2,50"
    assert imp[idx["Import"]] == "Import vom 01.10.2026 aus Datei alt.csv"
    assert imp[idx["Notiz"]] == "'=SUMME(A1)"
    assert "ohne Zeitgrenzen" in imp[idx["Flags"]]
    # deleted entry only on request
    assert not any(r[idx["Gelöscht am"]] for r in body)
    with_deleted = parse_csv(export.export_csv(db, include_deleted=True))
    assert len(with_deleted) == len(rows) + 1


def test_csv_date_filter_and_semicolons_quoted(db, data):
    entry = db.scalars(select(TimeEntry).where(TimeEntry.day == date(2026, 9, 18))).one()
    entries_svc.update_entry(db, entry.id, {"note": 'a; b "c"'})
    rows = parse_csv(export.export_csv(db, date_from=date(2026, 9, 18), date_to=date(2026, 9, 18)))
    assert len(rows) == 2
    assert rows[1][rows[0].index("Notiz")] == 'a; b "c"'


# =========================================================================== JSON


def test_json_export_structure(db, data):
    backup = export.export_json(db)
    assert backup["format"] == "deputat-backup" and backup["format_version"] == 1
    assert backup["schema_revision"] == "0004"
    assert set(backup["tables"]) >= {"settings", "time_entry", "time_entry_revision", "month_close", "period",
                                     "ferien", "category", "calendar_day", "backup_log", "mehrarbeit_month"}
    assert backup["counts"]["time_entry"] == db.scalar(select(func.count()).select_from(TimeEntry))
    assert backup["sha256"]
    json.dumps(backup)  # serialisable
    entry = backup["tables"]["time_entry"][0]
    assert isinstance(entry["start_at"], str) and isinstance(entry["flags"], list)


def _fresh_db(tmp_path, monkeypatch, template):
    """A second, empty (migrated + seeded) database in another DATA_DIR."""
    from app.config import reset_config
    from app.db.seed import seed
    from app.db.session import SessionLocal

    import sqlite3

    data_dir = tmp_path / "other"
    data_dir.mkdir()
    src, dst = sqlite3.connect(template), sqlite3.connect(data_dir / "deputat.sqlite")
    src.backup(dst)
    src.close()
    dst.close()
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    reset_config()
    session = SessionLocal()
    seed(session)
    return session


def test_json_roundtrip_into_empty_database(db, data, tmp_path, monkeypatch, _migrated_template):
    backup = export.export_json(db)
    db.close()
    other = _fresh_db(tmp_path, monkeypatch, _migrated_template)
    try:
        assert not export.has_user_data(other)
        result = export.restore_json(other, json.dumps(backup).encode("utf-8"))
        assert not result.replaced and result.safety_backup is None
        again = export.export_json(other)
        assert again["tables"] == backup["tables"]
        assert again["sha256"] == backup["sha256"]
        # revisions restored, hash of the closed month still verifies (T20 across databases)
        assert other.scalar(select(func.count()).select_from(TimeEntryRevision)) == backup["counts"]["time_entry_revision"]
        mc = other.get(MonthClose, "2026-08")
        assert mc is not None and mc.entries_hash_sha256 == entries_svc.month_entries_hash(other, "2026-08")
        assert entries_svc.month_entries_hash(other, "2026-09") == backup_hash_for(backup, "2026-09")
    finally:
        other.close()


def backup_hash_for(backup, ym):
    from app.core.hashing import entries_hash

    rows = [r for r in backup["tables"]["time_entry"] if r["day"].startswith(ym) and r["status"] == "active"]
    for r in rows:
        for k in ("start_at", "end_at", "created_at", "deleted_at"):
            if r[k]:
                r[k] = datetime.fromisoformat(r[k])
        r["day"] = date.fromisoformat(r["day"])
    return entries_hash(rows)


def test_restore_into_populated_db_requires_replace(db, data):
    backup = export.export_json(db)
    with pytest.raises(export.RestoreConflict, match="bestätigen"):
        export.restore_json(db, backup)


def test_restore_replace_restores_previous_state_with_safety_backup(db, data, settings_env):
    backup = export.export_json(db)
    count_before = backup["counts"]["time_entry"]
    add(db, date(2026, 10, 5), dtime(8), 60)
    db.get(Settings, 1).name = "Geändert"
    db.commit()
    result = export.restore_json(db, backup, replace=True)
    assert result.replaced and result.safety_backup
    assert (settings_env.backups_dir / result.safety_backup).is_file()
    assert db.scalar(select(func.count()).select_from(TimeEntry)) == count_before
    assert db.get(Settings, 1).name == ""
    log = db.scalars(select(BackupLog).where(BackupLog.kind == "restore")).all()
    assert len(log) == 1 and log[0].file == result.safety_backup
    tables = export.export_json(db)["tables"]
    assert tables["time_entry"] == backup["tables"]["time_entry"]
    assert tables["time_entry_revision"] == backup["tables"]["time_entry_revision"]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda b: b.update(format="other"), "Keine Deputat-Sicherung"),
        (lambda b: b.update(format_version=99), "Format-Version"),
        (lambda b: b.update(schema_revision="9999"), "Schema-Version"),
        (lambda b: b["tables"].update(fremd=[]), "Prüfsumme"),
        (lambda b: (b["tables"]["time_entry"][0].update(duration_seconds="viel"), resign(b)), "ungültiger Wert"),
        (lambda b: (b["tables"]["time_entry"][0].update(extra=1), resign(b)), "unbekannte Spalten"),
        (lambda b: (b["tables"].update(fremd=[]), resign(b)), "Unbekannte Tabellen"),
        (lambda b: (b["tables"].update(settings=[]), resign(b)), "Einstellungszeile"),
        (lambda b: (b["tables"]["time_entry"].append(dict(b["tables"]["time_entry"][0])), resign(b)),
         "doppelter Primärschlüssel"),
        (lambda b: (b["tables"]["time_entry"][0].update(start_at="gestern"), resign(b)), "ungültiger Wert"),
    ],
)
def test_restore_validation_errors_leave_db_untouched(db, data, mutate, message):
    backup = export.export_json(db)
    before = export.export_json(db)["tables"]
    mutate(backup)
    with pytest.raises(export.ExportError, match=message):
        export.restore_json(db, backup, replace=True)
    assert export.export_json(db)["tables"] == before


def test_restore_rejects_inconsistent_references(db, data):
    backup = export.export_json(db)
    backup["tables"]["time_entry_revision"][0]["entry_id"] = "does-not-exist"
    resign(backup)
    before = export.export_json(db)["tables"]
    with pytest.raises(export.ExportError, match="nicht konsistent"):
        export.restore_json(db, backup, replace=True, safety_backup=False)
    assert export.export_json(db)["tables"] == before


def test_parse_backup_errors():
    with pytest.raises(export.ExportError, match="Ungültiges JSON"):
        export.parse_backup(b"{nope")
    with pytest.raises(export.ExportError, match="UTF-8"):
        export.parse_backup(b"\xff\xfe\x00")
    with pytest.raises(export.ExportError):
        export.parse_backup("[1, 2]")


def test_store_export_writes_into_exports_dir(settings_env):
    path = export.store_export("test.csv", b"abc")
    assert path == settings_env.exports_dir / "test.csv" and path.read_bytes() == b"abc"
    with pytest.raises(export.ExportError):
        export.store_export("../evil.csv", b"x")


# =========================================================================== ICS


def test_ics_is_parseable_and_has_one_event_per_active_entry(db, data):
    from icalendar import Calendar

    content = export.export_ics(db)
    assert content.startswith(b"BEGIN:VCALENDAR\r\n")
    assert all(len(line.encode("utf-8")) <= 75 for line in content.decode("utf-8").split("\r\n"))
    cal_obj = Calendar.from_ical(content)
    events = [c for c in cal_obj.walk("VEVENT")]
    active = db.scalar(select(func.count()).select_from(TimeEntry).where(
        TimeEntry.deleted_at.is_(None), TimeEntry.status == "active"))
    assert len(events) == active
    uids = {str(e["UID"]) for e in events}
    assert len(uids) == len(events)
    teaching = [e for e in events if str(e["SUMMARY"]).startswith("U Unterricht")]
    assert len(teaching) == 3 and "3b Deutsch" in str(teaching[0]["SUMMARY"])
    assert teaching[0].decoded("DTEND") - teaching[0].decoded("DTSTART") == timedelta(hours=8)
    allday = [e for e in events if not isinstance(e.decoded("DTSTART"), datetime)]
    assert len(allday) == 1 and allday[0].decoded("DTSTART") == date(2026, 9, 24)
    assert "(2:30)" in str(allday[0]["SUMMARY"])
    k = next(e for e in events if "Proben 3b" in str(e["DESCRIPTION"]))
    assert k.decoded("DTEND") - k.decoded("DTSTART") == timedelta(hours=2)


def test_ics_skips_running_timer(db, data):
    from app.services import timer

    timer.start(db, cat_id(db, "K"), started_at=datetime(2026, 10, 29, 8), now=datetime(2026, 10, 29, 8, 1))
    content = export.export_ics(db).decode("utf-8")
    assert content.count("BEGIN:VEVENT") == db.scalar(select(func.count()).select_from(TimeEntry).where(
        TimeEntry.deleted_at.is_(None), TimeEntry.status == "active", TimeEntry.end_at.is_not(None)))
