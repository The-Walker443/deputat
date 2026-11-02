"""Quick entry ("Erfassen"): one duration per day, the lowest possible hurdle for recording at all.

The home screen shows a week Mo–So with one field per day. The field shows the day's **total** recorded
work: the sum of all active, non-deleted, finished entries of the day (timer, manual, import, confirmed
timetable) except pseudo categories (Krank 'KR' only neutralises the soll). A *running* timer is not part
of the field (the row says "Timer läuft"); once stopped, its entry counts like any other entry.

Entering a value sets that total. The app keeps exactly one **Tagessumme** entry per day for it:

* duration-only (``has_bounds=False`` -> ``ohne_zeitgrenzen``), ``source='manuell'``, main category ``AZ``
  ("Arbeitszeit (pauschal, nicht aufgeschlüsselt)", :data:`app.db.seed.TAGESSUMME_CODE`), no subcategory;
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

If several entries qualify as Tagessumme (e.g. one created by hand in *Einträge*), the newest one is the
Tagessumme and the others count as detail.

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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.calendar import build_days, holidays_between
from app.core.parsing import parse_duration
from app.core.types import Day
from app.db.models import Category, EntrySource, EntryStatus, MonthClose, Period, TimeEntry
from app.db.seed import TAGESSUMME_CODE
from app.services import entries as entries_svc
from app.services.entries import EntryConflict, EntryValidationError
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

_WRITE_LOCK = threading.Lock()

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


def input_time(at: Any, now: datetime) -> tuple[datetime, bool]:
    """Device time of the input -> ``(effective time, delayed)`` (naive UTC).

    ``None`` / implausibly old -> server time. More than 5 min in the future -> :class:`EntryValidationError`.
    """
    if at is None or at == "":
        return now, False
    if isinstance(at, str):
        try:
            at = datetime.fromisoformat(at.strip())
        except ValueError:
            raise EntryValidationError("Ungültiger Zeitpunkt der Eingabe.") from None
    if not isinstance(at, datetime):
        raise EntryValidationError("Ungültiger Zeitpunkt der Eingabe.")
    at = to_utc_naive(at).replace(microsecond=0)
    if at > now + AT_FUTURE_TOLERANCE:
        raise EntryValidationError("Der Zeitpunkt der Eingabe liegt in der Zukunft – Uhr des Geräts prüfen.")
    if at < now - AT_MAX_AGE:
        return now, True
    at = min(at, now)
    return at, now - at > DELAYED_AFTER


# =========================================================================== day state


def tagessumme_category(db: Session) -> Category:
    cat = db.scalars(select(Category).where(Category.code == TAGESSUMME_CODE, Category.parent_id.is_(None))).first()
    if cat is None:  # the seed runs on every start; only a broken catalogue gets here
        raise EntryValidationError("Kategorie „AZ – Arbeitszeit (pauschal)“ fehlt – App neu starten.")
    return cat


def is_tagessumme(entry: TimeEntry, az_id: int) -> bool:
    return (
        entry.category_id == az_id
        and entry.subcategory_id is None
        and not entry.has_bounds
        and entry.source == EntrySource.MANUELL.value
        and entry.status == EntryStatus.ACTIVE.value
        and entry.deleted_at is None
    )


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
        main = found[-1] if found else None  # newest (rows are ordered by created_at)
        detail = details.get(d, []) + found[:-1]
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
    when, delayed = input_time(at, now)

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
    if ts is None:
        entries_svc.create_entry(
            db,
            {"has_bounds": False, "day": day, "duration_seconds": target, "category_id": tagessumme_category(db).id},
            source=EntrySource.MANUELL.value,
            reason=reason,
            now=when,
        )
        return "angelegt"
    if int(ts.duration_seconds or 0) == target:
        return "unveraendert"
    entries_svc.update_entry(db, ts.id, {"duration_seconds": target}, reason=reason, now=when, recorded_now=True)
    return "geaendert"


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
    krank_neutral_setting = bool(get_settings(db).krank_neutralisiert) if ctxs else False

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
        "days": out_days,
    }


def day_view(db: Session, d: date, *, today: date | None = None) -> dict[str, Any]:
    week = week_view(db, monday_of(d), today=today)
    return next(x for x in week["days"] if x["date"] == d.isoformat())
