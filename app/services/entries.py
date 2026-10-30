"""Time entries – the ONLY place that writes ``time_entry`` (Konzept §4 invariants, §6.3, §8.2/8.3).

Rules implemented here:

* **Duration / bounds.** Bounded entries get ``duration_seconds = end_at - start_at`` (whole seconds).
  Entries without bounds (``has_bounds=False``, "nur Dauer") start at local 00:00 of their day,
  end at ``start + duration`` (so ``end_at`` is never NULL for them) and carry ``ohne_zeitgrenzen``.
  A bounded entry without ``end_at`` and without a duration is a running timer (``source='timer'`` only).
* **Day.** ``day`` = local (Europe/Berlin) date of ``start_at``.
* **Midnight split.** A bounded entry crossing local midnight is stored as several entries
  (``core.timesplit.split_at_midnight``, half-open intervals → the sum stays exact). Every part gets
  a revision ``revision_no=0`` whose snapshot (``{"_event": "created", "split": {...}}``) records the
  original span and the ids of all parts – this links the parts without a schema change.
* **Overlaps.** Active, bounded, non-import entries must not overlap (half-open). A running timer only
  occupies the time that has passed so far (it is capped at the next entry when stopped). Entries without bounds and imported entries are exempt (both ways).
* **One running timer** at most (``end_at IS NULL``).
* **Flags.** ``nachtraeglich`` (created > 48 h after the day, ``core.is_nachtraeglich``) and ``korrigiert``
  are sticky: once set they can never be removed. ``wochenende``/``feiertag``/``ferienarbeit`` are derived
  from ``calendar_day`` (weekday / ``holiday`` as fallback) and ``ohne_zeitgrenzen`` from ``has_bounds``;
  they are recomputed on every write and ignored in input. All other flags are user flags.
  Changing the bounds of a timer entry adds ``korrigiert`` (Konzept §8.1), as does a correction in a
  closed month.
* **Revisions.** Every effective update/delete/restore appends a ``time_entry_revision`` with the full
  previous state (``revision_no`` = previous max + 1, ``change_reason``). Revision 0 is reserved for the
  creation note (split / created in a closed month).
* **Locks.** Writes touching a closed month (``month_close`` row exists, or ``entry.locked``) need a reason
  (otherwise :class:`EntryLocked`, HTTP 423). With a reason the change is applied, the month row is set to
  ``nachtraeglich_korrigiert``, its old hash is appended to ``hash_history`` and the new hash stored.
* **Month hash.** :func:`entry_hash_dict` maps an entry to the ``core.hashing`` fields and
  :func:`month_entries_hash` hashes all ``status='active'`` entries of a month *including soft-deleted
  ones* (``deleted_at`` is a hash field, so a deletion stays visible). The months service must reuse these.

All public write functions commit by default (``commit=False`` lets callers such as the import service
batch several writes in one transaction; they must commit themselves).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, false, func, or_, select, true
from sqlalchemy.orm import Session, object_session

from app.core.hashing import HASH_FIELDS, entries_hash
from app.core.timesplit import is_nachtraeglich, split_at_midnight
from app.core.types import DayType, HolidayScope
from app.db.models import (
    ENTRY_FLAGS,
    CalendarDay,
    Category,
    EntrySource,
    EntryStatus,
    Holiday,
    Location,
    MonthClose,
    MonthCloseStatus,
    Settings,
    TimeEntry,
    TimeEntryRevision,
)
from app.util import fmt_hm, local_date, local_midnight_utc, now_utc, to_local, to_utc_naive

MAX_PIECE_SECONDS = 24 * 3600
# Plausibility range for dates/datetimes: protects against overflow (year 1 / 9999) and typos.
MIN_YEAR = 2000
MAX_YEAR = 2099

FLAG_FERIENARBEIT = "ferienarbeit"
FLAG_WOCHENENDE = "wochenende"
FLAG_FEIERTAG = "feiertag"
FLAG_NACHTRAEGLICH = "nachtraeglich"
FLAG_KORRIGIERT = "korrigiert"
FLAG_OHNE_ZEITGRENZEN = "ohne_zeitgrenzen"

DERIVED_FLAGS = frozenset({FLAG_FERIENARBEIT, FLAG_WOCHENENDE, FLAG_FEIERTAG, FLAG_OHNE_ZEITGRENZEN})
STICKY_FLAGS = frozenset({FLAG_NACHTRAEGLICH, FLAG_KORRIGIERT})
USER_FLAGS = frozenset(ENTRY_FLAGS) - DERIVED_FLAGS - STICKY_FLAGS

CREATE_FIELDS = frozenset({
    "start_at", "end_at", "duration_seconds", "has_bounds", "day", "category_id", "subcategory_id",
    "klasse", "fach", "location", "note", "flags", "status", "import_batch_id", "timetable_slot_id",
})
UPDATE_FIELDS = CREATE_FIELDS - {"import_batch_id"}

SPLIT_REASON = "Automatisch an Mitternacht geteilt"
LOCKED_MESSAGE = "Monat abgeschlossen – Änderung nur mit Begründung"


# --------------------------------------------------------------------------- errors


class DomainError(Exception):
    """Base class for business rule violations; ``status_code`` is the HTTP status the API uses."""

    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class EntryValidationError(DomainError):
    status_code = 400


class EntryNotFound(DomainError):
    status_code = 404


class EntryConflict(DomainError):
    """Overlap, second running timer, already deleted, ..."""

    status_code = 409


class EntryLocked(DomainError):
    status_code = 423


# --------------------------------------------------------------------------- small helpers


def _naive_utc(value: Any, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise EntryValidationError(f"Ungültiger Zeitpunkt für {field}: {value!r}") from exc
    if isinstance(value, date) and not isinstance(value, datetime):
        raise EntryValidationError(f"Für {field} wird Datum und Uhrzeit erwartet.")
    if not isinstance(value, datetime):
        raise EntryValidationError(f"Ungültiger Zeitpunkt für {field}.")
    _check_year(value.year)
    return to_utc_naive(value).replace(microsecond=0)


def _check_year(year: int) -> None:
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise EntryValidationError(f"Das Datum muss zwischen {MIN_YEAR} und {MAX_YEAR} liegen.")


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        _check_year(value.year)
        return local_date(value)
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value.strip())
        except ValueError as exc:
            raise EntryValidationError(f"Ungültiges Datum: {value!r}") from exc
    if isinstance(value, date):
        _check_year(value.year)
        return value
    raise EntryValidationError("Ungültiges Datum.")


def _as_duration(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, timedelta):
        return int(value.total_seconds())
    if isinstance(value, bool):
        raise EntryValidationError("Ungültige Dauer.")
    if isinstance(value, (int, float)):
        return int(round(value))
    if isinstance(value, str):
        from app.core.parsing import parse_duration

        try:
            return parse_duration(value)
        except ValueError as exc:
            raise EntryValidationError(str(exc)) from exc
    raise EntryValidationError("Ungültige Dauer.")


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def year_month(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _month_bounds(ym: str) -> tuple[date, date]:
    try:
        y, m = (int(x) for x in ym.split("-"))
        start = date(y, m, 1)
    except (ValueError, TypeError) as exc:
        raise EntryValidationError(f"Ungültiger Monat: {ym!r} (erwartet JJJJ-MM)") from exc
    end = date(y + (m == 12), m % 12 + 1, 1)
    return start, end


def _iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _fmt_local(dt: datetime | None, fmt: str = "%H:%M") -> str:
    return to_local(dt).strftime(fmt) if dt is not None else "läuft"


def _get_settings(db: Session) -> Settings | None:
    return db.get(Settings, 1)


def _month_close(db: Session, d: date) -> MonthClose | None:
    return db.get(MonthClose, year_month(d))


# --------------------------------------------------------------------------- serialisation / hashing


def entry_snapshot(entry: TimeEntry) -> dict[str, Any]:
    """Full JSON-safe state of an entry (all columns) – used as revision snapshot."""
    result: dict[str, Any] = {}
    for column in TimeEntry.__table__.columns:
        value = getattr(entry, column.key)
        if isinstance(value, datetime):
            value = _iso_utc(value)
        elif isinstance(value, date):
            value = value.isoformat()
        elif isinstance(value, list):
            value = list(value)
        result[column.key] = value
    return result


def entry_hash_dict(entry: TimeEntry) -> dict[str, Any]:
    """Map an entry to the fields of ``core.hashing.HASH_FIELDS`` (raw values; the core normalises)."""
    return {field: getattr(entry, field) for field in HASH_FIELDS}


def month_hash_entries(db: Session, ym: str) -> list[TimeEntry]:
    """Entries that make up a month's hash: ``status='active'`` with ``day`` in the month, incl. soft-deleted."""
    start, end = _month_bounds(ym)
    return list(
        db.scalars(
            select(TimeEntry)
            .where(TimeEntry.day >= start, TimeEntry.day < end, TimeEntry.status == EntryStatus.ACTIVE.value)
            .order_by(TimeEntry.start_at, TimeEntry.id)
        )
    )


