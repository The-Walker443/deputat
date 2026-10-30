"""Exports (Konzept §10.4): CSV of the entries, JSON full backup + restore, ICS of the entries.

- :func:`export_csv` – entries flat, UTF-8 with BOM, ``;`` separator and decimal comma for German
  Excel. Text cells starting with ``= + - @`` are prefixed with ``'`` (no formula injection). The file
  ends with an empty row and a "Prüf-Hash" row (Konzept L4: exports carry a check hash, see
  :func:`exported_entries_hash`); the ICS export carries it as ``X-DEPUTAT-PRUEF-HASH``.
- :func:`export_json` / :func:`export_json_bytes` – every table of the schema (``Base.metadata``) with
  the Alembic schema revision, the app version and a SHA-256 over the table data.
- :func:`restore_json` – validates a JSON backup (format, schema revision, tables, columns, value types,
  mandatory checksum, ids and file references) and restores it in ONE transaction: into an empty database,
  or replacing all data when ``replace=True`` (the API requires ``confirm=true`` for that). Revisions are
  restored like every other table. Before replacing data a safety backup
  (``backups/…_vor-wiederherstellung.sqlite.gz``) is made. After inserting, the stored hash of every closed
  month is recomputed from the restored entries; a mismatch rejects the whole restore (a closed month can
  never be rewritten silently through a backup file – Konzept L4/§10.1).
- :func:`export_ics` – one VEVENT per active entry (read-only calendar view).
- :func:`store_export` – files generated through the API are also stored under ``DATA_DIR/exports``;
  :func:`cleanup_exports` keeps at most :data:`MAX_STORED_EXPORTS` files and none older than the backup
  retention (exports are full plaintext copies and GET requests can be triggered cross-site).

The restore writes ``time_entry`` rows directly (it restores raw table contents, revisions included);
this is the only intended exception to "all writes go through services/entries.py".
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, Table, Time, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.types import JSON

from app.config import APP_VERSION, RULES_VERSION, get_config
from app.core.hashing import HASH_VERSION, entries_hash
from app.db.base import Base
from app.db.models import Category, EntryStatus, ImportBatch, TimeEntry
from app.util import fmt_dec, fmt_hm, now_utc, to_local

log = logging.getLogger(__name__)

BACKUP_FORMAT = "deputat-backup"
BACKUP_FORMAT_VERSION = 1
CSV_DELIMITER = ";"
MAX_RESTORE_BYTES = 64 * 1024 * 1024
MAX_STORED_EXPORTS = 30
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
REPORT_FILE_RE = re.compile(r"reports/\d{4}-\d{2}\.html")
CSV_HASH_LABEL = "Prüf-Hash"

# Tables whose rows mean "the database holds user data" (seeded singletons/categories do not count).
USER_DATA_TABLES = (
    "time_entry", "time_entry_revision", "period", "ferien", "day_override", "month_close",
    "import_batch", "document", "timetable", "day_confirmation", "mehrarbeit_month",
)

CSV_COLUMNS = (
    "Datum", "Wochentag", "Von", "Bis", "Dauer (h:mm)", "Dauer (h)", "Dauer (s)", "Kategorie-Code",
    "Kategorie", "Unterkategorie", "Klasse", "Fach", "Ort", "Flags", "Notiz", "Quelle", "Import",
    "Status", "Erfasst am", "Geändert am", "Gelöscht am", "Gesperrt", "ID",
)
WEEKDAYS_SHORT = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


STATUS_LABELS = {
    EntryStatus.ACTIVE.value: "aktiv",
    EntryStatus.VORSCHLAG.value: "Vorschlag",
    EntryStatus.VERWORFEN.value: "verworfen",
}


class ExportError(ValueError):
    """Invalid export/restore input; German message."""

    status_code = 400


class RestoreConflict(ExportError):
    """The database is not empty and ``replace`` was not confirmed."""

    status_code = 409


# =========================================================================== helpers


def timestamp_slug(now: datetime | None = None) -> str:
    return to_local(now or now_utc()).strftime("%Y%m%d-%H%M%S")


def store_export(filename: str, content: bytes) -> Path:
    """Write ``content`` to ``DATA_DIR/exports/<filename>`` (atomic) and return the path."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", filename) or filename.startswith("."):
        raise ExportError("Ungültiger Dateiname für den Export.")
    directory = get_config().exports_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(content)
    tmp.replace(path)
    cleanup_exports()
    return path


