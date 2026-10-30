"""Timetable (Stundenplan) versions and slots, timetable suggestions and day confirmation
(Konzept §4, §6.3, §7.2).

Versions
--------
A :class:`~app.db.models.Timetable` is valid from ``valid_from`` to ``valid_to`` (inclusive, ``None`` =
open-ended). Versions never overlap. ``create_timetable(..., close_previous=True)`` and
:func:`copy_timetable` (Halbjahreswechsel) end the version that is valid before the new start on the
day before. Slots belong to a version: ``weekday`` 1–5, ``slot_no`` 0–99 (unique per weekday; pause
supervisions use a number of their own, e.g. 0 for Frühaufsicht), times from ``settings.zeitraster`` by
``slot_no`` unless given explicitly, ``kind`` (unterricht | aufsicht | sprechstunde | konferenz_fix | frei),
``klasse``, ``fach`` and a category (default U / AV / E / KO by kind; ``frei`` has none). Non-free slots of
one weekday must not overlap in time.

Suggestions
-----------
:func:`generate_suggestions` creates, for a school day (``calendar_day.day_type='schultag'`` without an
absence), one ``time_entry`` per non-free slot of the valid version with ``source='stundenplan'``,
``status='vorschlag'`` (does not count) and ``timetable_slot_id`` – always through
:mod:`app.services.entries`. It is idempotent: a slot that already has a (non-deleted) entry that day is
skipped, whatever its status. Unconfirmed suggestions that no longer fit (slot removed, version changed,
the day became a non-school day) are soft-deleted with a reason. Confirmed days and closed months are never
touched by the generator.

Große Pausen (§6.3): gaps between lessons are not suggested – unless ``settings.grosse_pause_zaehlt`` is set;
then every gap of at most :data:`PAUSE_MAX_MINUTES` between two consecutive suggested slots becomes an
AV suggestion "Große Pause (Präsenz)" (``timetable_slot_id`` NULL, identified by its start time). A
supervision during the break is an ``aufsicht`` slot and therefore always suggested.

A suggestion that overlaps an active manual/timer entry is still created; :func:`suggestion_conflicts`
reports the overlap (also part of the generate result and the day view) so the UI can resolve it with a
deviation (``entfallen`` or ``zeit_angepasst``) before confirming.

Confirmation
------------
:func:`confirm_day` takes the deviations per slot (or per suggestion entry id):

* ``wie_geplant`` (default for every slot without deviation): the entry becomes ``active`` as planned.
* ``entfallen``: ``status='verworfen'``.
* ``vertretung``: held as Vertretung (``klasse``, optional ``fach``/times, ``art`` = ``angeordnet`` (default)
  | ``genehmigt`` → flag ``vertretung_angeordnet`` / ``vertretung_genehmigt``), category U with the
  subcategory "Vertretung (gehalten)". Also allowed on a ``frei`` slot: then a new U entry is created.
* ``exkursion``: category SV (or ``category_id``), optional changed times.
* ``zeit_angepasst``: ``start``/``end`` (local ``HH:MM``) replace the planned times.

Every status/field change goes through :func:`app.services.entries.update_entry` and therefore leaves a
revision. ``extra`` adds Vertretungen that were not in the plan (U entries with the Vertretung flag,
``source='manuell'``); they are additive and idempotent (an identical entry is reused).

The deviations given on a (re-)confirmation are the complete description of the day: a slot without
deviation is (back) "wie geplant". Re-confirming with the same input changes nothing (no revisions). The
planned state of every slot is stored in ``day_confirmation.deviations_json`` on the first confirmation, so a
later re-confirmation can revert a deviation even if the slot was edited meanwhile. ``deviations_json``::

    {"timetable_id": ..., "items": {key: {"entry_id", "slot_id", "kind", "label", "planned", "deviation",
     "status"}}, "extra": [{"entry_id", "start", "end", "klasse", "fach", "art"}], "history": [iso, ...]}

``key`` is the slot id, or ``"entry:<id>"`` for pause suggestions and entries whose slot was deleted.

Concurrency: generation and confirmation run under a process-wide lock (the app runs one process), so the
scheduler job and a request opening the day cannot create duplicates.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.types import FLAG_VERTRETUNG_ANGEORDNET, FLAG_VERTRETUNG_GENEHMIGT, AbsenceType, DayType
from app.db.models import (
    CalendarDay,
    Category,
    DayConfirmation,
    EntrySource,
    EntryStatus,
    Location,
    MonthClose,
    Period,
    Settings,
    SlotKind,
    TimeEntry,
    Timetable,
    TimetableSlot,
)
from app.services import entries as entries_svc
from app.util import LOCAL_TZ, fmt_hm, local_to_utc, now_utc, to_local, today_local

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- constants

DEFAULT_CATEGORY_CODES: dict[str, str] = {
    SlotKind.UNTERRICHT.value: "U",
    SlotKind.AUFSICHT.value: "AV",
    SlotKind.SPRECHSTUNDE.value: "E",
    SlotKind.KONFERENZ_FIX.value: "KO",
}
KIND_LABELS = {
    SlotKind.UNTERRICHT.value: "Unterricht",
    SlotKind.AUFSICHT.value: "Aufsicht",
    SlotKind.SPRECHSTUNDE.value: "Sprechstunde",
    SlotKind.KONFERENZ_FIX.value: "Konferenz",
    SlotKind.FREI.value: "frei",
}
WEEKDAY_NAMES = {1: "Montag", 2: "Dienstag", 3: "Mittwoch", 4: "Donnerstag", 5: "Freitag", 6: "Samstag",
                 7: "Sonntag"}
DAY_TYPE_LABELS = {
    DayType.FERIENTAG.value: "Ferientag",
    DayType.UNTERRICHTSFREI.value: "unterrichtsfreier Tag",
    DayType.FEIERTAG.value: "Feiertag",
    DayType.WOCHENENDE.value: "Wochenende",
}
ABSENCE_LABELS = {
    AbsenceType.URLAUB.value: "Urlaub",
    AbsenceType.KRANK.value: "krank",
    AbsenceType.FORTBILDUNG.value: "Fortbildung",
    AbsenceType.DIENSTBEFREIUNG.value: "Dienstbefreiung",
    AbsenceType.SONDERURLAUB.value: "Sonderurlaub",
    AbsenceType.ELTERNZEIT.value: "Elternzeit",
}

MAX_SLOT_NO = 99
PAUSE_MAX_MINUTES = 30
PAUSE_NOTE = "Große Pause (Präsenz)"
PAUSE_CATEGORY_CODE = "AV"
VERTRETUNG_CATEGORY_CODE = "U"
VERTRETUNG_SUBCATEGORY_NAME = "Vertretung (gehalten)"
EXKURSION_CATEGORY_CODE = "SV"

DEVIATION_WIE_GEPLANT = "wie_geplant"
DEVIATION_ENTFALLEN = "entfallen"
DEVIATION_VERTRETUNG = "vertretung"
DEVIATION_EXKURSION = "exkursion"
DEVIATION_ZEIT = "zeit_angepasst"
DEVIATION_TYPES = (DEVIATION_WIE_GEPLANT, DEVIATION_ENTFALLEN, DEVIATION_VERTRETUNG, DEVIATION_EXKURSION,
                   DEVIATION_ZEIT)
DEVIATION_LABELS = {
    DEVIATION_WIE_GEPLANT: "wie geplant",
    DEVIATION_ENTFALLEN: "entfallen",
    DEVIATION_VERTRETUNG: "Vertretung gehalten",
    DEVIATION_EXKURSION: "Exkursion",
    DEVIATION_ZEIT: "Zeit angepasst",
}
VERTRETUNG_FLAGS = {"angeordnet": FLAG_VERTRETUNG_ANGEORDNET, "genehmigt": FLAG_VERTRETUNG_GENEHMIGT}
ALL_VERTRETUNG_FLAGS = frozenset(VERTRETUNG_FLAGS.values())

RETIRE_REASON = "Stundenplan-Vorschlag nicht mehr gültig"
UNLINK_REASON = "Stundenplan-Stunde gelöscht – Verknüpfung entfernt"
CONFIRM_HISTORY_MAX = 20
SCHEDULER_JOB_ID = "timetable_suggestions"

_LOCK = threading.RLock()
_HHMM = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


# --------------------------------------------------------------------------- errors


class TimetableError(entries_svc.DomainError):
    """Invalid timetable/confirmation input (German message)."""

    status_code = 422


class TimetableNotFound(entries_svc.DomainError):
    status_code = 404


class TimetableConflict(entries_svc.DomainError):
    """Overlapping versions/slots, or a suggestion that collides with an existing entry on confirm."""

    status_code = 409


# --------------------------------------------------------------------------- small helpers


def _fmt_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def _hm(t: time) -> str:
    return t.strftime("%H:%M") if t.second == 0 else t.strftime("%H:%M:%S")


def _clean(value: Any, *, max_len: int | None = None, field_label: str = "") -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if max_len is not None and len(text) > max_len:
        raise TimetableError(f"{field_label} ist zu lang (höchstens {max_len} Zeichen).")
    return text


def _parse_date(value: Any, label: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise TimetableError(f"{label}: Datum im Format JJJJ-MM-TT erwartet.") from None


def _parse_time(value: Any, label: str) -> time | None:
    """``time``, ``"HH:MM"``/``"HH:MM:SS"`` or a datetime (converted to local time)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return to_local(value).time().replace(microsecond=0, tzinfo=None)
    if isinstance(value, time):
        return value.replace(microsecond=0, tzinfo=None)
    text = str(value).strip()
    match = _HHMM.match(text)
    if match:
        h, m, s = int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)
        if h < 24 and m < 60 and s < 60:
            return time(h, m, s)
    elif "T" in text:
        try:
            return _parse_time(datetime.fromisoformat(text), label)
        except ValueError:
            pass
    raise TimetableError(f"{label}: Uhrzeit im Format HH:MM erwartet.")