def month_entries_hash(db: Session, ym: str) -> str:
    """SHA-256 over the canonical form of :func:`month_hash_entries` (flushes pending changes first)."""
    db.flush()
    return entries_hash(entry_hash_dict(e) for e in month_hash_entries(db, ym))


def entry_to_dict(entry: TimeEntry) -> dict[str, Any]:
    """JSON representation for the API / UI (UTC ISO with ``Z`` plus local ISO strings)."""
    # Resolve by id: the relationship attributes can be stale after update_entry changed the ids in a
    # session with expire_on_commit=False.
    session = object_session(entry)
    if session is not None:
        category = session.get(Category, entry.category_id)
        subcategory = session.get(Category, entry.subcategory_id) if entry.subcategory_id else None
    else:
        category, subcategory = entry.category, entry.subcategory
    return {
        "id": entry.id,
        "day": entry.day.isoformat() if entry.day else None,
        "start_at": _iso_utc(entry.start_at),
        "end_at": _iso_utc(entry.end_at),
        "start_local": to_local(entry.start_at).isoformat() if entry.start_at else None,
        "end_local": to_local(entry.end_at).isoformat() if entry.end_at else None,
        "running": entry.end_at is None,
        "duration_seconds": entry.duration_seconds,
        "duration_hm": fmt_hm(entry.duration_seconds),
        "has_bounds": entry.has_bounds,
        "category_id": entry.category_id,
        "category_code": category.code if category else None,
        "category_name": category.name if category else None,
        "subcategory_id": entry.subcategory_id,
        "subcategory_name": subcategory.name if subcategory else None,
        "klasse": entry.klasse,
        "fach": entry.fach,
        "location": entry.location,
        "note": entry.note,
        "flags": list(entry.flags or []),
        "source": entry.source,
        "status": entry.status,
        "import_batch_id": entry.import_batch_id,
        "timetable_slot_id": entry.timetable_slot_id,
        "created_at": _iso_utc(entry.created_at),
        "updated_at": _iso_utc(entry.updated_at),
        "deleted_at": _iso_utc(entry.deleted_at),
        "locked": entry.locked,
    }


def revision_kind(revision: TimeEntryRevision) -> str:
    """'erstellt' (revision 0 creation note) | 'geaendert' | 'geloescht' | 'wiederhergestellt'."""
    snap = revision.snapshot_json or {}
    if snap.get("_event") == "created":
        return "erstellt"
    return {"delete": "geloescht", "restore": "wiederhergestellt"}.get(snap.get("_action"), "geaendert")


