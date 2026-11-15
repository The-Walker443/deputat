"""Quick entry ("Erfassen"): one duration per day, the lowest possible hurdle for recording at all.

The home screen shows a week Mo–So with one field per day. The field shows the day's **total** recorded
work: the sum of all active, non-deleted, finished entries of the day (timer, manual, import, confirmed
timetable) except pseudo categories (Krank 'KR' only neutralises the soll). A *running* timer is not part
of the field (the row says "Timer läuft"); once stopped, its entry counts like any other entry.

Entering a value sets that total. The app keeps exactly one **Tagessumme** entry per day for it:

* marked ``time_entry.tagessumme`` (migration 0006), duration-only (``has_bounds=False`` ->
  ``ohne_zeitgrenzen``), ``source='manuell'``, no subcategory; its main category follows the **day type**
  (see below): ``AZ`` ("Arbeitszeit (pauschal, nicht aufgeschlüsselt)", :data:`app.db.seed.TAGESSUMME_CODE`)
  for a normal day, ``F`` for Fortbildung, ``SV`` for Klassenfahrt (plus the flag ``klassenfahrt``) and
  Schulveranstaltung;
* ``duration = entered total − sum of all other entries of the day`` ("detail");
* totals are compared in whole minutes as displayed (``fmt_hm``): the displayed total again is a no-op,
  the displayed detail sum, 0 or empty soft-deletes the Tagessumme entry (nothing else is ever touched),
  anything below the detail sum -> :class:`DayTotalBelowDetail` (409).

Every write goes through :mod:`app.services.entries` (revisions, ``nachtraeglich`` > 48 h – also when a
Tagessumme is *changed* later –, locks of closed months -> :class:`~app.services.entries.EntryLocked`
without ``reason``). Writes are serialised in the process (one uvicorn process, SQLite), so two requests at
the same moment (e.g. two tabs flushing the same offline queue) cannot create two Tagessummen.

Offline: the client sends the device time of the input (``at``). It is used for the 48-h rule, and a
*delayed* request (``at`` more than :data:`DELAYED_AFTER` before the server time) whose ``at`` is older than
the Tagessumme's last change is ignored (``action='veraltet'``): last write wins by input time, so a value
queued on one device cannot overwrite a newer one from another device.

Entries added *after* the Tagessumme (a stopped timer, a confirmed timetable suggestion, ...) count on top.
:func:`week_view` reports them per day (``nachgetragen_seconds`` / ``prompt``) so the screen can ask once
"keep the total or take the new sum?" – nothing is rewritten silently. Sending the current total again
confirms the new sum (``action='bestaetigt'``).

If several entries qualify as Tagessumme, the newest *marked* one is the Tagessumme and the others count as
detail. Unmarked duration-only ``AZ`` entries (no subcategory, source 'manuell' – e.g. created by hand in
*Einträge*, or restored from a backup taken before 0006) still qualify when no marked entry exists (the rule
before 0006); writing such an entry marks it. A duration-only ``F``/``SV`` entry from the entry form never
qualifies. Changing a Tagessumme's category or giving it bounds in *Einträge* turns it into an ordinary entry.

**Day type** (:func:`set_day_typ`, ``PUT /api/days/{date}/typ``): ``unterricht`` (default), ``krank`` (whole
day without hours: stored as calendar absence ``krank``, the Tagessumme is removed – individual entries
stay and count on top), ``fortbildung`` (calendar absence ``fortbildung``: soll unchanged, no timetable
suggestions, not a leave candidate; Tagessumme category ``F``), ``klassenfahrt`` (``day_override.erfassung_typ``;
Tagessumme ``SV`` + flag ``klassenfahrt`` -> the Klassenfahrt variant/cap applies) and ``schulveranstaltung``
(``erfassung_typ``; Tagessumme ``SV``). Switching only changes the Tagessumme's category/flags (revision);
individually recorded entries keep theirs. Days with an absence *entered* in the calendar that the screen does not
manage (Dienstbefreiung, Sonderurlaub, Elternzeit, manually assigned Urlaub) cannot be switched here
(:class:`DayTypLocked`); leave of the automatic plan can (:func:`fixed_absence`). Only *marked* Tagessummen are
retyped (a hand-made AZ entry becomes the Tagessumme only through a value write). Krank set in the calendar removes
the Tagessumme like the chip (:func:`apply_calendar_absences`). A Klassenfahrt day is a trip day for the ledger
whatever entries it holds (``ledger.klassenfahrt_days``), so the row's cap hint comes from the ledger.
Tagessumme writes do not check whether F/SV are deactivated or need a note (the type is an explicit choice).

Read side: :func:`week_view` / :func:`day_view` combine the raw recorded totals with the account figures of
:mod:`app.services.ledger` (soll, Ist incl. Krank-Gutschrift, saldo) of each day's own period.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.calendar import build_days, holidays_between
from app.core.parsing import parse_duration
from app.core.types import Day
from app.db.models import Category, DayOverride, EntrySource, EntryStatus, MonthClose, Period, TimeEntry
from app.db.seed import TAGESSUMME_CODE
from app.services import entries as entries_svc
from app.services.entries import USER_FLAGS, EntryConflict, EntryLocked, EntryValidationError
from app.util import fmt_hm, local_midnight_utc, now_utc, to_utc_naive, today_local

log = logging.getLogger("app.services.quick_entry")

MAX_DAY_TOTAL = 24 * 3600  # a normal day; DST days have 23 h / 25 h (:func:`day_length`)
MAX_ANY_DAY = 25 * 3600

# Device time ("at") of an input: accepted up to 5 min in the future (clock skew) and up to 14 days back
# (an older queue is implausible -> server time). A request is "delayed" (it waited in the offline queue)
# when its input time lies more than DELAYED_AFTER before the server time.
AT_FUTURE_TOLERANCE = timedelta(minutes=5)
AT_MAX_AGE = timedelta(days=14)
DELAYED_AFTER = timedelta(minutes=2)

# Serialises every write that touches the Tagessummen: the two "Erfassen" endpoints and the calendar changes that
# retype or remove them (calendar_service). Re-entrant: a type change runs the calendar change inside the lock.
_WRITE_LOCK = threading.RLock()

WEEKDAYS_SHORT = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
WEEKDAYS_LONG = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
MONTHS = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember")

ABSENCE_BADGES = {
    "urlaub": "Urlaub",
    "krank": "Krank",
    "fortbildung": "Fortbildung",
    "dienstbefreiung": "Dienstbefreiung",
    "sonderurlaub": "Sonderurlaub",
    "elternzeit": "Elternzeit",
}

# --------------------------------------------------------------------------- day types ("Typ" chip)
TYP_UNTERRICHT = "unterricht"
TYP_KRANK = "krank"
TYP_FORTBILDUNG = "fortbildung"
TYP_KLASSENFAHRT = "klassenfahrt"
TYP_SCHULVERANSTALTUNG = "schulveranstaltung"
DAY_TYPES = (TYP_UNTERRICHT, TYP_KRANK, TYP_FORTBILDUNG, TYP_KLASSENFAHRT, TYP_SCHULVERANSTALTUNG)
TYP_LABELS = {
    TYP_UNTERRICHT: "Unterricht",
    TYP_KRANK: "Krank",
    TYP_FORTBILDUNG: "Fortbildung",
    TYP_KLASSENFAHRT: "Klassenfahrt",
    TYP_SCHULVERANSTALTUNG: "Schulveranstaltung",
}
# Types stored as calendar absence (day_override.absence_type) and types stored in day_override.erfassung_typ.
ABSENCE_TYPES = frozenset({TYP_KRANK, TYP_FORTBILDUNG})
ERFASSUNG_TYPES = frozenset({TYP_KLASSENFAHRT, TYP_SCHULVERANSTALTUNG})
# Calendar absences the screen shows but does not change (link to the calendar instead) – Urlaub only when it was
# assigned by hand (:func:`fixed_absence`); leave of the automatic plan is shown as AUTO_LEAVE_LABEL and stays
# switchable (Krank/Fortbildung move it to another day).
FIXED_ABSENCES = frozenset({"urlaub", "dienstbefreiung", "sonderurlaub", "elternzeit"})
AUTO_LEAVE_LABEL = "Urlaub (autom.)"
AUTO_LEAVE_HINT = "Urlaub automatisch zugeordnet – bei Krank oder Fortbildung rückt er auf einen anderen Ferientag."
FLAG_KLASSENFAHRT = "klassenfahrt"
FLAG_NACHTBEREITSCHAFT = "nachtbereitschaft"
# (category code, klassenfahrt flag) of the Tagessumme per type; krank keeps none (only one left over from a
# calendar-set krank day, which stays as it is).
TYP_TARGET = {TYP_UNTERRICHT: (TAGESSUMME_CODE, False), TYP_KRANK: (TAGESSUMME_CODE, False),
              TYP_FORTBILDUNG: ("F", False), TYP_KLASSENFAHRT: ("SV", True), TYP_SCHULVERANSTALTUNG: ("SV", False)}
KRANK_REASON = "Tag als krank markiert"
# Chip text when no type is chosen: the calendar's own kind of day.
DAY_KIND_LABELS = {"schultag": "Unterricht", "ferientag": "Ferien", "feiertag": "Feiertag",
                   "wochenende": "Wochenende", "unterrichtsfrei": "Unterrichtsfrei"}
DAY_KIND_STYLE = {"schultag": "unterricht", "ferientag": "ferien", "feiertag": "feiertag",
                  "wochenende": "wochenende", "unterrichtsfrei": "frei"}
NO_SOLL_KINDS = frozenset({"wochenende", "feiertag"})

# The pre-0006 identification of a Tagessumme (same statement as migration 0006): used after restoring a JSON
# backup taken before 0006, whose time_entry rows carry no "tagessumme" column.
_LEGACY_CRITERIA = (
    "{t}.category_id = (SELECT id FROM category WHERE code = 'AZ' AND parent_id IS NULL) "
    "AND {t}.subcategory_id IS NULL AND {t}.has_bounds = 0 AND {t}.source = 'manuell' "
    "AND {t}.status = 'active' AND {t}.deleted_at IS NULL AND {t}.end_at IS NOT NULL"
)
LEGACY_TAGESSUMME_SQL = (
    "UPDATE time_entry SET tagessumme = 1 WHERE id IN ("
    "SELECT t.id FROM time_entry t WHERE " + _LEGACY_CRITERIA.format(t="t") + " "
    "AND NOT EXISTS (SELECT 1 FROM time_entry n WHERE n.day = t.day AND " + _LEGACY_CRITERIA.format(t="n") + " "
    "AND (n.created_at > t.created_at OR (n.created_at = t.created_at AND n.id > t.id))))"
)


class DayTypLocked(EntryConflict):
    """The day carries a calendar absence the "Erfassen" screen does not manage (HTTP 409)."""


def round_minute(seconds: int) -> int:
    """Seconds rounded to whole minutes exactly like the display (``fmt_hm``: half up)."""
    return int((int(seconds) + 30) // 60) * 60


def day_length(d: date) -> int:
    """Length of the local day in seconds (82800 / 86400 / 90000 on DST days)."""
    return int((local_midnight_utc(d + timedelta(days=1)) - local_midnight_utc(d)).total_seconds())


class DayTotalBelowDetail(EntryConflict):
    """The entered total is smaller than the entries recorded individually for the day (HTTP 409)."""

    def __init__(self, detail_seconds: int):
        minimum = fmt_hm(detail_seconds)
        super().__init__(f"Für diesen Tag sind bereits {minimum} h einzeln erfasst – bitte mindestens {minimum} "
                         "eingeben.")
        self.detail_seconds = detail_seconds
        self.min_seconds = round_minute(detail_seconds)


# =========================================================================== input

_INVALID = "Bitte eine Dauer wie 7,5 oder 7,30 eingeben."
_SPACE_HM_RE = re.compile(r"^(\d{1,2})\s+(\d{2})$")  # "7 30"
_TWO_DIGITS_RE = re.compile(r"^(\d{1,2})?[.,](\d{2})\s*(?:h|std\.?|stunden?)?$")  # "7.30" / "7,30"
_HHMM_RE = re.compile(r"^(\d{1,2})(\d{2})$")  # "730" -> hint "7:30?"
# Two digits after the separator are minutes ("7.30" as in "7.30 Uhr") – except the quarter hours written as
# decimals, which are just as common: "7,25" = 7¼ h, "7,50" = 7½ h ("7,75" is decimal anyway: 75 > 59).
DECIMAL_TWO_DIGITS = frozenset({"25", "50"})


def parse_total(value: Any, *, max_seconds: int = MAX_DAY_TOTAL) -> int:
    """User input -> seconds, rounded to whole minutes. Empty / None -> 0 (= no entry).

    * ``"7"``, ``"7,5"`` / ``"7.5"`` (decimal hours), ``"7:30"``, ``"7h30"``, ``"8 h"``, ``"450 min"``;
    * two digits after ``,`` / ``.`` are **minutes**: ``"7,30"`` / ``"7.30"`` = 7:30, ``"7,45"`` = 7:45 –
      except ``,25`` / ``,50`` (quarter hours as decimals: 7:15 / 7:30) and values above 59 (``"7,75"``);
    * ``"7 30"`` (with a space) = 7:30;
    * numbers are hours.

    Raises :class:`EntryValidationError` for anything else, negative values and more than ``max_seconds``
    (with a hint for "730": "Meintest du 7:30?").
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return 0
    if isinstance(value, bool):
        raise EntryValidationError(_INVALID)
    if not isinstance(value, str):
        try:
            seconds = parse_duration(value)
        except (ValueError, TypeError):
            raise EntryValidationError(_INVALID) from None
        return check_total(round_minute(seconds), max_seconds=max_seconds)
    text = value.strip().casefold()
    if text.startswith("-"):
        raise EntryValidationError("Die Dauer darf nicht negativ sein.")
    hint = None
    m = _SPACE_HM_RE.match(text)
    if m is None:
        m2 = _TWO_DIGITS_RE.match(text)
        if m2 is not None and m2[2] not in DECIMAL_TWO_DIGITS and int(m2[2]) <= 59:
            m = m2
    if m is not None:
        if int(m[2]) > 59:
            raise EntryValidationError("Minuten bitte von 00 bis 59 angeben, z. B. 7,30.")
        seconds = int(m[1] or 0) * 3600 + int(m[2]) * 60
    else:
        compact = re.sub(r"\s+", "", text)  # "8 h" -> "8h"
        try:
            seconds = parse_duration(compact)
        except ValueError:
            raise EntryValidationError(_INVALID) from None
        if (hm := _HHMM_RE.match(compact)) and int(hm[1]) <= 24 and int(hm[2]) <= 59:
            hint = (int(hm[1]), hm[2])
    seconds = round_minute(seconds)
    if hint and seconds > max_seconds:
        raise EntryValidationError(f"{_too_long(max_seconds)} Meintest du {hint[0]}:{hint[1]}? "
                                   f"Dann bitte {hint[0]},{hint[1]} eingeben.")
    return check_total(seconds, max_seconds=max_seconds)