def _settings(db: Session) -> Settings:
    settings = db.get(Settings, 1)
    if settings is None:  # pragma: no cover - seed always creates it
        raise TimetableError("Einstellungen fehlen.")
    return settings


def _categories_by_code(db: Session) -> dict[str, Category]:
    return {c.code: c for c in db.scalars(select(Category))}


def _vertretung_subcategory(db: Session, parent: Category) -> Category | None:
    return db.scalars(
        select(Category).where(Category.parent_id == parent.id, Category.name == VERTRETUNG_SUBCATEGORY_NAME)
    ).first()


def _main_category_id(db: Session, category_id: int | None) -> tuple[int | None, int | None]:
    """(main id, subcategory id) for a category id that may point to a subcategory."""
    if category_id is None:
        return None, None
    cat = db.get(Category, category_id)
    if cat is not None and cat.parent_id is not None:
        return cat.parent_id, cat.id
    return category_id, None


def _month_closed(db: Session, d: date) -> bool:
    return db.get(MonthClose, entries_svc.year_month(d)) is not None


def _iso(dt: datetime | None) -> str | None:
    return dt.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z") if dt is not None else None


# =========================================================================== versions


def list_timetables(db: Session) -> list[Timetable]:
    return list(db.scalars(select(Timetable).order_by(Timetable.valid_from)))


def get_timetable(db: Session, timetable_id: str) -> Timetable:
    tt = db.get(Timetable, timetable_id)
    if tt is None:
        raise TimetableNotFound("Stundenplan nicht gefunden.")
    return tt


def timetable_for_date(db: Session, d: date) -> Timetable | None:
    """The version valid on ``d`` (``valid_from <= d <= valid_to``; open end allowed)."""
    return db.scalars(
        select(Timetable)
        .where(Timetable.valid_from <= d, (Timetable.valid_to.is_(None)) | (Timetable.valid_to >= d))
        .order_by(Timetable.valid_from.desc())
        .limit(1)
    ).first()


def resolve_timetable(db: Session, ref: str | None, today: date | None = None) -> Timetable:
    """Version by id or ``'current'`` (the one valid today)."""
    if ref in (None, "", "current"):
        tt = timetable_for_date(db, today or today_local())
        if tt is None:
            raise TimetableNotFound("Für heute gilt kein Stundenplan.")
        return tt
    return get_timetable(db, ref)


def _range_text(tt: Timetable) -> str:
    end = _fmt_date(tt.valid_to) if tt.valid_to else "offen"
    return f"{_fmt_date(tt.valid_from)}–{end}"


def _overlapping(db: Session, valid_from: date, valid_to: date | None, exclude_id: str | None) -> list[Timetable]:
    query = select(Timetable).where((Timetable.valid_to.is_(None)) | (Timetable.valid_to >= valid_from))
    if valid_to is not None:
        query = query.where(Timetable.valid_from <= valid_to)
    if exclude_id:
        query = query.where(Timetable.id != exclude_id)
    return list(db.scalars(query.order_by(Timetable.valid_from)))


def _ensure_no_overlap(db: Session, valid_from: date, valid_to: date | None, *, exclude_id: str | None = None,
                       close_previous: bool = False) -> None:
    if valid_to is not None and valid_to < valid_from:
        raise TimetableError("„Gültig bis“ liegt vor „Gültig ab“.")
    db.flush()
    others = _overlapping(db, valid_from, valid_to, exclude_id)
    if not others:
        return
    if close_previous and all(o.valid_from < valid_from for o in others):
        for other in others:
            other.valid_to = valid_from - timedelta(days=1)
        return
    other = others[0]
    raise TimetableConflict(
        f"Der Gültigkeitszeitraum überschneidet sich mit „{other.label}“ ({_range_text(other)})."
    )


def _default_label(valid_from: date) -> str:
    return f"Stundenplan ab {_fmt_date(valid_from)}"


def create_timetable(
    db: Session,
    *,
    valid_from: date | str,
    valid_to: date | str | None = None,
    label: str | None = None,
    slots: Iterable[Mapping[str, Any]] | None = None,
    close_previous: bool = False,
    commit: bool = True,
) -> Timetable:
    """Create a version (optionally with slots). ``close_previous`` ends the version valid before."""
    start = _parse_date(valid_from, "Gültig ab")
    if start is None:
        raise TimetableError("„Gültig ab“ fehlt.")
    end = _parse_date(valid_to, "Gültig bis")
    _ensure_no_overlap(db, start, end, close_previous=close_previous)
    tt = Timetable(
        label=_clean(label, max_len=100, field_label="Bezeichnung") or _default_label(start),
        valid_from=start,
        valid_to=end,
    )
    db.add(tt)
    db.flush()
    if slots is not None:
        set_slots(db, tt.id, slots, commit=False)
    if commit:
        db.commit()
    return tt


def update_timetable(db: Session, timetable_id: str, changes: Mapping[str, Any], *, commit: bool = True
                     ) -> Timetable:
    """Change ``label``, ``valid_from`` and/or ``valid_to`` (``None`` = open-ended)."""
    unknown = set(changes) - {"label", "valid_from", "valid_to", "close_previous"}
    if unknown:
        raise TimetableError(f"Unbekannte Felder: {', '.join(sorted(unknown))}")
    tt = get_timetable(db, timetable_id)
    start = _parse_date(changes["valid_from"], "Gültig ab") if "valid_from" in changes else tt.valid_from
    if start is None:
        raise TimetableError("„Gültig ab“ fehlt.")
    end = _parse_date(changes["valid_to"], "Gültig bis") if "valid_to" in changes else tt.valid_to
    if (start, end) != (tt.valid_from, tt.valid_to):
        _ensure_no_overlap(db, start, end, exclude_id=tt.id, close_previous=bool(changes.get("close_previous")))
        tt.valid_from, tt.valid_to = start, end
    if "label" in changes:
        tt.label = _clean(changes["label"], max_len=100, field_label="Bezeichnung") or _default_label(start)
    if commit:
        db.commit()
    return tt


def _retire_suggestions_of_slots(db: Session, slot_ids: Iterable[str], now: datetime) -> int:
    """Soft-delete unconfirmed suggestions linked to the slots (closed months are left alone)."""
    ids = [s for s in slot_ids if s]
    if not ids:
        return 0
    rows = db.scalars(
        select(TimeEntry).where(
            TimeEntry.timetable_slot_id.in_(ids),
            TimeEntry.status == EntryStatus.VORSCHLAG.value,
            TimeEntry.deleted_at.is_(None),
        )
    ).all()
    count = 0
    for entry in rows:
        if _month_closed(db, entry.day) or entry.locked:
            continue
        entries_svc.delete_entry(db, entry.id, reason=RETIRE_REASON, now=now, commit=False)
        count += 1
    return count


def _unlink_entries_of_slots(db: Session, slot_ids: Iterable[str], now: datetime) -> int:
    """Clear ``timetable_slot_id`` of the (confirmed) entries of slots about to be deleted – through the
    entries service, so every change leaves a revision (the FK's ON DELETE SET NULL would do it silently).

    Entries in closed months are never rewritten: deleting such a slot is refused (end the version with
    ``valid_to`` instead), so the revision chain of locked entries stays complete.
    """
    slot_ids = list(slot_ids)
    if not slot_ids:
        return 0
    rows = db.scalars(
        select(TimeEntry).where(TimeEntry.timetable_slot_id.in_(slot_ids), TimeEntry.deleted_at.is_(None))
    ).all()
    blocked = sorted({e.day for e in rows if e.locked or _month_closed(db, e.day)})
    if blocked:
        raise TimetableConflict(
            "Diese Stunden sind mit bestätigten Einträgen in abgeschlossenen Monaten verknüpft "
            f"(z. B. {blocked[0]:%d.%m.%Y}) und können nicht gelöscht werden. "
            "Bitte stattdessen die Stundenplan-Version mit „gültig bis“ beenden."
        )
    for entry in rows:
        entries_svc.update_entry(db, entry.id, {"timetable_slot_id": None}, reason=UNLINK_REASON, now=now,
                                 commit=False)
    return len(rows)