def revision_to_dict(revision: TimeEntryRevision) -> dict[str, Any]:
    return {
        "id": revision.id,
        "entry_id": revision.entry_id,
        "revision_no": revision.revision_no,
        "changed_at": _iso_utc(revision.changed_at),
        "change_reason": revision.change_reason,
        "kind": revision_kind(revision),
        "snapshot": revision.snapshot_json,
    }


# --------------------------------------------------------------------------- validation pieces


@dataclass
class _Times:
    has_bounds: bool
    start_at: datetime
    end_at: datetime | None  # None = running timer
    duration_seconds: int
    day: date

    @property
    def running(self) -> bool:
        return self.end_at is None


def _resolve_times(
    *,
    has_bounds: bool | None,
    start_at: datetime | None,
    end_at: datetime | None,
    duration: int | None,
    day: date | None,
    allow_running: bool,
    max_total: int | None,
) -> _Times:
    if has_bounds is None:
        has_bounds = start_at is not None

    if not has_bounds:
        if day is None and start_at is not None:
            day = local_date(start_at)
        if day is None:
            raise EntryValidationError("Datum fehlt.")
        if duration is None:
            raise EntryValidationError("Dauer fehlt.")
        if duration <= 0:
            raise EntryValidationError("Die Dauer muss größer als 0 sein.")
        if duration > MAX_PIECE_SECONDS:
            raise EntryValidationError("Ein Eintrag darf höchstens 24 Stunden dauern.")
        start = local_midnight_utc(day)
        return _Times(False, start, start + timedelta(seconds=duration), duration, day)

    if start_at is None:
        raise EntryValidationError("Startzeit fehlt.")
    if end_at is None and duration is not None:
        if duration <= 0:
            raise EntryValidationError("Die Dauer muss größer als 0 sein.")
        end_at = start_at + timedelta(seconds=duration)
    if end_at is None:
        if not allow_running:
            raise EntryValidationError("Endzeit oder Dauer fehlt.")
        return _Times(True, start_at, None, 0, local_date(start_at))
    if end_at <= start_at:
        raise EntryValidationError("Die Endzeit muss nach der Startzeit liegen.")
    total = int((end_at - start_at).total_seconds())
    if duration is not None and duration != total:
        raise EntryValidationError("Die Dauer passt nicht zu Start- und Endzeit.")
    if max_total is not None and total > max_total:
        raise EntryValidationError("Ein Eintrag darf höchstens 24 Stunden dauern.")
    return _Times(True, start_at, end_at, total, local_date(start_at))


def _pieces(times: _Times) -> list[tuple[datetime, datetime | None]]:
    if not times.has_bounds or times.running:
        return [(times.start_at, times.end_at)]
    pieces = split_at_midnight(times.start_at, times.end_at)
    for a, b in pieces:  # a piece never exceeds its local day; guard the 24 h rule anyway
        day_len = (local_midnight_utc(local_date(a) + timedelta(days=1)) - local_midnight_utc(local_date(a)))
        if (b - a).total_seconds() > max(MAX_PIECE_SECONDS, day_len.total_seconds()):
            raise EntryValidationError("Ein Eintrag darf je Tag höchstens 24 Stunden dauern.")
    return pieces


def _resolve_category(db: Session, category_id: Any, subcategory_id: Any, *, check_active: bool
                      ) -> tuple[Category, Category | None]:
    if category_id is None:
        raise EntryValidationError("Kategorie fehlt.")
    try:
        category = db.get(Category, int(category_id))
    except (TypeError, ValueError):
        category = None
    if category is None:
        raise EntryValidationError("Unbekannte Kategorie.")
    sub: Category | None = None
    if subcategory_id not in (None, ""):
        try:
            sub = db.get(Category, int(subcategory_id))
        except (TypeError, ValueError):
            sub = None
        if sub is None:
            raise EntryValidationError("Unbekannte Unterkategorie.")
    if category.parent_id is not None:  # a subcategory was picked directly
        if sub is not None and sub.id != category.id:
            raise EntryValidationError("Unterkategorie passt nicht zur Kategorie.")
        sub = category
        category = db.get(Category, category.parent_id)
    if sub is not None and sub.parent_id != category.id:
        raise EntryValidationError("Unterkategorie passt nicht zur Kategorie.")
    if check_active and (not category.active or (sub is not None and not sub.active)):
        raise EntryValidationError(f"Kategorie „{(sub or category).name}“ ist deaktiviert.")
    return category, sub


def _validate_attrs(db: Session, *, category: Category, klasse: str | None, location: str | None,
                    note: str | None, status: str, source: str) -> None:
    if category.requires_note and not note:
        raise EntryValidationError(f"Für die Kategorie „{category.name}“ ist eine Notiz Pflicht.")
    if klasse is not None:
        settings = _get_settings(db)
        allowed = list(settings.klassen or []) if settings else []
        if allowed and klasse not in allowed:
            raise EntryValidationError(f"Unbekannte Klasse „{klasse}“ (siehe Einstellungen).")
    if location is not None and location not in {loc.value for loc in Location}:
        raise EntryValidationError(f"Ungültiger Ort „{location}“.")
    if status not in {s.value for s in EntryStatus}:
        raise EntryValidationError(f"Ungültiger Status „{status}“.")
    if source not in {s.value for s in EntrySource}:
        raise EntryValidationError(f"Ungültige Quelle „{source}“.")


def _split_input_flags(flags: Any) -> tuple[set[str], set[str]]:
    """Return (user flags, sticky flags) from input; derived flags are dropped."""
    if flags is None:
        return set(), set()
    if isinstance(flags, str):
        flags = [flags]
    result = set()
    for flag in flags:
        flag = str(flag).strip()
        if flag not in ENTRY_FLAGS:
            raise EntryValidationError(f"Unbekanntes Kennzeichen „{flag}“.")
        result.add(flag)
    return result & USER_FLAGS, result & STICKY_FLAGS