def _too_long(max_seconds: int) -> str:
    if max_seconds == MAX_DAY_TOTAL:
        return "Ein Tag hat höchstens 24 Stunden."
    return f"Dieser Tag hat {max_seconds // 3600} Stunden (Zeitumstellung)."


def check_total(seconds: Any, *, max_seconds: int = MAX_DAY_TOTAL) -> int:
    """Validate a total given in seconds (0 .. ``max_seconds``, whole number)."""
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise EntryValidationError("Ungültige Dauer.")
    if seconds < 0:
        raise EntryValidationError("Die Dauer darf nicht negativ sein.")
    if seconds > max_seconds:
        raise EntryValidationError(_too_long(max_seconds))
    return seconds


def _parse_at(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError:
            raise EntryValidationError("Ungültiger Zeitpunkt der Eingabe.") from None
    if not isinstance(value, datetime):
        raise EntryValidationError("Ungültiger Zeitpunkt der Eingabe.")
    return to_utc_naive(value)


def input_time(at: Any, now: datetime, sent_at: Any = None) -> tuple[datetime, bool]:
    """Device time of the input -> ``(effective time, delayed)`` (naive UTC).

    ``None`` / implausibly old -> server time. More than 5 min in the future -> :class:`EntryValidationError`.

    ``sent_at``: the device time at which the request was *sent* (header ``X-Sent-At`` of the offline queue).
    Both times come from the same device clock, so ``sent_at - at`` is the real waiting time whatever the
    clock's offset: the input happened that long before ``now``, and only a wait of more than
    :data:`DELAYED_AFTER` makes the request "delayed" (a device whose clock is a few minutes slow is not).
    """
    if at is None or at == "":
        return now, False
    at = _parse_at(at)
    if sent_at not in (None, ""):
        try:
            age = _parse_at(sent_at) - at
        except EntryValidationError:
            age = None
        if age is not None and age >= timedelta(0):
            if age > AT_MAX_AGE:
                return now, True
            return (now - age).replace(microsecond=0), age > DELAYED_AFTER
    at = at.replace(microsecond=0)
    if at > now + AT_FUTURE_TOLERANCE:
        raise EntryValidationError("Der Zeitpunkt der Eingabe liegt in der Zukunft – Uhr des Geräts prüfen.")
    if at < now - AT_MAX_AGE:
        return now, True
    at = min(at, now)
    return at, now - at > DELAYED_AFTER


# =========================================================================== day state


def main_category(db: Session, code: str) -> Category:
    cat = db.scalars(select(Category).where(Category.code == code, Category.parent_id.is_(None))).first()
    if cat is None:  # the seed runs on every start; only a broken catalogue gets here
        raise EntryValidationError(f"Kategorie „{code}“ fehlt – App neu starten.")
    return cat


def tagessumme_category(db: Session) -> Category:
    return main_category(db, TAGESSUMME_CODE)


def is_tagessumme(entry: TimeEntry, az_id: int) -> bool:
    """Candidate for the day's Tagessumme: marked (0006), or an unmarked duration-only AZ entry (old rule)."""
    if entry.has_bounds or entry.status != EntryStatus.ACTIVE.value or entry.deleted_at is not None:
        return False
    if entry.tagessumme:
        return True
    return (
        entry.category_id == az_id
        and entry.subcategory_id is None
        and entry.source == EntrySource.MANUELL.value
    )


def mark_legacy_tagessummen(db: Session) -> int:
    """Mark the Tagessummen of data from before migration 0006 (e.g. a restored JSON backup); commits."""
    count = db.execute(text(LEGACY_TAGESSUMME_SQL)).rowcount or 0
    db.commit()
    return count


def stored_typ(override: DayOverride | None) -> str:
    """The day type chosen for a day (``unterricht`` if none): calendar absence krank/fortbildung first."""
    if override is None:
        return TYP_UNTERRICHT
    if override.absence_type in ABSENCE_TYPES:
        return str(override.absence_type)
    if override.erfassung_typ in ERFASSUNG_TYPES:
        return str(override.erfassung_typ)
    return TYP_UNTERRICHT


def fixed_absence(override: DayOverride | None, absence: str | None) -> str | None:
    """The day's absence if the screen must not change it (link to the calendar instead), else ``None``.

    Fixed are absences the user *entered* in the calendar: Dienstbefreiung, Sonderurlaub, Elternzeit (only
    possible as override) and manually assigned leave (``urlaub_manuell``). Leave the automatic leave plan put on
    the day (e.g. Buß- und Bettag, Ferien weekdays) is not fixed: Krank/Fortbildung move it to another candidate,
    exactly as the calendar form does.
    """
    if absence not in FIXED_ABSENCES:
        return None
    if absence == "urlaub" and not (override is not None and override.urlaub_manuell):
        return None
    return absence


def tagessumme_target(override: DayOverride | None) -> tuple[str, bool]:
    """(category code, klassenfahrt flag) the day's Tagessumme should have."""
    if override is not None and override.absence_type == TYP_FORTBILDUNG:
        return "F", False
    if override is not None and override.erfassung_typ == TYP_KLASSENFAHRT:
        return "SV", True
    if override is not None and override.erfassung_typ == TYP_SCHULVERANSTALTUNG:
        return "SV", False
    return TAGESSUMME_CODE, False


@dataclass
class DayState:
    """Raw recorded work of one day, split into the Tagessumme entry and everything else."""

    day: date
    tagessumme: TimeEntry | None
    detail_seconds: int  # all other finished work entries
    detail_count: int
    running: bool  # a running timer covers this day (not part of the total)
    running_seconds: int = 0
    later_seconds: int = 0  # detail entries added/changed after the Tagessumme's last change
    nb_seconds: int = 0  # detail entries flagged nachtbereitschaft (variant B drops them)

    @property
    def tagessumme_seconds(self) -> int:
        return int(self.tagessumme.duration_seconds or 0) if self.tagessumme is not None else 0

    @property
    def total(self) -> int:
        return self.detail_seconds + self.tagessumme_seconds


def _running_parts(start_at: datetime, now: datetime) -> list[tuple[date, int]]:
    from app.services.ledger import running_parts

    return running_parts(start_at, now)


def day_states(db: Session, start: date, end: date, *, now: datetime | None = None) -> dict[date, DayState]:
    """:class:`DayState` for every day ``start..end`` (two queries)."""
    now = now or now_utc()
    cats = {c.id: c for c in db.scalars(select(Category))}
    az = next((c for c in cats.values() if c.code == TAGESSUMME_CODE and c.parent_id is None), None)
    az_id = az.id if az is not None else -1

    def pseudo(entry: TimeEntry) -> bool:
        cat = cats.get(entry.category_id)
        return bool(cat is not None and cat.is_pseudo)

    active = (TimeEntry.status == EntryStatus.ACTIVE.value, TimeEntry.deleted_at.is_(None))
    rows = list(
        db.scalars(
            select(TimeEntry)
            .where(TimeEntry.day.between(start, end), TimeEntry.end_at.is_not(None), *active)
            .order_by(TimeEntry.created_at, TimeEntry.id)
        )
    )
    # A running timer may have started on an earlier day (forgotten over midnight): its elapsed time is
    # shown per local day, exactly as the ledger books it – but it is not part of the field's total, so a
    # day total entered meanwhile does not depend on the moment it reaches the server.
    running_rows = list(db.scalars(select(TimeEntry).where(TimeEntry.end_at.is_(None), TimeEntry.day <= end, *active)))
    candidates: dict[date, list[TimeEntry]] = defaultdict(list)
    details: dict[date, list[TimeEntry]] = defaultdict(list)
    running: dict[date, int] = defaultdict(int)
    for e in running_rows:
        if pseudo(e):
            continue
        for part_day, part_seconds in _running_parts(e.start_at, now):
            if start <= part_day <= end:
                running[part_day] += part_seconds
    for e in rows:
        if pseudo(e):
            continue
        (candidates if is_tagessumme(e, az_id) else details)[e.day].append(e)

    result: dict[date, DayState] = {}
    d = start
    while d <= end:
        found = candidates.get(d, [])
        marked = [e for e in found if e.tagessumme]
        main = (marked or found)[-1] if found else None  # newest marked, else newest (ordered by created_at)
        detail = details.get(d, []) + [e for e in found if e is not main]
        later = 0
        if main is not None and main.updated_at is not None:
            later = sum(int(e.duration_seconds or 0) for e in detail
                        if (e.updated_at or e.created_at) is not None and (e.updated_at or e.created_at) > main.updated_at)
        result[d] = DayState(
            day=d,
            tagessumme=main,
            detail_seconds=sum(int(e.duration_seconds or 0) for e in detail),
            detail_count=len(detail),
            running=d in running,
            running_seconds=running.get(d, 0),
            later_seconds=later,
            nb_seconds=sum(int(e.duration_seconds or 0) for e in detail if FLAG_NACHTBEREITSCHAFT in (e.flags or [])),
        )
        d += timedelta(days=1)
    return result


def day_state(db: Session, d: date, *, now: datetime | None = None) -> DayState:
    return day_states(db, d, d, now=now)[d]


# =========================================================================== write


def set_day_total(
    db: Session,
    day: date,
    seconds: int,
    *,
    client_action_id: str | None = None,
    reason: str | None = None,
    at: Any = None,
    sent_at: Any = None,
    now: datetime | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Set the recorded total of ``day`` to ``seconds`` (see module docstring); returns :func:`week_view`
    plus ``day`` and ``action`` (angelegt | geaendert | geloescht | unveraendert | bestaetigt | veraltet).

    ``at`` is the device time of the input (offline queue), ``client_action_id`` is echoed back. Repeating a
    request is harmless (same total -> no write, no revision). ``reason`` is required in a closed month.
    """
    entries_svc._check_year(day.year)
    seconds = check_total(seconds, max_seconds=day_length(day))
    now = to_utc_naive(now or now_utc()).replace(microsecond=0)
    when, delayed = input_time(at, now, sent_at)

    with _WRITE_LOCK:
        if db.in_transaction():
            db.commit()  # end an open read snapshot: see what a request that held the lock just wrote
        action = _apply_total(db, day, seconds, reason=reason, now=now, when=when, delayed=delayed)

    result = week_view(db, monday_of(day), today=today, now=now)
    result["day"] = next(x for x in result["days"] if x["date"] == day.isoformat())
    result["action"] = action
    result["client_action_id"] = client_action_id
    return result


def _apply_total(db: Session, day: date, seconds: int, *, reason: str | None, now: datetime, when: datetime,
                 delayed: bool) -> str:
    state = day_state(db, day, now=now)
    ts = state.tagessumme
    if ts is not None and delayed and ts.updated_at is not None and when < ts.updated_at:
        return "veraltet"  # a queued value older than the last change (e.g. from another device)
    override = db.get(DayOverride, day)
    if (delayed and override is not None and stored_typ(override) == TYP_KRANK
            and override.erfassung_typ_at is not None and when < override.erfassung_typ_at):
        return "veraltet"  # typed before the day was marked krank (e.g. on another device)

    if seconds == round_minute(state.total):
        if ts is not None and state.later_seconds and not ts.locked and entries_svc._month_close(db, day) is None:
            ts.updated_at = now  # "the new sum is right": confirms the entries added later (no content change)
            db.commit()
            return "bestaetigt"
        return "unveraendert"

    floor = round_minute(state.detail_seconds)
    if seconds < floor:
        raise DayTotalBelowDetail(state.detail_seconds)
    target = 0 if seconds == floor else seconds - state.detail_seconds

    if target == 0:
        if ts is None:
            return "unveraendert"
        entries_svc.delete_entry(db, ts.id, reason=reason, now=when)
        return "geloescht"
    code, klassenfahrt = tagessumme_target(override)
    if ts is None:
        entries_svc.create_entry(
            db,
            {"has_bounds": False, "day": day, "duration_seconds": target, "category_id": main_category(db, code).id,
             "flags": [FLAG_KLASSENFAHRT] if klassenfahrt else []},
            source=EntrySource.MANUELL.value,
            reason=reason,
            now=when,
            tagessumme=True,
        )
        return "angelegt"
    if int(ts.duration_seconds or 0) == target:
        return "unveraendert"
    changes: dict[str, Any] = {"duration_seconds": target, **_type_changes(db, ts, code, klassenfahrt)}
    entries_svc.update_entry(db, ts.id, changes, reason=reason, now=when, recorded_now=True, tagessumme=True)
    return "geaendert"


def _type_changes(db: Session, ts: TimeEntry, code: str, klassenfahrt: bool) -> dict[str, Any]:
    """Changes that give the Tagessumme ``ts`` the category/flag of its day type (empty if it has them)."""
    category = main_category(db, code)
    current = set(ts.flags or [])
    user = (current & USER_FLAGS) - {FLAG_KLASSENFAHRT}
    if klassenfahrt:
        user.add(FLAG_KLASSENFAHRT)
    changes: dict[str, Any] = {}
    if ts.category_id != category.id or ts.subcategory_id is not None:
        changes.update(category_id=category.id, subcategory_id=None)
    if (FLAG_KLASSENFAHRT in current) != klassenfahrt:
        changes["flags"] = sorted(user)
    return changes


def _retype(db: Session, ts: TimeEntry, code: str, klassenfahrt: bool, *, reason: str | None, now: datetime) -> bool:
    """Switch the Tagessumme to the category/flag of its day type (no commit). The value is unchanged, so
    ``updated_at`` (last change of the *value*: "veraltet", "added later" prompt) is kept."""
    changes = _type_changes(db, ts, code, klassenfahrt)
    if not changes:
        return False
    kept = ts.updated_at
    entries_svc.update_entry(db, ts.id, changes, reason=reason, now=now, commit=False, tagessumme=True)
    ts.updated_at = kept
    db.flush()
    return True


def sync_tagessumme_types(db: Session, start: date, end: date, *, reason: str | None = None,
                          now: datetime | None = None) -> int:
    """Bring every Tagessumme in ``[start, end]`` in line with its day type (after a calendar change, e.g.
    Fortbildung set in the calendar -> category F). Locked days without ``reason`` are skipped. Commits.

    Only *marked* Tagessummen (``time_entry.tagessumme``) are touched: an unmarked duration-only AZ entry made by
    hand in *Einträge* is never retyped silently (it becomes the Tagessumme only through a value write)."""
    now = to_utc_naive(now or now_utc()).replace(microsecond=0)
    with _WRITE_LOCK:
        if db.in_transaction():
            db.commit()
        states = day_states(db, start, end, now=now)
        if not any(st.tagessumme is not None and st.tagessumme.tagessumme for st in states.values()):
            return 0
        overrides = {o.date: o for o in db.scalars(select(DayOverride).where(DayOverride.date.between(start, end)))}
        changed = 0
        for d, st in states.items():
            if st.tagessumme is None or not st.tagessumme.tagessumme:
                continue
            code, klassenfahrt = tagessumme_target(overrides.get(d))
            try:
                changed += _retype(db, st.tagessumme, code, klassenfahrt, reason=reason, now=now)
            except EntryLocked:
                log.info("Tagessumme vom %s nicht angepasst: Monat abgeschlossen", d)
        db.commit()
    return changed


def apply_calendar_absences(db: Session, before: dict[date, str | None], *, reason: str | None = None,
                            now: datetime | None = None) -> int:
    """After a calendar change of the absences (``before``: each changed day's previous absence), before its
    commit: a day that became **krank** loses its (marked) Tagessumme – soft delete with revision, reason
    :data:`KRANK_REASON` – and its "Erfassen" type, exactly like Krank chosen on the chip. Otherwise the day total
    would count on top of the credited soll while the field on "Erfassen" is disabled. Individual entries stay.

    No commit. Raises :class:`EntryLocked` for a locked Tagessumme without ``reason`` (the calendar's own
    closed-month guard asks for the reason first). Returns the number of removed Tagessummen.
    """
    now = to_utc_naive(now or now_utc()).replace(microsecond=0)
    removed = 0
    for d, old in sorted(before.items()):
        row = db.get(DayOverride, d)
        if row is None or row.absence_type != TYP_KRANK or old == TYP_KRANK:
            continue
        row.erfassung_typ = None
        ts = day_state(db, d, now=now).tagessumme
        if ts is not None and ts.tagessumme:
            entries_svc.delete_entry(db, ts.id, reason=f"{KRANK_REASON}: {reason}" if reason else KRANK_REASON,
                                     now=now, commit=False)
            removed += 1
    db.flush()
    return removed


def check_typ(value: Any) -> str:
    if value not in DAY_TYPES:
        raise EntryValidationError("Unbekannter Tagestyp – erlaubt: " + ", ".join(DAY_TYPES) + ".")
    return str(value)


def set_day_typ(
    db: Session,
    day: date,
    typ: str,
    *,
    client_action_id: str | None = None,
    reason: str | None = None,
    at: Any = None,
    sent_at: Any = None,
    now: datetime | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Set the day type of ``day`` (see module docstring); returns :func:`week_view` plus ``day``, ``action``
    (geaendert | unveraendert | veraltet) and ``removed_seconds`` (Tagessumme removed by ``krank``).

    Like :func:`set_day_total`: ``at`` = device time of the input (a delayed request older than the last type
    change is ignored, ``veraltet``), repeating is harmless, ``reason`` is required in a closed month
    (:class:`EntryLocked` / :class:`~app.services.calendar_service.CalendarLocked`, 423).
    Errors: :class:`DayTypLocked` (409, absence from the calendar), :class:`EntryValidationError` (unknown type,
    Krank on a weekend/holiday, Krank/Fortbildung outside every school year).
    """
    entries_svc._check_year(day.year)
    typ = check_typ(typ)
    now = to_utc_naive(now or now_utc()).replace(microsecond=0)
    when, delayed = input_time(at, now, sent_at)

    with _WRITE_LOCK:
        if db.in_transaction():
            db.commit()
        try:
            action, removed = _apply_typ(db, day, typ, reason=entries_svc._clean_str(reason), now=now, when=when,
                                         delayed=delayed)
        except Exception:
            db.rollback()
            raise

    result = week_view(db, monday_of(day), today=today, now=now)
    result["day"] = next(x for x in result["days"] if x["date"] == day.isoformat())
    result["action"] = action
    result["removed_seconds"] = removed
    result["client_action_id"] = client_action_id
    return result


def _calendar_day(db: Session, day: date) -> tuple[Day, Period | None]:
    """Core day (type, absence incl. the leave assignment) and its period."""
    from app.db.models import CalendarDay
    from app.services import calendar_service as cal

    period = cal.period_for_date(db, day)
    row = db.get(CalendarDay, day) if period is not None else None
    if row is not None:
        from app.core.types import AbsenceType, DayType

        return Day(date=day, day_type=DayType(row.day_type),
                   absence=AbsenceType(row.absence_type) if row.absence_type else None, ferien_id=row.ferien_id,
                   holiday_name=row.holiday_name, is_holiday=row.day_type == DayType.FEIERTAG.value), period
    return _fallback_days(db, [day])[day], period


def _apply_typ(db: Session, day: date, typ: str, *, reason: str | None, now: datetime, when: datetime,
               delayed: bool) -> tuple[str, int]:
    from app.services import calendar_service as cal

    override = db.get(DayOverride, day)
    if (delayed and override is not None and override.erfassung_typ_at is not None
            and when < override.erfassung_typ_at):
        return "veraltet", 0
    core, period = _calendar_day(db, day)
    fixed = fixed_absence(override, core.absence.value if core.absence is not None else None)
    if fixed:
        label = ABSENCE_BADGES.get(fixed, fixed)
        raise DayTypLocked(f"Der Tag ist im Kalender als {label} eingetragen – bitte dort ändern.")
    state = day_state(db, day, now=now)
    ts = state.tagessumme
    current = stored_typ(override)
    if typ == current:
        if (ts is not None and ts.tagessumme and typ != TYP_KRANK and not ts.locked
                and entries_svc._month_close(db, day) is None):
            code, klassenfahrt = tagessumme_target(override)
            if _retype(db, ts, code, klassenfahrt, reason=None, now=when):  # repair a missed sync
                db.commit()
                return "geaendert", 0
        return "unveraendert", 0
    if typ == TYP_KRANK and core.day_type.value in NO_SOLL_KINDS:
        raise EntryValidationError("An Wochenenden und Feiertagen gibt es kein Soll – „Krank“ ist dort nicht nötig.")
    current_absence = override.absence_type if override is not None else None
    if typ in ABSENCE_TYPES:
        new_absence = typ
    elif current_absence in ABSENCE_TYPES:
        new_absence = None
    else:
        new_absence = current_absence
    absence_changes = new_absence != current_absence
    if absence_changes and period is None:
        raise EntryValidationError(f"Der {cal.fmt_date_de(day)} liegt in keinem angelegten Schuljahr – Krank und "
                                   "Fortbildung lassen sich erst mit Schuljahr eintragen.")
    if not reason and (entries_svc._month_close(db, day) is not None or (ts is not None and ts.locked)):
        raise EntryLocked(entries_svc.LOCKED_MESSAGE)

    if override is None:
        override = DayOverride(date=day)
        db.add(override)
    override.erfassung_typ = typ if typ in ERFASSUNG_TYPES else None
    override.erfassung_typ_at = when
    db.flush()  # the calendar service looks the row up with Session.get
    removed = 0
    if ts is not None and typ == TYP_KRANK:
        removed = int(ts.duration_seconds or 0)
        entries_svc.delete_entry(db, ts.id, reason=f"{KRANK_REASON}: {reason}" if reason else KRANK_REASON,
                                 now=when, commit=False)
    elif ts is not None and ts.tagessumme:  # an unmarked AZ entry made by hand is not retyped (only value writes)
        code, klassenfahrt = TYP_TARGET[typ]
        _retype(db, ts, code, klassenfahrt, reason=reason, now=when)
    if absence_changes:
        # flush + closed-month guard + rematerialise + commit (one transaction with the changes above). A dormant
        # manual-leave flag (automatic leave mode) cannot stay next to an absence – the calendar would refuse it.
        cal.set_override(db, day, absence_type=new_absence, day_type=override.day_type,
                         urlaub_manuell=bool(override.urlaub_manuell) and not new_absence, note=override.note,
                         reason=reason, typ_at=when)
    else:
        db.commit()
    return "geaendert", removed


# =========================================================================== read


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_ref(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def parse_week_ref(ref: str) -> date | None:
    """Monday of ``YYYY-Www`` (``None`` if malformed or out of range)."""
    m = re.match(r"^(\d{4})-W(\d{1,2})$", ref or "")
    if not m:
        return None
    try:
        monday = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except ValueError:
        return None
    if not entries_svc.MIN_YEAR <= monday.year <= entries_svc.MAX_YEAR:
        return None
    return monday


def week_label(monday: date) -> str:
    """'KW 45 · 02.–08.11.2026' (month/year only where they change)."""
    sunday = monday + timedelta(days=6)
    kw = monday.isocalendar()[1]
    if monday.year != sunday.year:
        span = f"{monday:%d.%m.%Y}–{sunday:%d.%m.%Y}"
    elif monday.month != sunday.month:
        span = f"{monday:%d.%m.}–{sunday:%d.%m.%Y}"
    else:
        span = f"{monday:%d.}–{sunday:%d.%m.%Y}"
    return f"KW {kw} · {span}"


def _periods_of_week(db: Session, days: list[date]) -> tuple[dict[date, Period], Period | None]:
    """Each day's period (days outside every period are missing) and the week's main period: the one holding
    Thursday (ISO convention), else the one holding most days."""
    periods = list(db.scalars(select(Period).order_by(Period.start)))
    by_day: dict[date, Period] = {}
    for d in days:
        p = next((p for p in periods if p.start <= d <= p.end), None)
        if p is not None:
            by_day[d] = p
    main = by_day.get(days[3])
    if main is None and by_day:
        counts: dict[str, int] = defaultdict(int)
        for p in by_day.values():
            counts[p.id] += 1
        best = max(counts, key=lambda k: counts[k])
        main = next(p for p in by_day.values() if p.id == best)
    return by_day, main


def _fallback_days(db: Session, days: list[date]) -> dict[date, Day]:
    """Weekday/holiday/override logic for days outside every period (no ferien, no leave)."""
    from app.db.models import DayOverride
    from app.services import calendar_service as cal
    from app.services.settings_service import get_settings

    settings = get_settings(db)
    holidays = holidays_between(days[0], days[-1], settings.regionale_feiertage or ())
    overrides = {
        o.date: cal._core_override(o)
        for o in db.scalars(select(DayOverride).where(DayOverride.date.between(days[0], days[-1])))
    }
    return {d.date: d for d in build_days(days[0], days[-1], [], holidays, overrides, frozenset())}


def _badges(day: Day | None, ferien_name: str | None) -> list[dict[str, str]]:
    if day is None:
        return []
    badges: list[dict[str, str]] = []
    kind = day.day_type.value
    if kind == "feiertag" or day.is_holiday:
        badges.append({"label": "Feiertag", "kind": "feiertag", "title": day.holiday_name or "Feiertag"})
    elif kind == "ferientag" or day.ferien_id:
        badges.append({"label": "Ferien", "kind": "ferien", "title": ferien_name or "Ferien"})
    elif kind == "unterrichtsfrei":
        badges.append({"label": "unterrichtsfrei", "kind": "frei", "title": "Unterrichtsfreier Tag"})
    if day.absence is not None:
        label = ABSENCE_BADGES.get(day.absence.value, day.absence.value)
        badges.append({"label": label, "kind": day.absence.value, "title": label})
    return badges


def _typ_info(override: DayOverride | None, kind: str, absence: str | None, *, in_period: bool, locked: bool,
              krank_neutral: bool, kf_cap: int | None, total: int, counted: int | None) -> dict[str, Any]:
    """Day type of one row: chip text/style, whether and to what it can be switched, Klassenfahrt cap hint.

    ``counted``: the day's Ist as the account counts it (Klassenfahrt variant applied, ``None`` outside a period).
    The cap hint is shown only when the account really counts less than the recorded total.
    """
    typ = stored_typ(override)
    fixed = fixed_absence(override, absence) if typ not in ABSENCE_TYPES else None
    auto_leave = absence == "urlaub" and fixed is None and typ not in ABSENCE_TYPES
    default_label = AUTO_LEAVE_LABEL if auto_leave else DAY_KIND_LABELS.get(kind, "Unterricht")
    default_style = "urlaub" if auto_leave else DAY_KIND_STYLE.get(kind, "unterricht")
    if fixed:
        label, style = ABSENCE_BADGES.get(fixed, fixed), ("urlaub" if fixed == "urlaub" else "frei")
    elif typ != TYP_UNTERRICHT:
        label, style = TYP_LABELS[typ], ("sv" if typ in ERFASSUNG_TYPES else typ)
    else:
        label, style = default_label, default_style
    options = [t for t in DAY_TYPES
               if not (t == TYP_KRANK and kind in NO_SOLL_KINDS) and not (t in ABSENCE_TYPES and not in_period)]
    if typ not in options:
        options.append(typ)
    # shown greyed out with a reason instead of missing silently: Krank/Fortbildung need a school year
    disabled = [t for t in DAY_TYPES if t in ABSENCE_TYPES and t not in options
                and not (t == TYP_KRANK and kind in NO_SOLL_KINDS)]
    cap = kf_cap if typ == TYP_KLASSENFAHRT else None
    capped = bool(cap is not None and counted is not None and round_minute(counted) < round_minute(total))
    return {
        "typ": typ,
        "typ_label": label,
        "typ_style": style,
        "typ_set": typ != TYP_UNTERRICHT,
        "typ_default_label": default_label,
        "typ_default_style": default_style,
        "typ_fixed": fixed,  # calendar absence the screen does not change (link to the calendar)
        "typ_changeable": fixed is None,  # closed month: with a reason (typ_needs_reason)
        "typ_needs_reason": locked,
        "typ_options": options,
        "typ_disabled": disabled,
        "typ_hint": AUTO_LEAVE_HINT if auto_leave else None,
        "krank": typ == TYP_KRANK,
        "krank_credit": typ == TYP_KRANK and krank_neutral and kind not in NO_SOLL_KINDS,
        "krank_would_credit": krank_neutral and in_period and kind not in NO_SOLL_KINDS,
        "kf_cap": cap,
        "kf_capped": capped,
        "kf_counted": counted if capped else None,
    }


def _prompt(st: DayState, locked: bool) -> dict[str, Any] | None:
    """Entries added after the Tagessumme: offer "back to the total you entered" or "the new sum is right"."""
    if st.tagessumme is None or not st.later_seconds or locked:
        return None
    total = round_minute(st.total)
    keep = max(round_minute(st.total - st.later_seconds), round_minute(st.detail_seconds))
    if keep >= total:
        return None
    return {"later": st.later_seconds, "keep": keep, "total": total}


def week_view(db: Session, monday: date, *, today: date | None = None, now: datetime | None = None
              ) -> dict[str, Any]:
    """Everything the "Erfassen" screen and ``GET /api/weeks/{ref}`` show for one ISO week (JSON-safe)."""
    from app.services import calendar_service as cal
    from app.services import ledger as ledger_svc
    from app.services.settings_service import get_settings

    today = today or today_local()
    now = now or now_utc()
    days = [monday + timedelta(days=i) for i in range(7)]
    states = day_states(db, days[0], days[-1], now=now)

    period_by_day, main_period = _periods_of_week(db, days)
    ctxs: dict[str, Any] = {}
    error = None
    for period in {p.id: p for p in period_by_day.values()}.values():  # at most two (week across a boundary)
        try:
            ctxs[period.id] = ledger_svc.load_context(db, period, today=today)
        except cal.NoPeriodError:
            db.rollback()
        except Exception:  # never break the entry screen because of the account figures
            log.exception("Kontodaten für die Woche konnten nicht berechnet werden")
            db.rollback()
            error = "Soll und Saldo sind gerade nicht verfügbar."
    main_ctx = ctxs.get(main_period.id) if main_period is not None else None
    has_any_period = bool(period_by_day) or db.scalar(select(Period.id).limit(1)) is not None

    def ctx_of(d: date) -> Any:
        p = period_by_day.get(d)
        return ctxs.get(p.id) if p is not None else None

    core_days: dict[date, Day] = {}
    for ctx in ctxs.values():
        core_days.update({x.date: x for x in ctx.days if days[0] <= x.date <= days[-1]})
    missing = [d for d in days if d not in core_days]
    fallback = _fallback_days(db, days) if missing else {}
    closed = set(
        db.scalars(
            select(MonthClose.year_month).where(
                MonthClose.year_month.in_({f"{d.year:04d}-{d.month:02d}" for d in days})
            )
        )
    )
    settings = get_settings(db)
    krank_neutral_setting = bool(settings.krank_neutralisiert) if ctxs else False
    overrides = {o.date: o for o in db.scalars(select(DayOverride).where(DayOverride.date.between(days[0], days[-1])))}
    kf_cap = round(float(settings.klassenfahrt_cap_h or 0) * 3600) if settings.klassenfahrt_variante == "B" else None

    out_days: list[dict[str, Any]] = []
    for d in days:
        st = states[d]
        ctx = ctx_of(d)
        core = core_days.get(d) or fallback.get(d)
        row = ctx.row(d) if ctx is not None else None
        ferien = ctx.ferien_by_id(core.ferien_id) if (ctx is not None and core is not None and core.ferien_id) else None
        absence = core.absence.value if core is not None and core.absence else None
        kind = core.day_type.value if core is not None else ("wochenende" if d.weekday() >= 5 else "schultag")
        ts = st.tagessumme
        flags = set(ts.flags or []) if ts is not None else set()
        locked = f"{d.year:04d}-{d.month:02d}" in closed or bool(ts is not None and ts.locked)
        out_days.append({
            "date": d.isoformat(),
            "weekday": WEEKDAYS_SHORT[d.weekday()],
            "weekday_long": WEEKDAYS_LONG[d.weekday()],
            "label_short": f"{d:%d.%m.}",
            "aria_label": f"{WEEKDAYS_LONG[d.weekday()]}, {d.day}. {MONTHS[d.month - 1]}: Stunden",
            "is_today": d == today,
            "future": d > today,
            "day_type": kind,
            "weekend": d.weekday() >= 5 or kind == "wochenende",
            "absence": absence,
            "holiday_name": core.holiday_name if core is not None else None,
            "ferien_name": ferien.name if ferien is not None else None,
            "badges": _badges(core, ferien.name if ferien is not None else None),
            "in_period": row is not None,
            "soll": int(row.soll) if row is not None else None,
            "ist_konto": int(row.ist) if row is not None else None,
            "delta": int(row.ist - row.soll) if row is not None else None,
            "krank_gutschrift": int(row.krank_gutschrift) if row is not None else 0,
            "krank_neutral": bool(absence == "krank" and krank_neutral_setting),
            "vor_erfassung": bool(row is not None and row.vor_erfassung),
            "ist": st.total,
            "tagessumme_seconds": st.tagessumme_seconds,
            "detail_seconds": st.detail_seconds,
            "has_detail_entries": st.detail_count > 0,
            "detail_count": st.detail_count,
            "running": st.running,
            "running_seconds": st.running_seconds,
            "nachgetragen_seconds": st.later_seconds if ts is not None else 0,
            "prompt": _prompt(st, locked),
            "max_seconds": day_length(d),
            "nachtraeglich": "nachtraeglich" in flags,
            "korrigiert": "korrigiert" in flags,
            "locked": locked,
            "entries_url": f"/eintraege?von={d.isoformat()}&bis={d.isoformat()}",
            "calendar_url": f"/kalender/tag/{d.isoformat()}",
            "nb_seconds": st.nb_seconds if kf_cap is not None else 0,
            **_typ_info(overrides.get(d), kind, absence, in_period=row is not None, locked=locked,
                        krank_neutral=krank_neutral_setting, kf_cap=kf_cap, total=st.total,
                        counted=ctx.ist_by_day.get(d, 0) if row is not None else None),
        })

    rows = [x for x in out_days if x["soll"] is not None]
    # Saldo of the week up to today – today only once something is recorded for it: a full day's soll as a
    # red minus first thing in the morning would only discourage.
    for x in out_days:
        x["in_saldo"] = x["soll"] is not None and (
            x["date"] < today.isoformat() or (x["is_today"] and (x["ist"] > 0 or bool(x["ist_konto"]))))
    past = [x for x in rows if x["in_saldo"]]
    open_rows = [x for x in rows if not x["in_saldo"] and x["soll"]]
    delta_until = None
    if past and open_rows:
        delta_until = "heute" if any(x["is_today"] for x in past) else "gestern"
    week_sum = main_ctx.week_of(monday) if main_ctx is not None else None
    today_ctx = next((c for c in ctxs.values() if c.period.start <= today <= c.period.end), None)
    return {
        "ref": week_ref(monday),
        "kw": monday.isocalendar()[1],
        "monday": monday.isoformat(),
        "sunday": days[-1].isoformat(),
        "label": week_label(monday),
        "prev_ref": week_ref(monday - timedelta(days=7)),
        "next_ref": week_ref(monday + timedelta(days=7)),
        "current_ref": week_ref(today),
        "is_current": monday == monday_of(today),
        "today": today.isoformat(),
        "has_period": bool(ctxs),
        "has_any_period": has_any_period,
        "period_label": main_ctx.period.label if main_ctx is not None else None,
        "message": error,
        "ist_erfasst": sum(x["ist"] for x in out_days),
        # account Ist (incl. Krank-Gutschrift / Klassenfahrt variant); recorded time where there is no soll
        "ist": sum(x["ist"] if x["ist_konto"] is None else x["ist_konto"] for x in out_days),
        "soll": sum(x["soll"] for x in rows) if rows else None,
        "delta": sum(x["delta"] for x in past) if past else None,
        "delta_partial": delta_until is not None,
        "delta_until": delta_until,
        "krank_gutschrift": sum(x["krank_gutschrift"] for x in out_days),
        "is_schulwoche": bool(week_sum and week_sum.is_schulwoche),
        "saldo_konto": int(today_ctx.saldo_heute) if today_ctx is not None else None,
        "kf_cap": kf_cap,  # Klassenfahrt cap of variant B (seconds), None for variant A
        "days": out_days,
    }


def day_view(db: Session, d: date, *, today: date | None = None) -> dict[str, Any]:
    week = week_view(db, monday_of(d), today=today)
    return next(x for x in week["days"] if x["date"] == d.isoformat())