def delete_timetable(db: Session, timetable_id: str, *, now: datetime | None = None, commit: bool = True) -> None:
    """Delete a version and its slots. Unconfirmed suggestions are retired; confirmed entries keep their data
    and lose the slot link through the entries service (revision); refused for entries in closed months."""
    tt = get_timetable(db, timetable_id)
    _retire_suggestions_of_slots(db, [s.id for s in tt.slots], now or now_utc())
    _unlink_entries_of_slots(db, [s.id for s in tt.slots], now or now_utc())
    db.delete(tt)
    db.flush()
    if commit:
        db.commit()
    db.expire_all()  # the FK set timetable_slot_id NULL in the database


def copy_timetable(
    db: Session,
    timetable_id: str,
    *,
    valid_from: date | str,
    valid_to: date | str | None = None,
    label: str | None = None,
    close_source: bool = True,
    commit: bool = True,
) -> Timetable:
    """Copy a version with all slots (Halbjahreswechsel). With ``close_source`` the version valid before
    ``valid_from`` is ended on the day before."""
    source = get_timetable(db, timetable_id)
    start = _parse_date(valid_from, "Gültig ab")
    if start is None:
        raise TimetableError("„Gültig ab“ fehlt.")
    slots = [slot_input(s) for s in source.slots]
    return create_timetable(
        db, valid_from=start, valid_to=valid_to, label=label or _default_label(start), slots=slots,
        close_previous=close_source, commit=commit,
    )


# =========================================================================== slots


def slot_input(slot: TimetableSlot) -> dict[str, Any]:
    """The slot as input mapping (for copies)."""
    return {
        "weekday": slot.weekday,
        "slot_no": slot.slot_no,
        "start_time": slot.start_time,
        "end_time": slot.end_time,
        "kind": slot.kind,
        "klasse": slot.klasse,
        "fach": slot.fach,
        "category_id": slot.category_id,
    }


def _zeitraster_times(settings: Settings, slot_no: int) -> tuple[time, time] | None:
    for item in settings.zeitraster or []:
        try:
            if int(item.get("slot_no")) == slot_no:
                start = _parse_time(item.get("start"), "Zeitraster")
                end = _parse_time(item.get("end"), "Zeitraster")
                if start and end:
                    return start, end
        except (TypeError, ValueError, TimetableError):
            continue
    return None


SLOT_FIELDS = frozenset({"weekday", "slot_no", "start_time", "end_time", "kind", "klasse", "fach", "category_id"})


def _slot_values(db: Session, settings: Settings, data: Mapping[str, Any], current: TimetableSlot | None
                 ) -> dict[str, Any]:
    """Validate slot input (full for a new slot, partial for an update) and return the column values."""
    unknown = set(data) - SLOT_FIELDS
    if unknown:
        raise TimetableError(f"Unbekannte Felder: {', '.join(sorted(unknown))}")

    def pick(key: str) -> Any:
        return data[key] if key in data else (getattr(current, key) if current is not None else None)

    try:
        weekday = int(pick("weekday"))
    except (TypeError, ValueError):
        raise TimetableError("Wochentag fehlt (1 = Montag … 5 = Freitag).") from None
    if not 1 <= weekday <= 5:
        raise TimetableError("Wochentag muss zwischen 1 (Montag) und 5 (Freitag) liegen.")
    try:
        slot_no = int(pick("slot_no"))
    except (TypeError, ValueError):
        raise TimetableError("Stundennummer fehlt.") from None
    if not 0 <= slot_no <= MAX_SLOT_NO:
        raise TimetableError(f"Stundennummer muss zwischen 0 und {MAX_SLOT_NO} liegen.")

    kind = str(pick("kind") or SlotKind.UNTERRICHT.value).strip()
    if kind not in KIND_LABELS:
        raise TimetableError(f"Unbekannte Art „{kind}“ (unterricht, aufsicht, sprechstunde, konferenz_fix, frei).")

    start = _parse_time(data.get("start_time"), "Beginn") if "start_time" in data else None
    end = _parse_time(data.get("end_time"), "Ende") if "end_time" in data else None
    if start is None or end is None:
        raster = _zeitraster_times(settings, slot_no)
        slot_no_changed = current is not None and "slot_no" in data and slot_no != current.slot_no
        if current is not None and not slot_no_changed:
            start = start or current.start_time
            end = end or current.end_time
        elif raster is not None:
            start = start or raster[0]
            end = end or raster[1]
        elif current is not None:
            start = start or current.start_time
            end = end or current.end_time
    if start is None or end is None:
        raise TimetableError(
            f"Für Stunde {slot_no} gibt es kein Zeitraster – bitte Beginn und Ende angeben."
        )
    if end <= start:
        raise TimetableError(f"Stunde {slot_no}: Das Ende muss nach dem Beginn liegen.")

    klasse = _clean(pick("klasse"), max_len=16, field_label="Klasse")
    if klasse is not None and "klasse" in data:
        allowed = list(settings.klassen or [])
        if allowed and klasse not in allowed:
            raise TimetableError(f"Unbekannte Klasse „{klasse}“ (siehe Einstellungen).")
    fach = _clean(pick("fach"), max_len=64, field_label="Fach")

    if "category_id" in data:
        category_id = data["category_id"]
    elif current is not None and "kind" in data and kind != current.kind:
        category_id = None  # new kind -> its default category
    else:
        category_id = current.category_id if current is not None else None
    if kind == SlotKind.FREI.value:
        category_id = None
    elif category_id in (None, ""):
        code = DEFAULT_CATEGORY_CODES[kind]
        category = db.scalars(select(Category).where(Category.code == code)).first()
        if category is None:
            raise TimetableError(f"Standardkategorie „{code}“ fehlt – bitte eine Kategorie wählen.")
        category_id = category.id
    else:
        try:
            category = db.get(Category, int(category_id))
        except (TypeError, ValueError):
            category = None
        if category is None:
            raise TimetableError("Unbekannte Kategorie.")
        if category.is_pseudo:
            raise TimetableError(f"Die Kategorie „{category.name}“ ist für den Stundenplan nicht geeignet.")
        category_id = category.id

    return {
        "weekday": weekday,
        "slot_no": slot_no,
        "start_time": start,
        "end_time": end,
        "kind": kind,
        "klasse": klasse,
        "fach": fach,
        "category_id": category_id,
    }


def _check_slots(slots: Iterable[Any]) -> None:
    """Unique (weekday, slot_no) and no time overlap of non-free slots per weekday."""
    by_day: dict[int, list[Any]] = defaultdict(list)
    for slot in slots:
        by_day[slot.weekday].append(slot)
    for weekday, items in by_day.items():
        seen: set[int] = set()
        for slot in items:
            if slot.slot_no in seen:
                raise TimetableConflict(f"{WEEKDAY_NAMES[weekday]}: Stunde {slot.slot_no} ist doppelt belegt.")
            seen.add(slot.slot_no)
        busy = sorted((s for s in items if s.kind != SlotKind.FREI.value), key=lambda s: (s.start_time, s.end_time))
        for a, b in zip(busy, busy[1:], strict=False):
            if b.start_time < a.end_time:
                raise TimetableConflict(
                    f"{WEEKDAY_NAMES[weekday]}: Stunde {a.slot_no} ({_hm(a.start_time)}–{_hm(a.end_time)}) und "
                    f"Stunde {b.slot_no} ({_hm(b.start_time)}–{_hm(b.end_time)}) überschneiden sich."
                )


@dataclass
class _SlotProbe:
    """Stand-in used to validate the would-be slot set before writing."""

    id: str | None
    weekday: int
    slot_no: int
    start_time: time
    end_time: time
    kind: str


def _probe(values: Mapping[str, Any], slot_id: str | None = None) -> _SlotProbe:
    return _SlotProbe(slot_id, values["weekday"], values["slot_no"], values["start_time"], values["end_time"],
                      values["kind"])


def get_slot(db: Session, slot_id: str) -> TimetableSlot:
    slot = db.get(TimetableSlot, slot_id)
    if slot is None:
        raise TimetableNotFound("Stunde nicht gefunden.")
    return slot


def add_slot(db: Session, timetable_id: str, data: Mapping[str, Any], *, commit: bool = True) -> TimetableSlot:
    tt = get_timetable(db, timetable_id)
    values = _slot_values(db, _settings(db), data, None)
    _check_slots([*(_probe(slot_input(s), s.id) for s in tt.slots), _probe(values)])
    slot = TimetableSlot(**values)
    tt.slots.append(slot)
    db.flush()
    if commit:
        db.commit()
    return slot


def update_slot(db: Session, slot_id: str, changes: Mapping[str, Any], *, now: datetime | None = None,
                commit: bool = True) -> TimetableSlot:
    """Change a slot. Unconfirmed suggestions of the slot are retired when its content changes, so the next
    generation creates them anew; confirmed entries keep their values."""
    slot = get_slot(db, slot_id)
    values = _slot_values(db, _settings(db), changes, slot)
    tt = slot.timetable
    _check_slots([*(_probe(slot_input(s), s.id) for s in tt.slots if s.id != slot.id), _probe(values, slot.id)])
    changed = any(getattr(slot, k) != v for k, v in values.items())
    if changed:
        _retire_suggestions_of_slots(db, [slot.id], now or now_utc())
        for key, value in values.items():
            setattr(slot, key, value)
    db.flush()
    if commit:
        db.commit()
    return slot