SUBCATEGORY_KLASSENFAHRT = "Klassenfahrt"
SUBCATEGORY_VERTRETUNG = "Vertretung (gehalten)"
_VERTRETUNG_FLAGS = frozenset({"vertretung_angeordnet", "vertretung_genehmigt"})


def _subcategory_flags(category: Category, sub: Category | None, user_flags: set[str]) -> set[str]:
    """Flags implied by the subcategory (Konzept §5: "SV … Klassenfahrt → Flag").

    * SV › Klassenfahrt always carries ``klassenfahrt`` (otherwise Variante B would not cap the trip day).
    * Unterricht › Vertretung (gehalten) without a Vertretung flag defaults to ``vertretung_angeordnet``
      (counts as Mehrarbeit, like the day confirmation's default); ``genehmigt`` can be chosen instead.
    """
    if sub is None:
        return set()
    if sub.name == SUBCATEGORY_KLASSENFAHRT:
        return {"klassenfahrt"}
    if sub.name == SUBCATEGORY_VERTRETUNG and category.is_unterricht and not (user_flags & _VERTRETUNG_FLAGS):
        return {"vertretung_angeordnet"}
    return set()


def _derived_flags(db: Session, day: date, has_bounds: bool) -> set[str]:
    flags: set[str] = set()
    if not has_bounds:
        flags.add(FLAG_OHNE_ZEITGRENZEN)
    if day.isoweekday() >= 6:
        flags.add(FLAG_WOCHENENDE)
    cal = db.get(CalendarDay, day)
    if cal is not None:
        if cal.day_type == DayType.WOCHENENDE.value:
            flags.add(FLAG_WOCHENENDE)
        if cal.day_type == DayType.FEIERTAG.value:
            flags.add(FLAG_FEIERTAG)
        if cal.ferien_id is not None or cal.day_type == DayType.FERIENTAG.value:
            flags.add(FLAG_FERIENARBEIT)
    else:
        holiday = db.get(Holiday, day)
        if holiday is not None and holiday.scope in (HolidayScope.GESETZLICH.value, HolidayScope.REGIONAL.value):
            flags.add(FLAG_FEIERTAG)
    return flags


def refresh_derived_flags(db: Session, start: date, end: date) -> int:
    """Re-derive ``wochenende``/``feiertag``/``ferienarbeit``/``ohne_zeitgrenzen`` after a calendar change.

    The derived flags depend on ``calendar_day`` (ferien, holidays, overrides), which can change after an
    entry was written (e.g. ferien imported later). Only entries of OPEN months that are not locked are
    updated – flags are hash fields, so closed months keep exactly what was closed. No revision is written
    and ``updated_at`` is kept: the flags are derived values, not user data. Returns the number of entries
    changed (the caller commits).
    """
    db.flush()
    # Load the calendar rows of the range once (identity map), _derived_flags then needs no queries.
    list(db.scalars(select(CalendarDay).where(CalendarDay.date.between(start, end))))
    list(db.scalars(select(Holiday).where(Holiday.date.between(start, end))))
    closed = set(db.scalars(select(MonthClose.year_month)))
    changed = 0
    for entry in db.scalars(
        select(TimeEntry).where(
            TimeEntry.day.between(start, end), TimeEntry.deleted_at.is_(None), TimeEntry.locked.is_(False)
        )
    ):
        if year_month(entry.day) in closed:
            continue
        current = list(entry.flags or [])
        new = _ordered_flags((set(current) - DERIVED_FLAGS) | _derived_flags(db, entry.day, entry.has_bounds))
        if new != current:
            entry.flags = new
            changed += 1
    return changed


def _ordered_flags(flags: Iterable[str]) -> list[str]:
    present = set(flags)
    return [f for f in ENTRY_FLAGS if f in present]


def _check_overlap(db: Session, start: datetime, end: datetime | None, *, exclude: Iterable[str] = (),
                   now: datetime | None = None) -> None:
    """Reject ``[start, end)`` if it overlaps an active, bounded, non-import entry.

    A *running* timer (``end_at IS NULL``, also the new one when ``end is None``) only occupies the time
    that has already passed, ``[start_at, max(now, start_at + 1 s))``: a planned later entry (e.g. confirmed
    lessons or an Elternabend entered in advance) never blocks a timer start; the timer is capped at the next
    entry when it is stopped (:func:`next_entry_start`).
    """
    now = now or now_utc()
    if end is None:
        end = max(now, start + timedelta(seconds=1))
    query = select(TimeEntry).where(
        TimeEntry.deleted_at.is_(None),
        TimeEntry.status == EntryStatus.ACTIVE.value,
        TimeEntry.has_bounds.is_(True),
        TimeEntry.source != EntrySource.IMPORT.value,
        TimeEntry.start_at < end,
        or_(
            TimeEntry.end_at > start,
            and_(TimeEntry.end_at.is_(None), or_(TimeEntry.start_at >= start, true() if now > start else false())),
        ),
    )
    excluded = [x for x in exclude if x]
    if excluded:
        query = query.where(TimeEntry.id.not_in(excluded))
    other = db.scalars(query.order_by(TimeEntry.start_at).limit(1)).first()
    if other is not None:
        code = other.category.code if other.category else "?"
        until = _fmt_local(other.end_at) if other.end_at is not None else "läuft"
        raise EntryConflict(
            "Überschneidung mit einem bestehenden Eintrag am "
            f"{_fmt_local(other.start_at, '%d.%m.%Y')}, {_fmt_local(other.start_at)}–{until} ({code})."
        )