def cleanup_exports(*, now: datetime | None = None) -> list[str]:
    """Delete stored exports older than the backup retention and all but the newest MAX_STORED_EXPORTS."""
    cfg = get_config()
    directory = cfg.exports_dir
    if not directory.is_dir():
        return []
    files = sorted(
        (p for p in directory.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True
    )
    cutoff = (now or datetime.now()).timestamp() - int(cfg.backup_retention_days) * 86400
    removed = []
    for index, path in enumerate(files):
        if index >= MAX_STORED_EXPORTS or path.stat().st_mtime < cutoff:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError as exc:  # pragma: no cover - best effort
                log.warning("Export %s could not be removed: %s", path.name, exc)
    return removed


def exported_entries_hash(entries: Iterable[TimeEntry]) -> str:
    """SHA-256 (``core.hashing``, canonical form v``HASH_VERSION``) over exactly the exported entries."""
    from app.services.entries import entry_hash_dict

    return entries_hash(entry_hash_dict(e) for e in entries)


def _entries(
    db: Session,
    date_from: date | None,
    date_to: date | None,
    *,
    include_deleted: bool,
    include_vorschlaege: bool,
) -> list[TimeEntry]:
    query = select(TimeEntry)
    if date_from is not None:
        query = query.where(TimeEntry.day >= date_from)
    if date_to is not None:
        query = query.where(TimeEntry.day <= date_to)
    if not include_deleted:
        query = query.where(TimeEntry.deleted_at.is_(None))
    statuses = [EntryStatus.ACTIVE.value] + ([EntryStatus.VORSCHLAG.value] if include_vorschlaege else [])
    query = query.where(TimeEntry.status.in_(statuses)).order_by(TimeEntry.start_at, TimeEntry.id)
    return list(db.scalars(query))


def _safe_cell(value: str | None) -> str:
    """Neutralise spreadsheet formulas in free-text cells."""
    if not value:
        return ""
    return "'" + value if value.startswith(_FORMULA_START) else value


def _local(dt: datetime | None, fmt: str) -> str:
    return to_local(dt).strftime(fmt) if dt else ""


# =========================================================================== CSV


def export_csv(
    db: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    include_deleted: bool = False,
    include_vorschlaege: bool = False,
) -> bytes:
    """Entries as CSV for German Excel (UTF-8 BOM, ``;``, decimal comma, CRLF)."""
    from app.services.reports import FLAG_LABELS, LOCATION_LABELS, SOURCE_LABELS, import_marker

    cats = {c.id: c for c in db.scalars(select(Category))}
    entries = _entries(db, date_from, date_to, include_deleted=include_deleted,
                       include_vorschlaege=include_vorschlaege)
    batch_ids = {e.import_batch_id for e in entries if e.import_batch_id}
    batches = (
        {b.id: b for b in db.scalars(select(ImportBatch).where(ImportBatch.id.in_(batch_ids)))}
        if batch_ids else {}
    )
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=CSV_DELIMITER, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for e in entries:
        cat = cats.get(e.category_id)
        sub = cats.get(e.subcategory_id) if e.subcategory_id else None
        seconds = int(e.duration_seconds or 0)
        writer.writerow([
            e.day.strftime("%d.%m.%Y"),
            WEEKDAYS_SHORT[e.day.weekday()],
            _local(e.start_at, "%H:%M:%S") if e.has_bounds else "",
            _local(e.end_at, "%H:%M:%S") if e.has_bounds else "",
            fmt_hm(seconds),
            fmt_dec(seconds, places=2, sep=","),
            seconds,
            cat.code if cat else "",
            _safe_cell(cat.name if cat else ""),
            _safe_cell(sub.name if sub else ""),
            _safe_cell(e.klasse),
            _safe_cell(e.fach),
            LOCATION_LABELS.get(e.location or "", e.location or ""),
            ", ".join(FLAG_LABELS.get(f, f) for f in (e.flags or [])),
            _safe_cell(e.note),
            SOURCE_LABELS.get(e.source, e.source),
            _safe_cell(import_marker(batches.get(e.import_batch_id or ""))),
            STATUS_LABELS.get(e.status, e.status),
            _local(e.created_at, "%d.%m.%Y %H:%M:%S"),
            _local(e.updated_at, "%d.%m.%Y %H:%M:%S"),
            _local(e.deleted_at, "%d.%m.%Y %H:%M:%S"),
            "ja" if e.locked else "nein",
            e.id,
        ])
    writer.writerow([])
    writer.writerow([CSV_HASH_LABEL, f"SHA-256 v{HASH_VERSION}", exported_entries_hash(entries),
                     f"{len(entries)} Einträge"])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


# =========================================================================== JSON backup


def schema_revision(db: Session) -> str | None:
    """Current Alembic revision of the database (``None`` if unknown)."""
    try:
        return db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # table missing (e.g. created via metadata.create_all)
        db.rollback()
        return None


def _tables() -> list[Table]:
    return list(Base.metadata.sorted_tables)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat() if value.tzinfo is None else value.astimezone(UTC).replace(
            tzinfo=None).isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    return value


def _checksum(tables: Mapping[str, Any]) -> str:
    payload = json.dumps(tables, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def export_json(db: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Full backup of every table (Konzept §10.4)."""
    tables: dict[str, list[dict[str, Any]]] = {}
    for table in _tables():
        pk = list(table.primary_key.columns)
        rows = db.execute(select(table).order_by(*pk)).mappings().all()
        tables[table.name] = [{k: _json_value(v) for k, v in row.items()} for row in rows]
    return {
        "format": BACKUP_FORMAT,
        "format_version": BACKUP_FORMAT_VERSION,
        "schema_revision": schema_revision(db),
        "app_version": APP_VERSION,
        "rules_version": RULES_VERSION,
        "created_at": _json_value((now or now_utc()).replace(microsecond=0)),
        "counts": {name: len(rows) for name, rows in tables.items()},
        "sha256": _checksum(tables),
        "tables": tables,
    }


def export_json_bytes(db: Session, *, now: datetime | None = None) -> bytes:
    return json.dumps(export_json(db, now=now), ensure_ascii=False, indent=1).encode("utf-8")


@dataclass
class RestoreResult:
    counts: dict[str, int]
    replaced: bool
    safety_backup: str | None = None
    warnings: list[str] = field(default_factory=list)


def _convert(table: Table, column_name: str, value: Any, where: str) -> Any:
    column = table.columns[column_name]
    if value is None:
        if not column.nullable and not column.primary_key:
            raise ExportError(f"{where}: Spalte „{column_name}“ darf nicht leer sein.")
        return None
    ctype = column.type
    try:
        if isinstance(ctype, DateTime):
            return datetime.fromisoformat(str(value)).replace(tzinfo=None)
        if isinstance(ctype, Date):
            return date.fromisoformat(str(value))
        if isinstance(ctype, Time):
            return time.fromisoformat(str(value))
        if isinstance(ctype, Boolean):
            if not isinstance(value, (bool, int)):
                raise ValueError
            return bool(value)
        if isinstance(ctype, Integer):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError
            return value
        if isinstance(ctype, Float):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError
            return float(value)
        if isinstance(ctype, JSON):
            return value
        if not isinstance(value, str):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise ExportError(f"{where}: ungültiger Wert für „{column_name}“: {value!r}") from None


def validate_backup(db: Session, data: Any) -> dict[str, list[dict[str, Any]]]:
    """Check a parsed JSON backup; returns the converted rows per table (raises :class:`ExportError`)."""
    if not isinstance(data, Mapping) or data.get("format") != BACKUP_FORMAT:
        raise ExportError("Keine Deputat-Sicherung (Feld „format“ fehlt oder ist falsch).")
    if data.get("format_version") != BACKUP_FORMAT_VERSION:
        raise ExportError(f"Nicht unterstützte Format-Version: {data.get('format_version')!r}.")
    current = schema_revision(db)
    if current is not None and data.get("schema_revision") != current:
        raise ExportError(
            f"Die Sicherung hat Schema-Version {data.get('schema_revision')!r}, die Datenbank {current!r}. "
            "Bitte mit der passenden App-Version wiederherstellen."
        )
    tables = data.get("tables")
    if not isinstance(tables, Mapping):
        raise ExportError("Die Sicherung enthält keine Tabellen.")
    if not isinstance(data.get("sha256"), str):
        raise ExportError("Die Sicherung hat keine Prüfsumme („sha256“) – Wiederherstellen abgelehnt.")
    if data["sha256"] != _checksum(tables):
        raise ExportError("Prüfsumme stimmt nicht – die Datei ist beschädigt oder wurde verändert.")
    known = {t.name: t for t in _tables()}
    unknown = sorted(set(tables) - set(known))
    if unknown:
        raise ExportError("Unbekannte Tabellen in der Sicherung: " + ", ".join(unknown) + ".")

    result: dict[str, list[dict[str, Any]]] = {}
    for name, table in known.items():
        rows = tables.get(name, [])
        if not isinstance(rows, list):
            raise ExportError(f"Tabelle „{name}“: Liste von Zeilen erwartet.")
        columns = set(table.columns.keys())
        converted = []
        seen_pk: set[tuple] = set()
        pk_cols = [c.name for c in table.primary_key.columns]
        for index, row in enumerate(rows, start=1):
            where = f"Tabelle „{name}“, Zeile {index}"
            if not isinstance(row, Mapping):
                raise ExportError(f"{where}: Objekt erwartet.")
            extra = set(row) - columns
            if extra:
                raise ExportError(f"{where}: unbekannte Spalten {', '.join(sorted(extra))}.")
            values = {col: _convert(table, col, row[col], where) for col in row}
            pk = tuple(values.get(c) for c in pk_cols)
            if any(v is None for v in pk):
                raise ExportError(f"{where}: Primärschlüssel fehlt.")
            if pk in seen_pk:
                raise ExportError(f"{where}: doppelter Primärschlüssel {pk!r}.")
            seen_pk.add(pk)
            _check_references(name, values, where)
            converted.append(values)
        result[name] = converted
    if len(result.get("settings", [])) != 1:
        raise ExportError("Die Sicherung muss genau eine Einstellungszeile enthalten.")
    return result


def _check_references(table: str, values: Mapping[str, Any], where: str) -> None:
    """Ids and file references that are used as paths must stay inside their DATA_DIR subfolder."""
    if table == "import_batch":
        batch_id = str(values.get("id") or "")
        if not UUID_RE.fullmatch(batch_id):
            raise ExportError(f"{where}: ungültige Import-ID.")
        stored = values.get("stored_file")
        if stored is not None:
            parts = re.split(r"[\\/]", str(stored))  # written on Linux or Windows
            if len(parts) < 2 or not parts[-1].startswith("original.") or parts[-2] != batch_id:
                raise ExportError(f"{where}: ungültiger Dateiverweis „stored_file“.")
    elif table == "month_close":
        report = values.get("report_file")
        if report is not None and not REPORT_FILE_RE.fullmatch(str(report)):
            raise ExportError(f"{where}: ungültiger Dateiverweis „report_file“.")
    elif table == "document":
        if values.get("rendered_pdf") is not None:
            raise ExportError(f"{where}: Dateiverweise von Dokumenten werden nicht wiederhergestellt.")


def _check_closed_months(db: Session) -> None:
    """Every closed month's stored hash must match the hash recomputed from the (restored) entries."""
    from app.db.models import MonthClose
    from app.services.entries import month_entries_hash

    bad = [
        mc.year_month
        for mc in db.scalars(select(MonthClose).order_by(MonthClose.year_month))
        if month_entries_hash(db, mc.year_month) != mc.entries_hash_sha256
    ]
    if bad:
        raise ExportError(
            "Die Sicherung ist nicht stimmig: Die Einträge der abgeschlossenen Monate "
            + ", ".join(bad)
            + " passen nicht zum gespeicherten Prüf-Hash. Wiederherstellen abgelehnt."
        )


def has_user_data(db: Session) -> bool:
    known = {t.name: t for t in _tables()}
    return any(
        db.execute(select(func.count()).select_from(known[name])).scalar() for name in USER_DATA_TABLES
    )


def parse_backup(content: bytes | str | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(content, Mapping):
        return content
    if isinstance(content, (bytes, bytearray)):
        if len(content) > MAX_RESTORE_BYTES:
            raise ExportError("Die Datei ist zu groß.")
        try:
            content = bytes(content).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ExportError("Die Datei ist kein UTF-8-Text.") from None
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ExportError(f"Ungültiges JSON (Zeile {exc.lineno}).") from None
    if not isinstance(data, Mapping):
        raise ExportError("Keine Deputat-Sicherung.")
    return data


def restore_json(
    db: Session,
    content: bytes | str | Mapping[str, Any],
    *,
    replace: bool = False,
    safety_backup: bool = True,
) -> RestoreResult:
    """Restore a JSON full backup (see module docstring). All or nothing."""
    data = parse_backup(content)
    rows_by_table = validate_backup(db, data)
    populated = has_user_data(db)
    if populated and not replace:
        raise RestoreConflict(
            "Die Datenbank enthält bereits Daten. Wiederherstellen ersetzt ALLE Daten – bitte ausdrücklich "
            "bestätigen."
        )
    db.commit()  # end any open read transaction before the snapshot / the restore transaction

    backup_file: str | None = None
    backup_result = None
    if populated and safety_backup:
        from app.services import backup as backup_service

        backup_result = backup_service.create_backup(kind="restore", suffix="vor-wiederherstellung", write_log=False,
                                                     cleanup=False)
        if not backup_result.ok:
            raise ExportError("Sicherheitskopie vor der Wiederherstellung fehlgeschlagen – abgebrochen.")
        backup_file = backup_result.file

    tables = _tables()
    try:
        for table in reversed(tables):
            db.execute(table.delete())
        db.execute(text("PRAGMA defer_foreign_keys = ON"))
        for table in tables:
            rows = rows_by_table.get(table.name, [])
            if table.name == "category":  # parents first (self reference)
                rows = sorted(rows, key=lambda r: (r.get("parent_id") is not None, r.get("id") or 0))
            # executemany needs identical keys; rows missing optional columns get the column defaults
            groups: dict[frozenset[str], list[dict[str, Any]]] = {}
            for row in rows:
                groups.setdefault(frozenset(row), []).append(row)
            for group in groups.values():
                for chunk_start in range(0, len(group), 500):
                    db.execute(table.insert(), group[chunk_start:chunk_start + 500])
        violations = db.execute(text("PRAGMA foreign_key_check")).fetchall()
        if violations:
            raise ExportError(
                f"Die Sicherung ist nicht konsistent ({len(violations)} ungültige Verweise, z. B. in "
                f"Tabelle „{violations[0][0]}“)."
            )
        db.expire_all()  # identity-mapped objects from before the restore must not hide the new rows
        _check_closed_months(db)
        if backup_result is not None:
            from app.db.models import BackupLog

            db.add(BackupLog(created_at=now_utc(), file=backup_result.file, size_bytes=backup_result.size_bytes,
                             ok=True, kind="restore", message="Sicherheitskopie vor Wiederherstellung"))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.expire_all()

    from app.db.seed import seed

    seed(db)  # timer_state/settings singletons if the backup lacked them; never changes restored rows
    counts = {name: len(rows) for name, rows in rows_by_table.items()}
    log.info("JSON backup restored (%s rows, replaced=%s)", sum(counts.values()), populated)
    return RestoreResult(counts=counts, replaced=populated, safety_backup=backup_file)


# =========================================================================== ICS


def _ics_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> Iterable[str]:
    """RFC 5545 line folding at 75 octets."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        yield line
        return
    current = b""
    first = True
    for ch in line:
        enc = ch.encode("utf-8")
        limit = 75 if first else 74
        if len(current) + len(enc) > limit:
            yield (current.decode("utf-8") if first else " " + current.decode("utf-8"))
            first = False
            current = b""
        current += enc
    yield current.decode("utf-8") if first else " " + current.decode("utf-8")


def _ics_dt(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def export_ics(
    db: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    now: datetime | None = None,
) -> bytes:
    """Active, non-deleted entries as an iCalendar file (one VEVENT each, times in UTC).

    Entries without bounds become all-day events on their day; a running timer is skipped.
    """
    from app.services.reports import FLAG_LABELS, LOCATION_LABELS, SOURCE_LABELS

    cats = {c.id: c for c in db.scalars(select(Category))}
    stamp = _ics_dt((now or now_utc()).replace(microsecond=0))
    entries = [e for e in _entries(db, date_from, date_to, include_deleted=False, include_vorschlaege=False)
               if e.end_at is not None]
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:-//Deputat//Arbeitszeitkonto {APP_VERSION}//DE",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Deputat – Arbeitszeit",
        "X-WR-TIMEZONE:Europe/Berlin",
        f"X-DEPUTAT-PRUEF-HASH:sha256-v{HASH_VERSION}:{exported_entries_hash(entries)}",
    ]
    for e in entries:
        cat = cats.get(e.category_id)
        sub = cats.get(e.subcategory_id) if e.subcategory_id else None
        seconds = int(e.duration_seconds or 0)
        title = f"{cat.code} {cat.name}" if cat else "Eintrag"
        if e.klasse or e.fach:
            title += " · " + " ".join(x for x in (e.klasse, e.fach) if x)
        if not e.has_bounds:
            title += f" ({fmt_hm(seconds)})"
        details = [f"Dauer: {fmt_hm(seconds)} h"]
        if sub:
            details.append(f"Unterkategorie: {sub.name}")
        if e.location:
            details.append(f"Ort: {LOCATION_LABELS.get(e.location, e.location)}")
        if e.flags:
            details.append("Flags: " + ", ".join(FLAG_LABELS.get(f, f) for f in e.flags))
        details.append(f"Quelle: {SOURCE_LABELS.get(e.source, e.source)}")
        if e.note:
            details.append(f"Notiz: {e.note}")
        lines += ["BEGIN:VEVENT", f"UID:{e.id}@deputat", f"DTSTAMP:{stamp}"]
        if e.has_bounds:
            lines += [f"DTSTART:{_ics_dt(e.start_at)}", f"DTEND:{_ics_dt(e.end_at)}"]
        else:
            nxt = date.fromordinal(e.day.toordinal() + 1)
            lines += [f"DTSTART;VALUE=DATE:{e.day.strftime('%Y%m%d')}", f"DTEND;VALUE=DATE:{nxt.strftime('%Y%m%d')}"]
        lines += [
            f"SUMMARY:{_ics_text(title)}",
            f"DESCRIPTION:{_ics_text(chr(10).join(details))}",
            f"CATEGORIES:{_ics_text(cat.code if cat else '')}",
            "TRANSP:TRANSPARENT",
            "CLASS:PRIVATE",
            f"LAST-MODIFIED:{_ics_dt(e.updated_at or e.created_at)}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    folded: list[str] = []
    for line in lines:
        folded.extend(_fold(line))
    return ("\r\n".join(folded) + "\r\n").encode("utf-8")