def delete_slot(db: Session, slot_id: str, *, now: datetime | None = None, commit: bool = True) -> None:
    slot = get_slot(db, slot_id)
    _retire_suggestions_of_slots(db, [slot.id], now or now_utc())
    _unlink_entries_of_slots(db, [slot.id], now or now_utc())
    slot.timetable.slots.remove(slot)
    db.flush()
    if commit:
        db.commit()
    db.expire_all()


def set_slots(db: Session, timetable_id: str, slots: Iterable[Mapping[str, Any]], *, now: datetime | None = None,
              commit: bool = True) -> list[TimetableSlot]:
    """Replace the slots of a version (weekly grid editor). Slots are matched by (weekday, slot_no), so
    unchanged slots keep their id (and the link of confirmed entries); missing ones are deleted."""
    tt = get_timetable(db, timetable_id)
    settings = _settings(db)
    now = now or now_utc()
    existing = {(s.weekday, s.slot_no): s for s in tt.slots}
    planned: list[tuple[TimetableSlot | None, dict[str, Any]]] = []
    for data in slots:
        values = _slot_values(db, settings, data, None)
        planned.append((existing.get((values["weekday"], values["slot_no"])), values))
    _check_slots([_probe(v, s.id if s else None) for s, v in planned])

    keep = {id(s) for s, _ in planned if s is not None}
    for slot in list(tt.slots):
        if id(slot) not in keep:
            _retire_suggestions_of_slots(db, [slot.id], now)
            _unlink_entries_of_slots(db, [slot.id], now)
            tt.slots.remove(slot)
    result: list[TimetableSlot] = []
    for slot, values in planned:
        if slot is None:
            slot = TimetableSlot(**values)
            tt.slots.append(slot)
        elif any(getattr(slot, k) != v for k, v in values.items()):
            _retire_suggestions_of_slots(db, [slot.id], now)
            for key, value in values.items():
                setattr(slot, key, value)
        result.append(slot)
    db.flush()
    if commit:
        db.commit()
        db.expire_all()
    return sorted(result, key=lambda s: (s.weekday, s.slot_no))


def slot_label(slot: TimetableSlot | Any, category: Category | None = None) -> str:
    """e.g. ``'3. Stunde · 3b Deutsch'`` or ``'Aufsicht (Stunde 0)'``."""
    head = f"{slot.slot_no}. Stunde" if slot.kind == SlotKind.UNTERRICHT.value else (
        f"{KIND_LABELS.get(slot.kind, slot.kind)} (Stunde {slot.slot_no})")
    detail = " ".join(x for x in (slot.klasse, slot.fach) if x)
    return f"{head} · {detail}" if detail else head


def slot_to_dict(db: Session, slot: TimetableSlot) -> dict[str, Any]:
    category = db.get(Category, slot.category_id) if slot.category_id else None
    seconds = int((datetime.combine(date.min, slot.end_time) - datetime.combine(date.min, slot.start_time))
                  .total_seconds())
    return {
        "id": slot.id,
        "timetable_id": slot.timetable_id,
        "weekday": slot.weekday,
        "weekday_name": WEEKDAY_NAMES.get(slot.weekday, str(slot.weekday)),
        "slot_no": slot.slot_no,
        "start_time": _hm(slot.start_time),
        "end_time": _hm(slot.end_time),
        "duration_seconds": seconds,
        "duration_hm": fmt_hm(seconds),
        "kind": slot.kind,
        "kind_label": KIND_LABELS.get(slot.kind, slot.kind),
        "klasse": slot.klasse,
        "fach": slot.fach,
        "category_id": slot.category_id,
        "category_code": category.code if category else None,
        "category_name": category.name if category else None,
        "color": category.color if category else None,
        "label": slot_label(slot),
    }


def timetable_to_dict(db: Session, tt: Timetable, *, include_slots: bool = True, today: date | None = None
                      ) -> dict[str, Any]:
    today = today or today_local()
    slots = list(tt.slots)
    unterricht = [s for s in slots if s.kind == SlotKind.UNTERRICHT.value]
    result: dict[str, Any] = {
        "id": tt.id,
        "label": tt.label,
        "valid_from": tt.valid_from,
        "valid_to": tt.valid_to,
        "aktuell": tt.valid_from <= today and (tt.valid_to is None or tt.valid_to >= today),
        "slot_count": len(slots),
        "unterricht_slots": len(unterricht),
    }
    if include_slots:
        result["slots"] = [slot_to_dict(db, s) for s in slots]
    return result


# =========================================================================== suggestions


@dataclass(frozen=True)
class _PlannedItem:
    key: str
    slot: TimetableSlot | None
    kind: str
    start: time
    end: time
    category_id: int | None
    klasse: str | None
    fach: str | None
    note: str | None
    label: str


@dataclass
class SuggestionResult:
    date: date
    timetable_id: str | None = None
    created: list[TimeEntry] = field(default_factory=list)
    existing: list[TimeEntry] = field(default_factory=list)
    retired: list[TimeEntry] = field(default_factory=list)
    skipped_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "timetable_id": self.timetable_id,
            "created": [entries_svc.entry_to_dict(e) for e in self.created],
            "existing": [entries_svc.entry_to_dict(e) for e in self.existing],
            "retired": [e.id for e in self.retired],
            "skipped_reason": self.skipped_reason,
            "warnings": list(self.warnings),
            "conflicts": list(self.conflicts),
            "confirmed": self.confirmed,
        }


def day_state(db: Session, d: date) -> tuple[CalendarDay | None, str | None]:
    """(calendar_day row, German reason why the day gets no suggestions / cannot be confirmed)."""
    row = db.get(CalendarDay, d)
    if row is None:
        return None, "Für diesen Tag ist kein Schuljahr angelegt."
    if row.day_type != DayType.SCHULTAG.value:
        label = DAY_TYPE_LABELS.get(row.day_type, row.day_type)
        if row.holiday_name and row.day_type == DayType.FEIERTAG.value:
            label = f"Feiertag: {row.holiday_name}"
        return row, f"Kein Schultag ({label})."
    if row.absence_type:
        return row, f"Ganztägige Abwesenheit ({ABSENCE_LABELS.get(row.absence_type, row.absence_type)})."
    return row, None


def planned_items(db: Session, d: date, timetable: Timetable | None = None, *, settings: Settings | None = None
                  ) -> list[_PlannedItem]:
    """The entries the timetable plans for ``d`` (non-free slots, plus Große Pausen if they count)."""
    timetable = timetable if timetable is not None else timetable_for_date(db, d)
    if timetable is None:
        return []
    settings = settings or _settings(db)
    cats = _categories_by_code(db)
    items: list[_PlannedItem] = []
    for slot in timetable.slots:
        if slot.weekday != d.isoweekday() or slot.kind == SlotKind.FREI.value:
            continue
        category_id = slot.category_id
        if category_id is None:
            default = cats.get(DEFAULT_CATEGORY_CODES.get(slot.kind, ""))
            category_id = default.id if default else None
        items.append(_PlannedItem(slot.id, slot, slot.kind, slot.start_time, slot.end_time, category_id,
                                  slot.klasse, slot.fach, None, slot_label(slot)))
    items.sort(key=lambda i: (i.start, i.end))
    if settings.grosse_pause_zaehlt and items:
        pause_cat = cats.get(PAUSE_CATEGORY_CODE)
        pauses = []
        for a, b in zip(items, items[1:], strict=False):
            gap = (datetime.combine(d, b.start) - datetime.combine(d, a.end)).total_seconds()
            if 0 < gap <= PAUSE_MAX_MINUTES * 60:
                pauses.append(_PlannedItem(
                    f"pause@{_hm(a.end)}", None, "pause", a.end, b.start, pause_cat.id if pause_cat else None,
                    None, None, PAUSE_NOTE, f"Große Pause {_hm(a.end)}–{_hm(b.start)}",
                ))
        items = sorted([*items, *pauses], key=lambda i: (i.start, i.end))
    return items


def _stundenplan_entries(db: Session, d: date) -> list[TimeEntry]:
    """Non-deleted entries of ``d`` created from the timetable (every status)."""
    return list(db.scalars(
        select(TimeEntry)
        .where(TimeEntry.day == d, TimeEntry.source == EntrySource.STUNDENPLAN.value, TimeEntry.deleted_at.is_(None))
        .order_by(TimeEntry.start_at, TimeEntry.id)
    ))


def _local_time(dt: datetime) -> time:
    return to_local(dt).time().replace(tzinfo=None)