def next_entry_start(db: Session, after: datetime, before: datetime, *, exclude: Iterable[str] = ()
                     ) -> TimeEntry | None:
    """The first active, bounded, non-import entry starting in ``(after, before)`` (timer capping)."""
    query = select(TimeEntry).where(
        TimeEntry.deleted_at.is_(None),
        TimeEntry.status == EntryStatus.ACTIVE.value,
        TimeEntry.has_bounds.is_(True),
        TimeEntry.source != EntrySource.IMPORT.value,
        TimeEntry.end_at.is_not(None),
        TimeEntry.start_at > after,
        TimeEntry.start_at < before,
    )
    excluded = [x for x in exclude if x]
    if excluded:
        query = query.where(TimeEntry.id.not_in(excluded))
    return db.scalars(query.order_by(TimeEntry.start_at).limit(1)).first()


def _check_single_running(db: Session, *, exclude: str | None = None) -> None:
    query = select(TimeEntry.id).where(TimeEntry.end_at.is_(None), TimeEntry.deleted_at.is_(None))
    if exclude:
        query = query.where(TimeEntry.id != exclude)
    if db.scalars(query.limit(1)).first() is not None:
        raise EntryConflict("Es läuft bereits ein Timer. Bitte zuerst beenden.")


def get_running_entry(db: Session) -> TimeEntry | None:
    """The running timer entry (``end_at IS NULL``), if any."""
    return db.scalars(
        select(TimeEntry).where(TimeEntry.end_at.is_(None), TimeEntry.deleted_at.is_(None))
        .order_by(TimeEntry.start_at.desc()).limit(1)
    ).first()


def _next_revision_no(db: Session, entry_id: str) -> int:
    current = db.scalar(select(func.max(TimeEntryRevision.revision_no)).where(TimeEntryRevision.entry_id == entry_id))
    return (current or 0) + 1


def _add_revision(db: Session, entry: TimeEntry, snapshot: dict[str, Any], reason: str | None, now: datetime,
                  *, revision_no: int | None = None) -> TimeEntryRevision:
    revision = TimeEntryRevision(
        entry_id=entry.id,
        revision_no=_next_revision_no(db, entry.id) if revision_no is None else revision_no,
        changed_at=now,
        change_reason=reason,
        snapshot_json=snapshot,
    )
    db.add(revision)
    return revision


def _require_reason_if_locked(db: Session, days: Iterable[date], reason: str | None, *,
                              entry_locked: bool = False) -> set[str]:
    """Return the closed months among ``days``; raise :class:`EntryLocked` if any and no reason."""
    closed = {year_month(d) for d in days if _month_close(db, d) is not None}
    if (closed or entry_locked) and not reason:
        raise EntryLocked(LOCKED_MESSAGE)
    return closed


def _mark_months_corrected(db: Session, months: Iterable[str], reason: str | None, now: datetime) -> None:
    """Recompute the hash of closed months after a correction; keep the old hash in ``hash_history``."""
    db.flush()
    for ym in sorted(set(months)):
        mc = db.get(MonthClose, ym)
        if mc is None:
            continue
        new_hash = month_entries_hash(db, ym)
        if new_hash == mc.entries_hash_sha256:
            continue
        history = list(mc.hash_history or [])
        history.append({
            "hash": mc.entries_hash_sha256,
            # valid from: the close, or the previous correction that produced this hash
            "closed_at": _iso_utc(mc.corrected_at or mc.closed_at),
            "replaced_at": _iso_utc(now),
            "reason": reason,
        })
        mc.hash_history = history
        mc.entries_hash_sha256 = new_hash
        mc.status = MonthCloseStatus.NACHTRAEGLICH_KORRIGIERT.value
        mc.corrected_at = now


def _creation_note(original: _Times | None, part_ids: list[str], part_no: int, reason: str | None) -> dict:
    split = None
    if original is not None and len(part_ids) > 1:
        split = {
            "original_start_at": _iso_utc(original.start_at),
            "original_end_at": _iso_utc(original.end_at),
            "original_duration_seconds": original.duration_seconds,
            "parts": part_ids,
            "part_no": part_no,
        }
    return {"_event": "created", "split": split, "reason": reason}


def _creation_reason(split: bool, reason: str | None) -> str | None:
    if split and reason:
        return f"{reason} ({SPLIT_REASON})"
    return reason if reason else (SPLIT_REASON if split else None)


# --------------------------------------------------------------------------- public API: read


def get_entry(db: Session, entry_id: str, *, include_deleted: bool = True) -> TimeEntry:
    entry = db.get(TimeEntry, entry_id)
    if entry is None or (not include_deleted and entry.deleted_at is not None):
        raise EntryNotFound("Eintrag nicht gefunden.")
    return entry


