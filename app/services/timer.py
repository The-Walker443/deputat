"""Timer: start / stop / switch / pause / resume / status (Konzept §6.3 Pausen, §8.1, §8.4).

All writes to ``time_entry`` go through :mod:`app.services.entries`; this module only orchestrates.

* **Switch semantics.** Starting while a timer runs stops the running one at the new start time
  (``switch`` is an alias of ``start``). A start whose device time lies *before* the running timer's
  start (offline queue conflict) is rejected with :class:`TimerConflict` (409).
* **Pause / Weiter.** ``pause`` stops the running entry and remembers category + payload in ``timer_state``;
  ``resume`` starts a new entry with the remembered values. Pause itself is never an entry.
* **Midnight.** Stopping a timer that crossed local midnight yields split entries (entries service).
* **Default location** (Konzept §8.1): ``settings.ui_default_location_rule`` (default Mo–Fr 07:00–14:00 →
  ``schule``, otherwise ``zuhause``) evaluated at the local start time, unless a location is given.
* **Kennzeichen** (Konzept §4): a start may carry the flags :data:`TIMER_FLAGS` – ``klassenfahrt`` and
  ``nachtbereitschaft`` for every category, ``vertretung_angeordnet`` / ``vertretung_genehmigt`` only for
  Unterricht (category or its parent ``is_unterricht``). Other flags are rejected (400); pause/resume keep them.
* **Offline idempotency.** Every action accepts ``client_action_id``; processed ids are remembered in
  ``timer_state.paused_payload["processed_actions"]`` (last :data:`MAX_REMEMBERED_ACTIONS`). A repeated id
  is ignored and the current status is returned with ``duplicate=True``.

``timer_state.paused_payload`` layout (JSON, no schema change)::

    {"pause": {"category_id", "subcategory_id", "klasse", "fach", "location", "location_explicit",
               "note", "flags", "paused_at"} | null,
     "running": {"entry_id", "location_explicit"} | null,   # was the running timer's location chosen?
     "processed_actions": ["<client_action_id>", ...]}

``timer_state.paused_category_id`` mirrors ``pause.category_id`` (NULL = not paused).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Category, EntrySource, Location, Settings, TimeEntry, TimerState
from app.db.seed import DEFAULT_LOCATION_RULE
from app.services import entries as entries_service
from app.core.timesplit import local_midnight_utc
from app.services.entries import DomainError, EntryValidationError
from app.util import local_date, now_utc, to_local, to_utc_naive

LONG_RUNNING_SECONDS = 6 * 3600
MAX_REMEMBERED_ACTIONS = 200
FUTURE_TOLERANCE = timedelta(minutes=5)

WARNING_LONG_RUNNING = "Der Timer läuft seit mehr als 6 Stunden – noch aktiv?"
WARNING_MIDNIGHT = "Der Timer läuft über Mitternacht – beim Beenden wird der Eintrag automatisch geteilt."
WARNING_AUTO_ENDED = (
    "Der Timer lief über Mitternacht hinaus und wurde automatisch um 24:00 des Starttags beendet "
    "(Konzept: vergessener Timer). Spätere Arbeitszeit bitte als Eintrag nachtragen."
)
# Konzept §8.1: a timer running over midnight ends at 23:59:59/24:00 of its start day. Ending shortly after
# midnight (e.g. working until 00:30) is still split at midnight; an end later than this after midnight –
# or on a later day – is treated as a forgotten timer and capped at the start day's midnight.
AUTO_END_AFTER_MIDNIGHT = timedelta(hours=4)

# Flags the user can set when starting a timer (screen "Heute"); the Vertretung flags only for Unterricht.
TIMER_FLAGS_ALL = ("klassenfahrt", "nachtbereitschaft")
TIMER_FLAGS_UNTERRICHT = ("vertretung_angeordnet", "vertretung_genehmigt")
TIMER_FLAGS = TIMER_FLAGS_ALL + TIMER_FLAGS_UNTERRICHT


class TimerConflict(DomainError):
    status_code = 409


class TimerNotRunning(DomainError):
    status_code = 409


@dataclass
class TimerStatus:
    running: TimeEntry | None
    elapsed_seconds: int
    paused: dict[str, Any] | None
    warnings: list[str]
    crosses_midnight: bool = False
    stopped: list[TimeEntry] = field(default_factory=list)  # entries finished by this action
    duplicate: bool = False
    server_time: datetime | None = None
    auto_ended: bool = False  # the finished entry was capped at the start day's midnight (forgotten timer)
    notices: list[str] = field(default_factory=list)  # one-off hints about this action (shown once)


# --------------------------------------------------------------------------- state helpers


def _now(now: datetime | None) -> datetime:
    return to_utc_naive(now) if now is not None else now_utc().replace(microsecond=0)


def _state(db: Session) -> TimerState:
    state = db.get(TimerState, 1)
    if state is None:
        state = TimerState(id=1)
        db.add(state)
        db.flush()
    return state


def _payload(state: TimerState) -> dict[str, Any]:
    raw = state.paused_payload if isinstance(state.paused_payload, dict) else {}
    return {
        "pause": raw.get("pause"),
        "running": raw.get("running"),
        "processed_actions": list(raw.get("processed_actions") or []),
    }


def _save_payload(state: TimerState, payload: dict[str, Any], now: datetime) -> None:
    pause = payload.get("pause")
    state.paused_category_id = pause.get("category_id") if pause else None
    payload["processed_actions"] = payload["processed_actions"][-MAX_REMEMBERED_ACTIONS:]
    state.paused_payload = dict(payload)  # new object so SQLAlchemy detects the JSON change
    state.updated_at = now


def _is_duplicate(db: Session, client_action_id: str | None) -> bool:
    if not client_action_id:
        return False
    return str(client_action_id) in _payload(_state(db))["processed_actions"]


def _remember(payload: dict[str, Any], client_action_id: str | None) -> None:
    if client_action_id:
        payload["processed_actions"].append(str(client_action_id))


def _parse_at(value: Any, now: datetime, label: str) -> datetime:
    if value is None or value == "":
        return now
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise EntryValidationError(f"Ungültiger Zeitpunkt ({label}): {value!r}") from exc
    if not isinstance(value, datetime):
        raise EntryValidationError(f"Ungültiger Zeitpunkt ({label}).")
    entries_service._check_year(value.year)
    at = to_utc_naive(value).replace(microsecond=0)
    if at > now + FUTURE_TOLERANCE:
        raise EntryValidationError(f"{label} liegt in der Zukunft.")
    return at


def _is_unterricht(db: Session, category_id: Any, subcategory_id: Any) -> bool:
    for cid in (subcategory_id, category_id):
        try:
            cat = db.get(Category, int(cid)) if cid is not None else None
        except (TypeError, ValueError):
            cat = None
        depth = 0
        while cat is not None and depth < 5:
            if cat.is_unterricht:
                return True
            cat, depth = (db.get(Category, cat.parent_id) if cat.parent_id else None), depth + 1
    return False


def timer_flags(db: Session, flags: Any, category_id: Any, subcategory_id: Any = None) -> list[str]:
    """Validate the flags of a timer start (see :data:`TIMER_FLAGS`); returns them in canonical order."""
    if flags is None or flags == "":
        return []
    if isinstance(flags, str):
        flags = [flags]
    if not isinstance(flags, (list, tuple, set)):
        raise EntryValidationError("Kennzeichen müssen eine Liste sein.")
    wanted = {str(f).strip() for f in flags if str(f).strip()}
    unknown = sorted(wanted - set(TIMER_FLAGS))
    if unknown:
        raise EntryValidationError(f"Kennzeichen „{unknown[0]}“ ist beim Timer nicht möglich.")
    if wanted & set(TIMER_FLAGS_UNTERRICHT) and not _is_unterricht(db, category_id, subcategory_id):
        raise EntryValidationError("„Vertretung angeordnet/genehmigt“ ist nur bei Unterricht möglich.")
    return [f for f in TIMER_FLAGS if f in wanted]


def _parse_hhmm(value: Any, default: time) -> time:
    try:
        return time.fromisoformat(str(value))
    except (TypeError, ValueError):
        return default


def default_location(db: Session, at: datetime) -> str:
    """Location for a timer start at ``at`` (UTC) according to ``settings.ui_default_location_rule``."""
    settings = db.get(Settings, 1)
    rule = dict(DEFAULT_LOCATION_RULE)
    if settings is not None and isinstance(settings.ui_default_location_rule, dict):
        rule.update({k: v for k, v in settings.ui_default_location_rule.items() if v is not None})
    local = to_local(at)
    weekdays = {int(d) for d in rule.get("weekdays") or []}
    start = _parse_hhmm(rule.get("from"), time(7, 0))
    end = _parse_hhmm(rule.get("to"), time(14, 0))
    inside = local.isoweekday() in weekdays and start <= local.time().replace(tzinfo=None) < end
    valid = {loc.value for loc in Location}
    value = rule.get("inside") if inside else rule.get("outside")
    if value not in valid:
        value = Location.SCHULE.value if inside else Location.ZUHAUSE.value
    return value


# --------------------------------------------------------------------------- status


def status(db: Session, *, now: datetime | None = None) -> TimerStatus:
    """Running entry, elapsed seconds, pause info and warnings (> 6 h, crossed midnight)."""
    now = _now(now)
    running = entries_service.get_running_entry(db)
    pause = _payload(_state(db))["pause"]
    warnings: list[str] = []
    elapsed = 0
    crosses = False
    if running is not None:
        elapsed = max(0, int((now - running.start_at).total_seconds()))
        if elapsed > LONG_RUNNING_SECONDS:
            warnings.append(WARNING_LONG_RUNNING)
        crosses = local_date(now) > running.day
        if crosses:
            warnings.append(WARNING_MIDNIGHT)
    return TimerStatus(running=running, elapsed_seconds=elapsed, paused=pause, warnings=warnings,
                       crosses_midnight=crosses, server_time=now)


def _result(db: Session, now: datetime, *, stopped: list[TimeEntry] | None = None,
            duplicate: bool = False, auto_ended: bool = False, capped: TimeEntry | None = None) -> TimerStatus:
    result = status(db, now=now)
    result.stopped = list(stopped or [])
    result.duplicate = duplicate
    result.auto_ended = auto_ended
    if auto_ended:
        result.warnings.append(WARNING_AUTO_ENDED)
    if capped is not None:
        result.notices.append(_capped_warning(capped))
    return result


def capped_end(running: TimeEntry, at: datetime) -> tuple[datetime, bool]:
    """End time for finishing ``running`` at ``at``: ``(end, auto_ended)`` (see AUTO_END_AFTER_MIDNIGHT)."""
    midnight = local_midnight_utc(running.day + timedelta(days=1)).replace(tzinfo=None)
    if at > midnight + AUTO_END_AFTER_MIDNIGHT:
        return max(midnight, running.start_at), True
    return at, False


def _finish(db: Session, running: TimeEntry, at: datetime, now: datetime
            ) -> tuple[list[TimeEntry], bool, TimeEntry | None]:
    """Finish ``running`` at ``at`` (midnight rule, then capped at the next entry that starts meanwhile).

    Returns ``(stored parts, auto_ended, next entry the timer was capped at or None)``.
    """
    end, auto_ended = capped_end(running, at)
    nxt = entries_service.next_entry_start(db, running.start_at, end, exclude=[running.id])
    if nxt is not None:
        end = nxt.start_at
    return entries_service.finish_running_entry(db, end, now=now, commit=False), auto_ended, nxt


def _capped_warning(nxt: TimeEntry) -> str:
    code = nxt.category.code if nxt.category is not None else "?"
    return (
        f"Der Timer wurde um {to_local(nxt.start_at):%H:%M} beendet, weil dort bereits ein Eintrag ({code}) "
        "beginnt."
    )


# --------------------------------------------------------------------------- actions


def _stop_running_at(db: Session, at: datetime, now: datetime
                     ) -> tuple[list[TimeEntry], bool, TimeEntry | None]:
    running = entries_service.get_running_entry(db)
    if running is None:
        return [], False, None
    if at < running.start_at:
        raise TimerConflict(
            "Es läuft bereits ein Timer, der nach diesem Zeitpunkt gestartet wurde "
            f"({to_local(running.start_at):%d.%m.%Y %H:%M}). Bitte Konflikt manuell lösen."
        )
    return _finish(db, running, at, now)


def start(
    db: Session,
    category_id: int,
    *,
    subcategory_id: int | None = None,
    klasse: str | None = None,
    fach: str | None = None,
    location: str | None = None,
    started_at: datetime | str | None = None,
    note: str | None = None,
    flags: list[str] | None = None,
    client_action_id: str | None = None,
    now: datetime | None = None,
    _location_explicit: bool | None = None,
) -> TimerStatus:
    """Start a timer at ``started_at`` (device time, default now); a running timer is stopped there first.

    ``flags``: optional :data:`TIMER_FLAGS` (Vertretung only for Unterricht), validated before anything changes.
    """
    now = _now(now)
    if _is_duplicate(db, client_action_id):
        return _result(db, now, duplicate=True)
    at = _parse_at(started_at, now, "Startzeit")
    start_flags = timer_flags(db, flags, category_id, subcategory_id)
    try:
        stopped, auto_ended, capped = _stop_running_at(db, at, now)
        explicit = bool(location) if _location_explicit is None else _location_explicit
        data = {
            "start_at": at,
            "end_at": None,
            "has_bounds": True,
            "category_id": category_id,
            "subcategory_id": subcategory_id,
            "klasse": klasse,
            "fach": fach,
            "location": location or default_location(db, at),
            "note": note,
            "flags": start_flags,
        }
        created = entries_service.create_entry(db, data, source=EntrySource.TIMER.value, now=now, commit=False)
        state = _state(db)
        payload = _payload(state)
        payload["pause"] = None
        payload["running"] = {"entry_id": created[0].id, "location_explicit": explicit}
        _remember(payload, client_action_id)
        _save_payload(state, payload, now)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return _result(db, now, stopped=stopped, auto_ended=auto_ended, capped=capped)


def switch(db: Session, category_id: int, **kwargs: Any) -> TimerStatus:
    """Change category: stop the running entry and start a new one (``at`` is an alias of ``started_at``)."""
    if "at" in kwargs:
        at = kwargs.pop("at")
        kwargs.setdefault("started_at", at)
    return start(db, category_id, **kwargs)


def stop(
    db: Session,
    stopped_at: datetime | str | None = None,
    *,
    client_action_id: str | None = None,
    now: datetime | None = None,
) -> TimerStatus:
    """Stop the running timer. While paused (nothing running) it ends the pause; otherwise 409."""
    now = _now(now)
    if _is_duplicate(db, client_action_id):
        return _result(db, now, duplicate=True)
    at = _parse_at(stopped_at, now, "Endzeit")
    state = _state(db)
    payload = _payload(state)
    running = entries_service.get_running_entry(db)
    if running is None and payload["pause"] is None:
        raise TimerNotRunning("Es läuft kein Timer.")
    auto_ended = False
    capped = None
    try:
        stopped: list[TimeEntry] = []
        if running is not None:
            if at < running.start_at:
                raise EntryValidationError("Die Endzeit liegt vor dem Start des Timers.")
            stopped, auto_ended, capped = _finish(db, running, at, now)
        payload["pause"] = None
        payload["running"] = None
        _remember(payload, client_action_id)
        _save_payload(state, payload, now)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return _result(db, now, stopped=stopped, auto_ended=auto_ended, capped=capped)


def pause(
    db: Session,
    at: datetime | str | None = None,
    *,
    client_action_id: str | None = None,
    now: datetime | None = None,
) -> TimerStatus:
    """Stop the running entry and remember category + payload for :func:`resume`."""
    now = _now(now)
    if _is_duplicate(db, client_action_id):
        return _result(db, now, duplicate=True)
    when = _parse_at(at, now, "Pausenbeginn")
    running = entries_service.get_running_entry(db)
    if running is None:
        raise TimerNotRunning("Es läuft kein Timer, der pausiert werden kann.")
    if when < running.start_at:
        raise EntryValidationError("Der Pausenbeginn liegt vor dem Start des Timers.")
    running_info = _payload(_state(db)).get("running") or {}
    if running_info.get("entry_id") == running.id:
        explicit = bool(running_info.get("location_explicit"))
    else:  # started elsewhere (e.g. created directly): explicit if it differs from the default
        explicit = running.location != default_location(db, running.start_at)
    remembered = {
        "category_id": running.category_id,
        "subcategory_id": running.subcategory_id,
        "klasse": running.klasse,
        "fach": running.fach,
        "location": running.location,
        "location_explicit": bool(explicit),
        "note": running.note,
        "flags": [f for f in TIMER_FLAGS if f in (running.flags or [])],
        "paused_at": when.isoformat() + "Z",
    }
    try:
        stopped, auto_ended, capped = _finish(db, running, when, now)
        state = _state(db)
        payload = _payload(state)
        payload["pause"] = remembered
        payload["running"] = None
        _remember(payload, client_action_id)
        _save_payload(state, payload, now)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return _result(db, now, stopped=stopped, auto_ended=auto_ended, capped=capped)


def resume(
    db: Session,
    at: datetime | str | None = None,
    *,
    client_action_id: str | None = None,
    now: datetime | None = None,
) -> TimerStatus:
    """Start a new entry with the category/payload remembered by :func:`pause`."""
    now = _now(now)
    if _is_duplicate(db, client_action_id):
        return _result(db, now, duplicate=True)
    pause_info = _payload(_state(db))["pause"]
    if not pause_info or not pause_info.get("category_id"):
        raise TimerNotRunning("Es gibt keinen pausierten Timer.")
    explicit = bool(pause_info.get("location_explicit"))
    return start(
        db,
        pause_info["category_id"],
        subcategory_id=pause_info.get("subcategory_id"),
        klasse=pause_info.get("klasse"),
        fach=pause_info.get("fach"),
        location=pause_info.get("location") if explicit else None,
        started_at=at,
        note=pause_info.get("note"),
        flags=pause_info.get("flags") or None,
        client_action_id=client_action_id,
        now=now,
        _location_explicit=explicit,
    )