def _match_existing(items: list[_PlannedItem], entries: list[TimeEntry]) -> dict[str, TimeEntry]:
    """Planned item key -> existing entry (slot link; pauses by start time)."""
    by_slot: dict[str, TimeEntry] = {}
    by_start: dict[time, TimeEntry] = {}
    for e in entries:
        if e.timetable_slot_id:
            by_slot.setdefault(e.timetable_slot_id, e)
        else:
            by_start.setdefault(_local_time(e.start_at), e)
    matched: dict[str, TimeEntry] = {}
    for item in items:
        entry = by_slot.get(item.slot.id) if item.slot is not None else by_start.get(item.start)
        if entry is not None:
            matched[item.key] = entry
    return matched


def _sync_suggestions(db: Session, d: date, *, settings: Settings, now: datetime, reason: str | None,
                      force: bool) -> SuggestionResult:
    """Create missing / retire stale suggestions of ``d`` without committing. ``force`` also works on
    confirmed days (used by :func:`confirm_day`)."""
    result = SuggestionResult(date=d)
    confirmation = db.get(DayConfirmation, d)
    result.confirmed = confirmation is not None
    _row, why = day_state(db, d)
    entries = _stundenplan_entries(db, d)
    closed = _month_closed(db, d)
    if closed and not reason:
        result.skipped_reason = "Der Monat ist abgeschlossen."
        result.existing = entries
        return result
    if why is not None:
        result.skipped_reason = why
        if confirmation is None:
            for entry in entries:
                if entry.status == EntryStatus.VORSCHLAG.value:
                    entries_svc.delete_entry(db, entry.id, reason=RETIRE_REASON, now=now, commit=False)
                    result.retired.append(entry)
        return result
    if confirmation is not None and not force:
        result.skipped_reason = "Der Tag ist bereits bestätigt."
        result.existing = entries
        return result

    tt = timetable_for_date(db, d)
    result.timetable_id = tt.id if tt else None
    items = planned_items(db, d, tt, settings=settings) if tt else []
    if tt is None:
        result.skipped_reason = "Für diesen Tag gilt kein Stundenplan."
    matched = _match_existing(items, entries)
    for item in items:
        entry = matched.get(item.key)
        if entry is not None:
            result.existing.append(entry)
            continue
        if item.category_id is None:
            result.warnings.append(f"{item.label}: keine Kategorie zugeordnet.")
            continue
        data = {
            "start_at": local_to_utc(d, item.start),
            "end_at": local_to_utc(d, item.end),
            "category_id": item.category_id,
            "klasse": item.klasse,
            "fach": item.fach,
            "location": Location.SCHULE.value,
            "note": item.note,
            "status": EntryStatus.VORSCHLAG.value,
            "timetable_slot_id": item.slot.id if item.slot is not None else None,
        }
        try:
            created = entries_svc.create_entry(db, data, source=EntrySource.STUNDENPLAN.value,
                                               reason=reason if closed else None, now=now, commit=False)
        except entries_svc.EntryValidationError as exc:
            result.warnings.append(f"{item.label}: {exc.message}")
            continue
        result.created.extend(created)

    keep = {e.id for e in matched.values()}
    for entry in entries:
        if entry.status == EntryStatus.VORSCHLAG.value and entry.id not in keep:
            entries_svc.delete_entry(db, entry.id, reason=RETIRE_REASON, now=now, commit=False)
            result.retired.append(entry)
    db.flush()
    result.conflicts = suggestion_conflicts(db, d)
    return result


def generate_suggestions(db: Session, d: date, *, now: datetime | None = None, commit: bool = True
                         ) -> SuggestionResult:
    """Create the timetable suggestions of day ``d`` (idempotent, see module docstring)."""
    now = now or now_utc().replace(microsecond=0)
    with _LOCK:
        try:
            result = _sync_suggestions(db, d, settings=_settings(db), now=now, reason=None, force=False)
            if commit:
                db.commit()
        except Exception:
            db.rollback()
            raise
    return result


def generate_today_suggestions(*, today: date | None = None, now: datetime | None = None) -> SuggestionResult | None:
    """Scheduler hook (00:05) and "first open of the day": generate today's suggestions in an own session.

    Safe to call any number of times; never raises (errors are logged and ``None`` is returned).
    """
    from app.db.session import SessionLocal

    try:
        with SessionLocal() as db:
            return generate_suggestions(db, today or today_local(), now=now)
    except Exception:
        log.exception("Stundenplan-Vorschläge konnten nicht erzeugt werden")
        return None


def register_scheduler_jobs(scheduler: Any) -> None:
    """Add the 00:05 (Europe/Berlin) job to an APScheduler 3.x scheduler."""
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        generate_today_suggestions,
        CronTrigger(hour=0, minute=5, timezone=LOCAL_TZ),
        id=SCHEDULER_JOB_ID,
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=6 * 3600,
    )


def suggestion_conflicts(db: Session, d: date) -> list[dict[str, Any]]:
    """Suggestions of ``d`` that overlap an active bounded manual/timer entry (to be resolved before confirm)."""
    suggestions = db.scalars(
        select(TimeEntry).where(
            TimeEntry.day == d, TimeEntry.status == EntryStatus.VORSCHLAG.value, TimeEntry.deleted_at.is_(None),
            TimeEntry.has_bounds.is_(True),
        ).order_by(TimeEntry.start_at)
    ).all()
    if not suggestions:
        return []
    active = db.scalars(
        select(TimeEntry).where(
            TimeEntry.status == EntryStatus.ACTIVE.value,
            TimeEntry.deleted_at.is_(None),
            TimeEntry.has_bounds.is_(True),
            TimeEntry.source != EntrySource.IMPORT.value,
            (TimeEntry.day == d) | (TimeEntry.end_at.is_(None)),
        ).order_by(TimeEntry.start_at)
    ).all()
    result = []
    for s in suggestions:
        hits = [
            a for a in active
            if a.start_at < s.end_at and (a.end_at is None or s.start_at < a.end_at)
        ]
        if not hits:
            continue
        slot = db.get(TimetableSlot, s.timetable_slot_id) if s.timetable_slot_id else None
        result.append({
            "entry_id": s.id,
            "slot_id": s.timetable_slot_id,
            "label": slot_label(slot) if slot else (s.note or "Stundenplan"),
            "start": _hm(_local_time(s.start_at)),
            "end": _hm(_local_time(s.end_at)),
            "overlaps": [
                {
                    "entry_id": a.id,
                    "source": a.source,
                    "category_code": a.category.code if a.category else None,
                    "start": _hm(_local_time(a.start_at)),
                    "end": _hm(_local_time(a.end_at)) if a.end_at else None,
                    "running": a.end_at is None,
                }
                for a in hits
            ],
            "hinweis": "Überschneidung mit einem erfassten Eintrag – bitte „entfallen“ wählen oder die Zeit anpassen.",
        })
    return result


# =========================================================================== confirmation


@dataclass(frozen=True)
class Deviation:
    type: str
    klasse: str | None = None
    fach: str | None = None
    art: str | None = None
    start: time | None = None
    end: time | None = None
    note: str | None = None
    category_id: int | None = None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["start"] = _hm(self.start) if self.start else None
        data["end"] = _hm(self.end) if self.end else None
        return {k: v for k, v in data.items() if v is not None}


def parse_deviation(raw: Mapping[str, Any]) -> Deviation:
    kind = str(raw.get("type") or "").strip().lower()
    if kind not in DEVIATION_TYPES:
        raise TimetableError(
            f"Unbekannte Abweichung „{raw.get('type')}“ (wie_geplant, entfallen, vertretung, exkursion, zeit_angepasst)."
        )
    start = _parse_time(raw.get("start"), "Beginn")
    end = _parse_time(raw.get("end"), "Ende")
    if start and end and end <= start:
        raise TimetableError("Das Ende muss nach dem Beginn liegen.")
    if kind == DEVIATION_ZEIT and (start is None or end is None):
        raise TimetableError("Für „Zeit angepasst“ bitte Beginn und Ende angeben.")
    art = None
    if kind == DEVIATION_VERTRETUNG:
        art = str(raw.get("art") or "angeordnet").strip().lower()
        if art not in VERTRETUNG_FLAGS:
            raise TimetableError("Art der Vertretung muss „angeordnet“ oder „genehmigt“ sein.")
    category_id = raw.get("category_id")
    if category_id not in (None, ""):
        try:
            category_id = int(category_id)
        except (TypeError, ValueError):
            raise TimetableError("Unbekannte Kategorie.") from None
    else:
        category_id = None
    if kind in (DEVIATION_WIE_GEPLANT, DEVIATION_ENTFALLEN):
        start = end = None
    return Deviation(
        type=kind,
        klasse=_clean(raw.get("klasse"), max_len=16, field_label="Klasse"),
        fach=_clean(raw.get("fach"), max_len=64, field_label="Fach"),
        art=art,
        start=start,
        end=end,
        note=_clean(raw.get("note")),
        category_id=category_id,
    )


@dataclass
class _Item:
    key: str
    entry: TimeEntry | None
    slot: TimetableSlot | None
    label: str

    @property
    def is_frei(self) -> bool:
        return self.slot is not None and self.slot.kind == SlotKind.FREI.value