def list_entries(
    db: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    category_id: int | None = None,
    source: str | None = None,
    status: str | None = None,
    flags: Iterable[str] | None = None,
    include_deleted: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[TimeEntry]:
    """Entries ordered by ``start_at``. ``date_from``/``date_to`` are inclusive local days.

    ``category_id`` matches the main category or the subcategory; ``flags`` requires ALL given flags;
    ``status=None`` returns every status.
    """
    query = select(TimeEntry)
    if date_from is not None:
        query = query.where(TimeEntry.day >= date_from)
    if date_to is not None:
        query = query.where(TimeEntry.day <= date_to)
    if category_id is not None:
        query = query.where(or_(TimeEntry.category_id == category_id, TimeEntry.subcategory_id == category_id))
    if source is not None:
        query = query.where(TimeEntry.source == source)
    if status is not None:
        query = query.where(TimeEntry.status == status)
    if not include_deleted:
        query = query.where(TimeEntry.deleted_at.is_(None))
    query = query.order_by(TimeEntry.start_at, TimeEntry.id)
    wanted = set(flags or [])
    if not wanted:
        if offset:
            query = query.offset(offset)
        if limit is not None:
            query = query.limit(limit)
        return list(db.scalars(query))
    rows = [e for e in db.scalars(query) if wanted <= set(e.flags or [])]
    rows = rows[offset:]
    return rows[:limit] if limit is not None else rows


def revisions(db: Session, entry_id: str) -> list[TimeEntryRevision]:
    get_entry(db, entry_id)
    return list(
        db.scalars(
            select(TimeEntryRevision)
            .where(TimeEntryRevision.entry_id == entry_id)
            .order_by(TimeEntryRevision.revision_no)
        )
    )


# --------------------------------------------------------------------------- public API: write


def create_entry(
    db: Session,
    data: Mapping[str, Any],
    *,
    source: str,
    reason: str | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> list[TimeEntry]:
    """Create an entry; returns all stored parts (more than one if it crossed local midnight).

    ``data`` keys: ``start_at``, ``end_at``, ``duration_seconds`` (int seconds or a duration string such as
    "2,5" / "2:30"), ``has_bounds``, ``day`` (for entries without bounds), ``category_id`` (a subcategory id
    is accepted and normalised), ``subcategory_id``, ``klasse``, ``fach``, ``location``, ``note``, ``flags``,
    ``status`` (default 'active'), ``import_batch_id``, ``timetable_slot_id``. Naive datetimes are UTC.
    ``reason`` is required when the entry lands in a closed month.
    """
    unknown = set(data) - CREATE_FIELDS
    if unknown:
        raise EntryValidationError(f"Unbekannte Felder: {', '.join(sorted(unknown))}")
    now = to_utc_naive(now) if now is not None else now_utc().replace(microsecond=0)
    reason = _clean_str(reason)
    source = str(source)

    times = _resolve_times(
        has_bounds=data.get("has_bounds"),
        start_at=_naive_utc(data.get("start_at"), "Start"),
        end_at=_naive_utc(data.get("end_at"), "Ende"),
        duration=_as_duration(data.get("duration_seconds")),
        day=_as_date(data.get("day")),
        allow_running=source == EntrySource.TIMER.value,
        max_total=None if source == EntrySource.TIMER.value else MAX_PIECE_SECONDS,
    )
    category, sub = _resolve_category(db, data.get("category_id"), data.get("subcategory_id"), check_active=True)
    klasse = _clean_str(data.get("klasse"))
    location = _clean_str(data.get("location"))
    note = _clean_str(data.get("note"))
    status = _clean_str(data.get("status")) or EntryStatus.ACTIVE.value
    _validate_attrs(db, category=category, klasse=klasse, location=location, note=note, status=status,
                    source=source)
    user_flags, sticky_flags = _split_input_flags(data.get("flags"))
    user_flags |= _subcategory_flags(category, sub, user_flags)

    pieces = _pieces(times)
    days = [local_date(a) if times.has_bounds else times.day for a, _ in pieces]
    closed = _require_reason_if_locked(db, days, reason)

    if times.running:
        _check_single_running(db)
    if status == EntryStatus.ACTIVE.value and times.has_bounds and source != EntrySource.IMPORT.value:
        for a, b in pieces:
            _check_overlap(db, a, b, now=now)

    created: list[TimeEntry] = []
    for (a, b), day in zip(pieces, days, strict=True):
        flags = set(user_flags) | sticky_flags | _derived_flags(db, day, times.has_bounds)
        if is_nachtraeglich(day, now):
            flags.add(FLAG_NACHTRAEGLICH)
        entry = TimeEntry(
            start_at=a,
            end_at=b,
            duration_seconds=int((b - a).total_seconds()) if b is not None else 0,
            has_bounds=times.has_bounds,
            day=day,
            category_id=category.id,
            subcategory_id=sub.id if sub else None,
            klasse=klasse,
            fach=_clean_str(data.get("fach")),
            location=location,
            note=note,
            flags=_ordered_flags(flags),
            source=source,
            status=status,
            import_batch_id=data.get("import_batch_id"),
            timetable_slot_id=data.get("timetable_slot_id"),
            created_at=now,
            updated_at=now,
            locked=year_month(day) in closed,
        )
        db.add(entry)
        created.append(entry)
    db.flush()

    split = len(created) > 1
    if split or reason:
        ids = [e.id for e in created]
        for no, entry in enumerate(created, start=1):
            _add_revision(db, entry, _creation_note(times, ids, no, reason), _creation_reason(split, reason), now,
                          revision_no=0)
    if closed:
        _mark_months_corrected(db, closed, reason, now)
    if commit:
        db.commit()
    return created


def update_entry(
    db: Session,
    entry_id: str,
    changes: Mapping[str, Any],
    *,
    reason: str | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> list[TimeEntry]:
    """Apply ``changes`` (same keys as :func:`create_entry`, partial); returns ``[entry, *new split parts]``.

    Appends a revision with the full previous state if anything changed. Bounds: a changed ``start_at`` keeps
    ``end_at``; ``duration_seconds`` without ``end_at`` sets ``end_at = start_at + duration``; switching to
    ``has_bounds=False`` needs ``day`` (or keeps the entry's day) and a duration.
    """
    entry = get_entry(db, entry_id)
    if entry.deleted_at is not None:
        raise EntryConflict("Gelöschte Einträge können nicht bearbeitet werden.")
    unknown = set(changes) - UPDATE_FIELDS
    if unknown:
        raise EntryValidationError(f"Nicht änderbare oder unbekannte Felder: {', '.join(sorted(unknown))}")
    now = to_utc_naive(now) if now is not None else now_utc().replace(microsecond=0)
    reason = _clean_str(reason)

    old_snapshot = entry_snapshot(entry)
    was_running = entry.end_at is None

    has_bounds = changes.get("has_bounds", entry.has_bounds)
    has_bounds = entry.has_bounds if has_bounds is None else bool(has_bounds)
    new_start = _naive_utc(changes["start_at"], "Start") if "start_at" in changes else None
    if has_bounds:
        start = new_start if "start_at" in changes else (entry.start_at if entry.has_bounds else None)
        duration = _as_duration(changes.get("duration_seconds"))
        if "end_at" in changes:
            end = _naive_utc(changes["end_at"], "Ende")
        elif duration is not None:
            end = None
        elif not entry.has_bounds:
            end, duration = None, entry.duration_seconds
        else:
            end = entry.end_at
        times = _resolve_times(has_bounds=True, start_at=start, end_at=end, duration=duration, day=None,
                               allow_running=entry.source == EntrySource.TIMER.value,
                               max_total=None if entry.source == EntrySource.TIMER.value else MAX_PIECE_SECONDS)
    else:
        day = _as_date(changes.get("day")) or (local_date(new_start) if new_start else None) or entry.day
        duration = (_as_duration(changes["duration_seconds"]) if "duration_seconds" in changes
                    else entry.duration_seconds)
        times = _resolve_times(has_bounds=False, start_at=None, end_at=None, duration=duration, day=day,
                               allow_running=False, max_total=MAX_PIECE_SECONDS)

    cat_changed = "category_id" in changes or "subcategory_id" in changes
    if "subcategory_id" in changes:
        sub_in = changes["subcategory_id"]
    elif "category_id" in changes and changes["category_id"] != entry.category_id:
        sub_in = None  # a new main category drops the old subcategory
    else:
        sub_in = entry.subcategory_id
    category, sub = _resolve_category(db, changes.get("category_id", entry.category_id), sub_in,
                                      check_active=cat_changed)
    klasse = _clean_str(changes["klasse"]) if "klasse" in changes else entry.klasse
    fach = _clean_str(changes["fach"]) if "fach" in changes else entry.fach
    location = _clean_str(changes["location"]) if "location" in changes else entry.location
    note = _clean_str(changes["note"]) if "note" in changes else entry.note
    status = (_clean_str(changes.get("status")) or entry.status) if "status" in changes else entry.status
    timetable_slot_id = changes["timetable_slot_id"] if "timetable_slot_id" in changes else entry.timetable_slot_id
    _validate_attrs(db, category=category, klasse=klasse if "klasse" in changes else None, location=location,
                    note=note, status=status, source=entry.source)

    current_flags = set(entry.flags or [])
    if "flags" in changes:
        user_flags, sticky_in = _split_input_flags(changes["flags"])
    else:
        user_flags, sticky_in = current_flags & USER_FLAGS, set()
    if sub is not None and (sub.id != entry.subcategory_id or sub.name == SUBCATEGORY_KLASSENFAHRT):
        user_flags |= _subcategory_flags(category, sub, user_flags)
    sticky = (current_flags & STICKY_FLAGS) | sticky_in

    pieces = _pieces(times)
    days = [local_date(a) if times.has_bounds else times.day for a, _ in pieces]

    bounds_changed = (times.start_at, times.end_at, times.has_bounds, times.duration_seconds) != (
        entry.start_at, entry.end_at, entry.has_bounds, entry.duration_seconds)
    if entry.source == EntrySource.TIMER.value and bounds_changed and not was_running:
        sticky.add(FLAG_KORRIGIERT)

    first_start, first_end = pieces[0]
    proposed = {
        "start_at": first_start,
        "end_at": first_end,
        "duration_seconds": int((first_end - first_start).total_seconds()) if first_end is not None else 0,
        "has_bounds": times.has_bounds,
        "day": days[0],
        "category_id": category.id,
        "subcategory_id": sub.id if sub else None,
        "klasse": klasse,
        "fach": fach,
        "location": location,
        "note": note,
        "status": status,
        "timetable_slot_id": timetable_slot_id,
    }
    # Moving an entry to another day records it for that day *now*: the 48-h rule is checked against the
    # time of the move as well, so "create early, move to a past day later" cannot avoid the flag.
    moved_day = days[0] != entry.day

    def flags_for(day: date, created_at: datetime) -> list[str]:
        flags = set(user_flags) | sticky | _derived_flags(db, day, times.has_bounds)
        if is_nachtraeglich(day, created_at) or (moved_day and is_nachtraeglich(day, now)):
            flags.add(FLAG_NACHTRAEGLICH)
        return _ordered_flags(flags)

    content_changed = len(pieces) > 1 or any(getattr(entry, k) != v for k, v in proposed.items())
    if not content_changed and flags_for(days[0], entry.created_at or now) == list(entry.flags or []):
        return [entry]  # nothing to do: no revision for a no-op

    closed = _require_reason_if_locked(db, [entry.day, *days], reason, entry_locked=entry.locked)
    if closed or entry.locked:
        sticky.add(FLAG_KORRIGIERT)
    new_flags = flags_for(days[0], entry.created_at or now)

    if times.running:
        _check_single_running(db, exclude=entry.id)
    if status == EntryStatus.ACTIVE.value and times.has_bounds and entry.source != EntrySource.IMPORT.value:
        for a, b in pieces:
            _check_overlap(db, a, b, exclude=[entry.id], now=now)

    old_snapshot["_action"] = "update"
    _add_revision(db, entry, old_snapshot, reason, now)
    for key, value in proposed.items():
        setattr(entry, key, value)
    entry.flags = new_flags
    entry.updated_at = now
    entry.locked = _month_close(db, days[0]) is not None

    result = [entry]
    for (a, b), day in zip(pieces[1:], days[1:], strict=True):
        part = TimeEntry(
            start_at=a, end_at=b, duration_seconds=int((b - a).total_seconds()), has_bounds=True, day=day,
            category_id=category.id, subcategory_id=sub.id if sub else None, klasse=klasse, fach=fach,
            location=location, note=note, flags=flags_for(day, now), source=entry.source, status=status,
            import_batch_id=entry.import_batch_id, timetable_slot_id=timetable_slot_id,
            created_at=now, updated_at=now, locked=_month_close(db, day) is not None,
        )
        db.add(part)
        result.append(part)
    db.flush()
    if len(result) > 1:
        ids = [e.id for e in result]
        for no, part in enumerate(result[1:], start=2):
            _add_revision(db, part, _creation_note(times, ids, no, reason), _creation_reason(True, reason), now,
                          revision_no=0)

    _mark_months_corrected(db, closed, reason, now)
    if commit:
        db.commit()
    return result


def delete_entry(
    db: Session,
    entry_id: str,
    *,
    reason: str | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> TimeEntry:
    """Soft delete (``deleted_at``) with a revision of the previous state."""
    entry = get_entry(db, entry_id)
    if entry.deleted_at is not None:
        raise EntryConflict("Eintrag ist bereits gelöscht.")
    now = to_utc_naive(now) if now is not None else now_utc().replace(microsecond=0)
    reason = _clean_str(reason)
    closed = _require_reason_if_locked(db, [entry.day], reason, entry_locked=entry.locked)
    snapshot = entry_snapshot(entry)
    snapshot["_action"] = "delete"
    _add_revision(db, entry, snapshot, reason, now)
    entry.deleted_at = now
    entry.updated_at = now
    _mark_months_corrected(db, closed, reason, now)
    if commit:
        db.commit()
    return entry


def restore_entry(
    db: Session,
    entry_id: str,
    *,
    reason: str | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> TimeEntry:
    """Undo a soft delete (overlap/lock rules apply again)."""
    entry = get_entry(db, entry_id)
    if entry.deleted_at is None:
        raise EntryConflict("Eintrag ist nicht gelöscht.")
    now = to_utc_naive(now) if now is not None else now_utc().replace(microsecond=0)
    reason = _clean_str(reason)
    closed = _require_reason_if_locked(db, [entry.day], reason, entry_locked=entry.locked)
    if entry.end_at is None:
        _check_single_running(db, exclude=entry.id)
    if entry.status == EntryStatus.ACTIVE.value and entry.has_bounds and entry.source != EntrySource.IMPORT.value:
        _check_overlap(db, entry.start_at, entry.end_at, exclude=[entry.id], now=now)
    snapshot = entry_snapshot(entry)
    snapshot["_action"] = "restore"
    _add_revision(db, entry, snapshot, reason, now)
    entry.deleted_at = None
    entry.updated_at = now
    _mark_months_corrected(db, closed, reason, now)
    if commit:
        db.commit()
    return entry


def finish_running_entry(
    db: Session,
    end_at: datetime,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> list[TimeEntry]:
    """Stop the running timer entry at ``end_at`` (used by the timer service).

    Completing a timer is not an edit, so no revision is written. If the timer crossed local midnight the
    entry is split (the running entry keeps the first part, revision 0 links the parts). A zero-length
    timer is discarded (soft delete with reason). Returns the stored parts (empty if discarded).
    """
    entry = get_running_entry(db)
    if entry is None:
        raise EntryConflict("Es läuft kein Timer.")
    now = to_utc_naive(now) if now is not None else now_utc().replace(microsecond=0)
    end = _naive_utc(end_at, "Ende")
    if end < entry.start_at:
        raise EntryValidationError("Das Ende liegt vor dem Start des Timers.")
    if end == entry.start_at:
        delete_entry(db, entry.id, reason="Timer ohne Dauer verworfen", now=now, commit=commit)
        return []

    times = _Times(True, entry.start_at, end, int((end - entry.start_at).total_seconds()), entry.day)
    pieces = _pieces(times)
    days = [local_date(a) for a, _ in pieces]
    _require_reason_if_locked(db, days, None, entry_locked=entry.locked)
    if entry.status == EntryStatus.ACTIVE.value:
        for a, b in pieces:
            _check_overlap(db, a, b, exclude=[entry.id], now=now)

    user_flags = set(entry.flags or []) & (USER_FLAGS | STICKY_FLAGS)
    first_start, first_end = pieces[0]
    entry.end_at = first_end
    entry.duration_seconds = int((first_end - first_start).total_seconds())
    entry.updated_at = now
    result = [entry]
    for (a, b), day in zip(pieces[1:], days[1:], strict=True):
        flags = (user_flags - {FLAG_NACHTRAEGLICH}) | _derived_flags(db, day, True)
        if is_nachtraeglich(day, now):
            flags.add(FLAG_NACHTRAEGLICH)
        part = TimeEntry(
            start_at=a, end_at=b, duration_seconds=int((b - a).total_seconds()), has_bounds=True, day=day,
            category_id=entry.category_id, subcategory_id=entry.subcategory_id, klasse=entry.klasse,
            fach=entry.fach, location=entry.location, note=entry.note, flags=_ordered_flags(flags),
            source=entry.source, status=entry.status, created_at=now, updated_at=now,
        )
        db.add(part)
        result.append(part)
    db.flush()
    if len(result) > 1:
        ids = [e.id for e in result]
        for no, part in enumerate(result, start=1):
            has_zero = db.scalar(select(TimeEntryRevision.id).where(
                TimeEntryRevision.entry_id == part.id, TimeEntryRevision.revision_no == 0))
            note = _creation_note(times, ids, no, None)
            # Revision 0 already taken (entry created with a reason) -> append as a normal revision.
            _add_revision(db, part, note, SPLIT_REASON, now, revision_no=None if has_zero else 0)
    if commit:
        db.commit()
    return result