@dataclass
class _Target:
    status: str
    start: time | None = None
    end: time | None = None
    category_id: int | None = None
    subcategory_id: int | None = None
    klasse: str | None = None
    fach: str | None = None
    vertretung_flag: str | None = None
    note: str | None = None  # only set when the deviation gives one


@dataclass
class ConfirmResult:
    date: date
    confirmation: DayConfirmation
    active: list[TimeEntry]
    verworfen: list[TimeEntry]
    extra: list[TimeEntry]
    changed: list[TimeEntry]
    first: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "confirmed_at": _iso(self.confirmation.confirmed_at),
            "first_confirmation": self.first,
            "deviations": self.confirmation.deviations_json,
            "active": [entries_svc.entry_to_dict(e) for e in self.active],
            "verworfen": [entries_svc.entry_to_dict(e) for e in self.verworfen],
            "extra": [entries_svc.entry_to_dict(e) for e in self.extra],
            "changed": [e.id for e in self.changed],
        }


def _planned_from_entry(entry: TimeEntry) -> dict[str, Any]:
    return {
        "start": _local_time(entry.start_at).isoformat(),
        "end": _local_time(entry.end_at).isoformat() if entry.end_at else None,
        "category_id": entry.category_id,
        "subcategory_id": entry.subcategory_id,
        "klasse": entry.klasse,
        "fach": entry.fach,
    }


def _planned_from_slot(db: Session, slot: TimetableSlot) -> dict[str, Any]:
    category_id, sub_id = _main_category_id(db, slot.category_id)
    if category_id is None:
        default = db.scalars(select(Category).where(Category.code == DEFAULT_CATEGORY_CODES.get(slot.kind, ""))).first()
        category_id = default.id if default else None
    return {
        "start": slot.start_time.isoformat(),
        "end": slot.end_time.isoformat(),
        "category_id": category_id,
        "subcategory_id": sub_id,
        "klasse": slot.klasse,
        "fach": slot.fach,
    }


def _target(db: Session, item: _Item, planned: dict[str, Any] | None, dev: Deviation | None,
            cats: dict[str, Category]) -> _Target | None:
    kind = dev.type if dev is not None else DEVIATION_WIE_GEPLANT
    if planned is None:  # a free slot: only a Vertretung creates an entry
        if kind == DEVIATION_VERTRETUNG:
            if not dev.klasse:
                raise TimetableError(f"{item.label}: Bitte die Klasse der Vertretung angeben.")
            slot = item.slot
            u = cats[VERTRETUNG_CATEGORY_CODE]
            sub = _vertretung_subcategory(db, u)
            return _Target(
                status=EntryStatus.ACTIVE.value,
                start=dev.start or slot.start_time,
                end=dev.end or slot.end_time,
                category_id=u.id,
                subcategory_id=sub.id if sub else None,
                klasse=dev.klasse,
                fach=dev.fach,
                vertretung_flag=VERTRETUNG_FLAGS[dev.art or "angeordnet"],
                note=dev.note,
            )
        if kind in (DEVIATION_WIE_GEPLANT, DEVIATION_ENTFALLEN):
            return _Target(status=EntryStatus.VERWORFEN.value) if item.entry is not None else None
        raise TimetableError(f"{item.label}: In einer freien Stunde ist nur „Vertretung“ möglich.")

    target = _Target(
        status=EntryStatus.ACTIVE.value,
        start=time.fromisoformat(planned["start"]),
        end=time.fromisoformat(planned["end"]),
        category_id=planned.get("category_id"),
        subcategory_id=planned.get("subcategory_id"),
        klasse=planned.get("klasse"),
        fach=planned.get("fach"),
        note=dev.note if dev is not None else None,
    )
    if kind == DEVIATION_ENTFALLEN:
        target.status = EntryStatus.VERWORFEN.value
    elif kind == DEVIATION_ZEIT:
        target.start, target.end = dev.start, dev.end
    elif kind == DEVIATION_VERTRETUNG:
        u = cats[VERTRETUNG_CATEGORY_CODE]
        planned_cat = db.get(Category, target.category_id) if target.category_id else None
        main = planned_cat if planned_cat is not None and planned_cat.is_unterricht else u
        sub = _vertretung_subcategory(db, main)
        target.category_id = main.id
        target.subcategory_id = sub.id if sub else None
        if dev.klasse:
            target.klasse = dev.klasse
            target.fach = dev.fach
        elif dev.fach:
            target.fach = dev.fach
        target.vertretung_flag = VERTRETUNG_FLAGS[dev.art or "angeordnet"]
        target.start, target.end = dev.start or target.start, dev.end or target.end
    elif kind == DEVIATION_EXKURSION:
        if dev.category_id is not None:
            main_id, sub_id = _main_category_id(db, dev.category_id)
            if main_id is None or db.get(Category, main_id) is None:
                raise TimetableError(f"{item.label}: Unbekannte Kategorie.")
            target.category_id, target.subcategory_id = main_id, sub_id
        else:
            sv = cats.get(EXKURSION_CATEGORY_CODE)
            if sv is None:
                raise TimetableError(f"{item.label}: Kategorie „{EXKURSION_CATEGORY_CODE}“ fehlt.")
            target.category_id, target.subcategory_id = sv.id, None
        target.start, target.end = dev.start or target.start, dev.end or target.end
    if target.end <= target.start:
        raise TimetableError(f"{item.label}: Das Ende muss nach dem Beginn liegen.")
    return target


def _changes(d: date, entry: TimeEntry, target: _Target) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if target.status != entry.status:
        changes["status"] = target.status
    if target.start is not None and target.end is not None:
        start, end = local_to_utc(d, target.start), local_to_utc(d, target.end)
        if (start, end) != (entry.start_at, entry.end_at):
            changes["start_at"], changes["end_at"] = start, end
    if target.category_id is not None and (target.category_id, target.subcategory_id) != (
            entry.category_id, entry.subcategory_id):
        changes["category_id"], changes["subcategory_id"] = target.category_id, target.subcategory_id
    if target.status == EntryStatus.ACTIVE.value or target.category_id is not None:
        if (target.klasse or None) != (entry.klasse or None):
            changes["klasse"] = target.klasse
        if (target.fach or None) != (entry.fach or None):
            changes["fach"] = target.fach
    current_user = set(entry.flags or []) & entries_svc.USER_FLAGS
    wanted = (current_user - ALL_VERTRETUNG_FLAGS) | ({target.vertretung_flag} if target.vertretung_flag else set())
    if wanted != current_user:
        changes["flags"] = sorted(wanted)
    if target.note is not None and target.note != entry.note:
        changes["note"] = target.note
    return changes


def _resolve_deviations(raw: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | None,
                        items: dict[str, _Item]) -> dict[str, Deviation]:
    if raw is None:
        return {}
    pairs: list[tuple[str | None, str | None, Mapping[str, Any]]] = []
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            pairs.append((str(key), str(key), value))
    else:
        for value in raw:
            if not isinstance(value, Mapping):
                raise TimetableError("Abweichungen: Liste von Objekten erwartet.")
            pairs.append((value.get("slot_id"), value.get("entry_id"), value))
    by_entry = {i.entry.id: k for k, i in items.items() if i.entry is not None}
    result: dict[str, Deviation] = {}
    for slot_id, entry_id, value in pairs:
        key = None
        if slot_id and slot_id in items:
            key = slot_id
        elif entry_id and entry_id in by_entry:
            key = by_entry[entry_id]
        elif entry_id and f"entry:{entry_id}" in items:
            key = f"entry:{entry_id}"
        if key is None:
            raise TimetableError("Abweichung für eine unbekannte Stunde – bitte die Ansicht neu laden.")
        if key in result:
            raise TimetableError(f"{items[key].label}: mehr als eine Abweichung angegeben.")
        result[key] = parse_deviation(value)
    return result


def _confirm_items(db: Session, d: date) -> dict[str, _Item]:
    tt = timetable_for_date(db, d)
    day_slots = {s.id: s for s in tt.slots if s.weekday == d.isoweekday()} if tt else {}
    items: dict[str, _Item] = {}
    for entry in _stundenplan_entries(db, d):
        slot = None
        if entry.timetable_slot_id:
            slot = day_slots.get(entry.timetable_slot_id) or db.get(TimetableSlot, entry.timetable_slot_id)
        key = entry.timetable_slot_id if entry.timetable_slot_id and entry.timetable_slot_id not in items \
            else f"entry:{entry.id}"
        if slot is not None:
            label = slot_label(slot)
        else:
            label = f"{entry.note or 'Stundenplan'} {_hm(_local_time(entry.start_at))}"
        items[key] = _Item(key, entry, slot, label)
    for slot in day_slots.values():
        if slot.kind == SlotKind.FREI.value and slot.id not in items:
            items[slot.id] = _Item(slot.id, None, slot, slot_label(slot))
    return items


def confirm_form_items(db: Session, d: date) -> list[dict[str, Any]]:
    """The items of the day-confirmation screen (``/tag/{date}``) with their current/last deviation.

    ``key`` is what :func:`confirm_day` accepts as ``slot_id`` of a deviation. Call :func:`generate_suggestions`
    first so that planned lessons of an unconfirmed day exist as suggestions.
    """
    items = _confirm_items(db, d)
    confirmation = db.get(DayConfirmation, d)
    previous = dict((confirmation.deviations_json or {}).get("items") or {}) if confirmation is not None else {}
    cats = {c.id: c for c in db.scalars(select(Category))}
    rows: list[dict[str, Any]] = []
    for key, item in items.items():
        entry, slot = item.entry, item.slot
        if entry is not None:
            start = _local_time(entry.start_at)
            end = _local_time(entry.end_at) if entry.end_at else None
            cat = cats.get(entry.category_id)
            klasse, fach, status = entry.klasse, entry.fach, entry.status
        else:
            start, end = (slot.start_time, slot.end_time) if slot is not None else (None, None)
            cat, klasse, fach, status = None, None, None, None
        dev = dict((previous.get(key) or {}).get("deviation") or {})
        rows.append({
            "key": key,
            "label": item.label,
            "start": _hm(start) if start else "",
            "end": _hm(end) if end else "",
            "sort": start or time.min,
            "code": cat.code if cat else "",
            "name": cat.name if cat else ("frei" if item.is_frei else ""),
            "color": cat.color if cat else "#94A3B8",
            "klasse": klasse,
            "fach": fach,
            "status": status,
            "frei": item.is_frei,
            "deviation": str(dev.get("type") or DEVIATION_WIE_GEPLANT),
            "dev": dev,
        })
    rows.sort(key=lambda r: (r["sort"], r["key"]))
    return rows


def _parse_extra(raw: Mapping[str, Any]) -> dict[str, Any]:
    start = _parse_time(raw.get("start"), "Beginn")
    end = _parse_time(raw.get("end"), "Ende")
    if start is None or end is None:
        raise TimetableError("Zusätzliche Vertretung: bitte Beginn und Ende angeben.")
    if end <= start:
        raise TimetableError("Zusätzliche Vertretung: Das Ende muss nach dem Beginn liegen.")
    klasse = _clean(raw.get("klasse"), max_len=16, field_label="Klasse")
    if not klasse:
        raise TimetableError("Zusätzliche Vertretung: bitte die Klasse angeben.")
    art = str(raw.get("art") or "angeordnet").strip().lower()
    if art not in VERTRETUNG_FLAGS:
        raise TimetableError("Art der Vertretung muss „angeordnet“ oder „genehmigt“ sein.")
    return {"start": start, "end": end, "klasse": klasse, "fach": _clean(raw.get("fach"), max_len=64,
            field_label="Fach"), "art": art, "note": _clean(raw.get("note"))}


def confirm_day(
    db: Session,
    d: date,
    deviations: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | None = None,
    *,
    extra: Iterable[Mapping[str, Any]] | None = None,
    reason: str | None = None,
    now: datetime | None = None,
    today: date | None = None,
) -> ConfirmResult:
    """Confirm the school day ``d`` ("Tag wie geplant" without deviations). Atomic: on any error nothing
    is written. Raises :class:`TimetableError` (422), :class:`TimetableConflict` (409, a slot collides with
    an existing entry) or the entries service errors (e.g. ``EntryLocked`` 423 in a closed month without
    ``reason``)."""
    now = now or now_utc().replace(microsecond=0)
    today = today or today_local()
    reason = _clean(reason)
    if d > today:
        raise TimetableError("Zukünftige Tage können noch nicht bestätigt werden.")
    with _LOCK:
        try:
            result = _confirm(db, d, deviations, extra, reason, now)
            db.commit()
            db.expire_all()  # update_entry leaves loaded relationships (e.g. subcategory) stale
        except Exception:
            db.rollback()
            raise
    return result


def _apply(db: Session, d: date, item: _Item, target: _Target, reason: str | None, now: datetime
           ) -> TimeEntry | None:
    """Bring the item's entry to ``target``; returns the entry if it was created or changed."""
    try:
        if item.entry is None:
            if target.status != EntryStatus.ACTIVE.value:
                return None
            data = {
                "start_at": local_to_utc(d, target.start),
                "end_at": local_to_utc(d, target.end),
                "category_id": target.category_id,
                "subcategory_id": target.subcategory_id,
                "klasse": target.klasse,
                "fach": target.fach,
                "location": Location.SCHULE.value,
                "note": target.note,
                "flags": [target.vertretung_flag] if target.vertretung_flag else [],
                "status": EntryStatus.ACTIVE.value,
                "timetable_slot_id": item.slot.id if item.slot is not None else None,
            }
            (entry, *_rest) = entries_svc.create_entry(db, data, source=EntrySource.STUNDENPLAN.value,
                                                       reason=reason, now=now, commit=False)
            item.entry = entry
            return entry
        changes = _changes(d, item.entry, target)
        if not changes:
            return None
        entries_svc.update_entry(db, item.entry.id, changes, reason=reason, now=now, commit=False)
        return item.entry
    except entries_svc.EntryConflict as exc:
        raise TimetableConflict(
            f"{item.label}: {exc.message} Bitte „entfallen“ wählen oder die Zeit anpassen."
        ) from exc
    except entries_svc.EntryLocked:
        raise
    except entries_svc.DomainError as exc:
        raise type(exc)(f"{item.label}: {exc.message}") from exc


def _confirm(db: Session, d: date, raw_deviations: Any, raw_extra: Iterable[Mapping[str, Any]] | None,
             reason: str | None, now: datetime) -> ConfirmResult:
    settings = _settings(db)
    _row, why = day_state(db, d)
    if why is not None:
        raise TimetableError(f"Der {_fmt_date(d)} kann nicht bestätigt werden: {why}")
    if _month_closed(db, d) and not reason:
        raise entries_svc.EntryLocked(entries_svc.LOCKED_MESSAGE)
    extras_in = [_parse_extra(x) for x in (raw_extra or [])]

    _sync_suggestions(db, d, settings=settings, now=now, reason=reason, force=True)
    db.flush()
    prev = db.get(DayConfirmation, d)
    prev_data: dict[str, Any] = dict(prev.deviations_json or {}) if prev is not None else {}
    prev_items: dict[str, Any] = dict(prev_data.get("items") or {})

    items = _confirm_items(db, d)
    devs = _resolve_deviations(raw_deviations, items)
    cats = _categories_by_code(db)
    if VERTRETUNG_CATEGORY_CODE not in cats:  # pragma: no cover - seeded
        raise TimetableError("Kategorie „U“ fehlt.")

    plans: dict[str, tuple[dict[str, Any] | None, _Target | None]] = {}
    for key, item in items.items():
        stored = (prev_items.get(key) or {}).get("planned")
        if item.entry is not None and item.entry.status == EntryStatus.VORSCHLAG.value:
            planned = _planned_from_entry(item.entry)
        elif stored:
            planned = stored
        elif item.slot is not None and not item.is_frei:
            planned = _planned_from_slot(db, item.slot)
        elif item.entry is not None and item.slot is None:
            planned = _planned_from_entry(item.entry)
        else:
            planned = None
        plans[key] = (planned, _target(db, item, planned, devs.get(key), cats))

    def order(key: str) -> tuple[int, time]:
        target = plans[key][1]
        first = 0 if target is None or target.status != EntryStatus.ACTIVE.value else 1
        return first, (target.start if target and target.start else time.min)

    changed: list[TimeEntry] = []
    for key in sorted(items, key=order):
        target = plans[key][1]
        if target is None:
            continue
        entry = _apply(db, d, items[key], target, reason, now)
        if entry is not None:
            changed.append(entry)

    extra_entries = _apply_extras(db, d, extras_in, cats, reason, now, changed)
    db.flush()

    items_json: dict[str, Any] = {}
    for key, item in items.items():
        planned, target = plans[key]
        dev = devs.get(key)
        if item.entry is None and planned is None and dev is None:
            continue  # untouched free slot
        items_json[key] = {
            "entry_id": item.entry.id if item.entry is not None else None,
            "slot_id": item.slot.id if item.slot is not None else None,
            "kind": item.slot.kind if item.slot is not None else "pause",
            "label": item.label,
            "planned": planned,
            "deviation": dev.to_json() if dev is not None else None,
            "status": item.entry.status if item.entry is not None else None,
        }
    extra_json = [
        x for x in (prev_data.get("extra") or [])
        if (e := db.get(TimeEntry, x.get("entry_id"))) is not None and e.deleted_at is None
    ]
    known = {x.get("entry_id") for x in extra_json}
    for entry, spec in extra_entries:
        if entry.id not in known:
            extra_json.append({
                "entry_id": entry.id, "start": _hm(spec["start"]), "end": _hm(spec["end"]),
                "klasse": spec["klasse"], "fach": spec["fach"], "art": spec["art"],
            })
            known.add(entry.id)
    history = list(prev_data.get("history") or [])
    if prev is not None:
        history = [*history, _iso(prev.confirmed_at)][-CONFIRM_HISTORY_MAX:]
    payload = {
        "timetable_id": (tt.id if (tt := timetable_for_date(db, d)) else None),
        "items": items_json,
        "extra": extra_json,
        "history": history,
    }
    if prev is None:
        confirmation = DayConfirmation(date=d, confirmed_at=now, deviations_json=payload)
        db.add(confirmation)
    else:
        confirmation = prev
        confirmation.confirmed_at = now
        confirmation.deviations_json = payload
    db.flush()

    day_entries = _stundenplan_entries(db, d)
    return ConfirmResult(
        date=d,
        confirmation=confirmation,
        active=[e for e in day_entries if e.status == EntryStatus.ACTIVE.value],
        verworfen=[e for e in day_entries if e.status == EntryStatus.VERWORFEN.value],
        extra=[e for e, _ in extra_entries],
        changed=changed,
        first=prev is None,
    )


def _apply_extras(db: Session, d: date, extras: list[dict[str, Any]], cats: dict[str, Category],
                  reason: str | None, now: datetime, changed: list[TimeEntry]) -> list[tuple[TimeEntry, dict]]:
    """Create additional Vertretungen (idempotent: an identical active entry is reused)."""
    if not extras:
        return []
    u = cats[VERTRETUNG_CATEGORY_CODE]
    sub = _vertretung_subcategory(db, u)
    result: list[tuple[TimeEntry, dict]] = []
    for spec in extras:
        start, end = local_to_utc(d, spec["start"]), local_to_utc(d, spec["end"])
        flag = VERTRETUNG_FLAGS[spec["art"]]
        same = db.scalars(
            select(TimeEntry).where(
                TimeEntry.day == d, TimeEntry.start_at == start, TimeEntry.end_at == end,
                TimeEntry.category_id == u.id, TimeEntry.status == EntryStatus.ACTIVE.value,
                TimeEntry.deleted_at.is_(None),
            )
        ).all()
        match = next((e for e in same if flag in (e.flags or []) and (e.klasse or None) == spec["klasse"]), None)
        if match is not None:
            result.append((match, spec))
            continue
        data = {
            "start_at": start, "end_at": end, "category_id": u.id, "subcategory_id": sub.id if sub else None,
            "klasse": spec["klasse"], "fach": spec["fach"], "location": Location.SCHULE.value,
            "note": spec["note"], "flags": [flag], "status": EntryStatus.ACTIVE.value,
        }
        label = f"Vertretung {spec['klasse']} {_hm(spec['start'])}–{_hm(spec['end'])}"
        try:
            (entry, *_rest) = entries_svc.create_entry(db, data, source=EntrySource.MANUELL.value, reason=reason,
                                                       now=now, commit=False)
        except entries_svc.EntryConflict as exc:
            raise TimetableConflict(f"{label}: {exc.message}") from exc
        except entries_svc.EntryLocked:
            raise
        except entries_svc.DomainError as exc:
            raise type(exc)(f"{label}: {exc.message}") from exc
        changed.append(entry)
        result.append((entry, spec))
    return result


def get_confirmation(db: Session, d: date) -> DayConfirmation | None:
    return db.get(DayConfirmation, d)


def confirmation_to_dict(confirmation: DayConfirmation | None) -> dict[str, Any] | None:
    if confirmation is None:
        return None
    return {
        "date": confirmation.date,
        "confirmed_at": _iso(confirmation.confirmed_at),
        "deviations": confirmation.deviations_json or {},
    }


# =========================================================================== day view / open days


def day_view(db: Session, d: date, *, today: date | None = None) -> dict[str, Any]:
    """Everything the day screen needs (``GET /api/days/{date}``): calendar type, soll/ist, entries,
    suggestions, the planned slots, conflicts and the confirmation."""
    from app.services import ledger

    today = today or today_local()
    detail = ledger.day_detail(db, d)
    out = asdict(detail)
    out["delta"] = detail.delta
    out["soll_hm"] = fmt_hm(detail.soll)
    out["ist_hm"] = fmt_hm(detail.ist)
    out["delta_hm"] = fmt_hm(detail.delta, plus=True)
    out["weekday_name"] = WEEKDAY_NAMES.get(d.isoweekday())

    _row, why = day_state(db, d)
    tt = timetable_for_date(db, d)
    slots = [s for s in tt.slots if s.weekday == d.isoweekday()] if tt else []
    confirmation = db.get(DayConfirmation, d)
    sp_entries = _stundenplan_entries(db, d)
    out["timetable"] = {"id": tt.id, "label": tt.label} if tt else None
    out["slots"] = [slot_to_dict(db, s) for s in slots]
    out["verworfen"] = [entries_svc.entry_to_dict(e) for e in sp_entries
                        if e.status == EntryStatus.VERWORFEN.value]
    out["conflicts"] = suggestion_conflicts(db, d)
    out["confirmation"] = confirmation_to_dict(confirmation)
    out["confirmed"] = confirmation is not None
    out["schultag"] = why is None
    out["suggestions_hinweis"] = why or (None if tt else "Für diesen Tag gilt kein Stundenplan.")
    out["can_confirm"] = why is None and d <= today and not _month_closed(db, d)
    out["month_closed"] = _month_closed(db, d)
    return out


@dataclass(frozen=True)
class OpenDay:
    date: date
    unbestaetigt: bool
    ohne_eintraege: bool
    vorschlaege: int
    absence: str | None
    gruende: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "weekday": self.date.isoweekday(),
            "weekday_name": WEEKDAY_NAMES.get(self.date.isoweekday()),
            "unbestaetigt": self.unbestaetigt,
            "ohne_eintraege": self.ohne_eintraege,
            "vorschlaege": self.vorschlaege,
            "absence": self.absence,
            "gruende": list(self.gruende),
        }


@dataclass(frozen=True)
class OpenDays:
    period: Period
    until: date
    days: list[OpenDay]

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": {"id": self.period.id, "label": self.period.label, "start": self.period.start,
                       "end": self.period.end},
            "until": self.until,
            "count": len(self.days),
            "unbestaetigt": sum(1 for x in self.days if x.unbestaetigt),
            "ohne_eintraege": sum(1 for x in self.days if x.ohne_eintraege),
            "days": [x.to_dict() for x in self.days],
        }


def open_days(db: Session, until: date | None = None, *, today: date | None = None,
              period: Period | str | None = None) -> OpenDays:
    """To-do list: school days of the (current) period up to ``until`` (default and maximum: today) that
    are not confirmed although the timetable plans something for them, or that have no active entry.

    Days with a full-day absence are skipped, except Fortbildung (which needs an entry, Konzept §5).
    Raises :class:`app.services.calendar_service.NoPeriodError` if there is no period.
    """
    from app.services import calendar_service as cal

    today = today or today_local()
    if not isinstance(period, Period):
        period = cal.resolve_period(db, period, today)
    end = min(until or today, today, period.end)
    start = period.start
    recording_start = _settings(db).erfassung_ab
    if recording_start is not None and recording_start > start:
        start = recording_start  # days before the start of recording are neutral (no to-do)
    if end < start:
        return OpenDays(period, end, [])

    rows = db.scalars(
        select(CalendarDay).where(
            CalendarDay.date.between(start, end), CalendarDay.day_type == DayType.SCHULTAG.value
        ).order_by(CalendarDay.date)
    ).all()
    confirmed = set(db.scalars(select(DayConfirmation.date).where(DayConfirmation.date.between(start, end))))
    active_counts = dict(db.execute(
        select(TimeEntry.day, func.count()).where(
            TimeEntry.day.between(start, end), TimeEntry.status == EntryStatus.ACTIVE.value,
            TimeEntry.deleted_at.is_(None),
        ).group_by(TimeEntry.day)
    ).all())
    suggestion_counts = dict(db.execute(
        select(TimeEntry.day, func.count()).where(
            TimeEntry.day.between(start, end), TimeEntry.status == EntryStatus.VORSCHLAG.value,
            TimeEntry.deleted_at.is_(None),
        ).group_by(TimeEntry.day)
    ).all())
    plans = [
        (tt.valid_from, tt.valid_to, {s.weekday for s in tt.slots if s.kind != SlotKind.FREI.value})
        for tt in list_timetables(db)
    ]

    def has_plan(d: date) -> bool:
        for valid_from, valid_to, weekdays in plans:
            if valid_from <= d and (valid_to is None or valid_to >= d):
                return d.isoweekday() in weekdays
        return False

    result: list[OpenDay] = []
    for row in rows:
        absence = row.absence_type
        if absence and absence != AbsenceType.FORTBILDUNG.value:
            continue
        unbestaetigt = absence is None and row.date not in confirmed and has_plan(row.date)
        ohne = active_counts.get(row.date, 0) == 0
        if not (unbestaetigt or ohne):
            continue
        gruende = []
        if unbestaetigt:
            gruende.append("Stundenplan nicht bestätigt")
        if ohne:
            gruende.append("Keine Einträge" if absence is None else "Fortbildung ohne Eintrag")
        result.append(OpenDay(row.date, unbestaetigt, ohne, suggestion_counts.get(row.date, 0), absence,
                              tuple(gruende)))
    return OpenDays(period, end, result)
