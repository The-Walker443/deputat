"""User-editable domain settings (singleton ``settings`` row, id=1).

Public API
----------
- :func:`get_settings` – the settings row (created by the seed if missing); refreshes the
  computed Altersermäßigung when it is not overridden.
- :func:`update_settings` – validated partial update; rematerialises all periods when a value
  that influences the calendar or the target time changed.
- :func:`compute_altersermaessigung` – Konzept §1.1 incl. Stichtag logic.
- :func:`soll_params` – the :class:`app.core.types.SollParams` for the calculation core.
- :func:`generate_api_token` / :func:`clear_api_token` / :func:`api_token_configured` – the bearer
  token for ``/api/sensors`` (only the SHA-256 hash is stored).

Validation errors raise :class:`SettingsValidationError` (a ``ValueError``) with a German message
and the offending field name.

Teilzeitfaktor: ``settings.upz_ist`` is the *actual* teaching load with Altersermäßigung and
Anrechnungsstunden already deducted (Konzept §4). Those reductions must not lower the target time
(Konzept §6.1), so the factor uses ``upz_ist + altersermaessigung + anrechnungsstunden`` (capped at
``upz_voll``) as the teaching load of the employment contract.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.core.calendar import REGIONAL_FRIEDENSFEST, REGIONAL_MARIAE_HIMMELFAHRT
from app.core.types import KlassenfahrtVariante, LeaveMode, SollParams
from app.db.models import Location, PeriodKind, Settings
from app.util import today_local

REGIONAL_OPTIONS: dict[str, str] = {
    REGIONAL_MARIAE_HIMMELFAHRT: "Mariä Himmelfahrt (15.08.)",
    REGIONAL_FRIEDENSFEST: "Augsburger Hohes Friedensfest (08.08.)",
}

# Konzept §1.1: −1 UPZ from the completed 58th, −2 from the 60th, −3 from the 62nd year of age.
ALTERSERMAESSIGUNG_STUFEN: tuple[tuple[int, int], ...] = ((62, 3), (60, 2), (58, 1))

# Fields whose change requires rebuilding calendar_day rows (day types, leave, soll).
CALENDAR_FIELDS = frozenset(
    {
        "upz_voll",
        "upz_ist",
        "anrechnungsstunden",
        "altersermaessigung",
        "altersermaessigung_manuell",
        "geburtsdatum",
        "wochen_soll_voll_h",
        "urlaubstage",
        "urlaubs_modus",
        "regionale_feiertage",
    }
)

# Counting rules that change the Ist of past days (evaluated in memory by the ledger).
RULE_FIELDS = frozenset({"krank_neutralisiert", "klassenfahrt_variante", "klassenfahrt_cap_h", "erfassung_ab"})

# A change of these fields may alter the figures of closed months: it needs a reason then (Konzept L4, §4).
GUARDED_FIELDS = CALENDAR_FIELDS | RULE_FIELDS

LOCKED_SETTINGS_MESSAGE = (
    "Die Änderung verändert Soll oder Ist der abgeschlossenen Monate {months} – speichern nur mit Begründung "
    "(die Monate werden dann als „nachträglich korrigiert“ markiert)."
)

# Fields that may be changed through update_settings (auth fields are handled separately).
EDITABLE_FIELDS = frozenset(
    {
        "name",
        "schule",
        "bundesland",
        "schulart",
        "upz_voll",
        "upz_ist",
        "anrechnungsstunden",
        "anrechnungs_grund",
        "altersermaessigung",
        "altersermaessigung_manuell",
        "geburtsdatum",
        "wochen_soll_voll_h",
        "urlaubstage",
        "unterrichtsstunde_min",
        "kontoperiode",
        "periode_start_md",
        "urlaubs_modus",
        "grosse_pause_zaehlt",
        "krank_neutralisiert",
        "uebertrag_verrechnen",
        "erfassung_ab",
        "regionale_feiertage",
        "klassen",
        "faecher",
        "zeitraster",
        "klassenfahrt_variante",
        "klassenfahrt_cap_h",
        "prognose_schultage",
        "ferienarbeit_schaetzung",
        "ui_default_location_rule",
    }
)

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_MD = re.compile(r"^(\d{2})-(\d{2})$")


class SettingsValidationError(ValueError):
    """Invalid settings value; ``str(exc)`` is a German message for the UI."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


# --------------------------------------------------------------------------- read


def _row(db: Session) -> Settings:
    settings = db.get(Settings, 1)
    if settings is None:
        from app.db.seed import seed

        seed(db)
        settings = db.get(Settings, 1)
    assert settings is not None
    return settings


def get_settings(db: Session, *, today: date | None = None) -> Settings:
    """The settings singleton. Refreshes the computed Altersermäßigung (commits if it changed).

    ``today`` is the REAL current date (default: today) and only selects the school year of the
    automatic Altersermäßigung. Never pass an evaluation date / Stichtag here: ``upz_ist`` is a single
    current value, so the Altersermäßigung must belong to the same (current) school year, otherwise the
    Teilzeitfaktor - and with it Soll/J_soll - would depend on the evaluation date (Konzept §6.1).
    """
    settings = _row(db)
    if not settings.altersermaessigung_manuell:
        computed = compute_altersermaessigung(settings.geburtsdatum, school_year_start_year(today))
        if float(settings.altersermaessigung or 0) != float(computed):
            settings.altersermaessigung = float(computed)
            db.commit()
    return settings


def school_year_start_year(today: date | None = None) -> int:
    """Calendar year in which the school year containing ``today`` started (01.08.)."""
    today = today or today_local()
    return today.year if today.month >= 8 else today.year - 1


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 February
        return d.replace(year=d.year + years, day=28)


def compute_altersermaessigung(geburtsdatum: date | None, school_year_start: int) -> int:
    """Altersermäßigung (UPZ) for the school year starting 01.08.``school_year_start``.

    Stichtag logic (Konzept §1.1): a birthday between 01.08. and 31.01. counts for the school year
    it falls into, a birthday between 01.02. and 31.07. only from the following school year. So a
    level applies when the birthday completing the age lies on or before 31.01. of the school
    year's second calendar year.
    """
    if geburtsdatum is None:
        return 0
    cutoff = date(school_year_start + 1, 1, 31)
    for age, reduction in ALTERSERMAESSIGUNG_STUFEN:
        if _add_years(geburtsdatum, age) <= cutoff:
            return reduction
    return 0


def contract_upz(settings: Settings) -> float:
    """UPZ of the employment contract: actual UPZ plus reductions, capped at the full-time UPZ."""
    total = (
        float(settings.upz_ist or 0)
        + float(settings.altersermaessigung or 0)
        + float(settings.anrechnungsstunden or 0)
    )
    return min(float(settings.upz_voll), total)


def soll_params(settings: Settings) -> SollParams:
    """SollParams for the core (see module docstring for the Teilzeitfaktor)."""
    return SollParams(
        wochen_soll_voll_h=float(settings.wochen_soll_voll_h),
        upz_voll=settings.upz_voll,
        upz_ist=contract_upz(settings),
    )


def teilzeitfaktor(settings: Settings) -> float:
    return contract_upz(settings) / float(settings.upz_voll) if settings.upz_voll else 0.0


def _de_num(value: float) -> str:
    return (f"{value:g}").replace(".", ",")


def settings_hints(settings: Settings) -> list[str]:
    """Plausibility hints for the settings screen (not errors)."""
    hints: list[str] = []
    total = (
        float(settings.upz_ist or 0)
        + float(settings.altersermaessigung or 0)
        + float(settings.anrechnungsstunden or 0)
    )
    if total > float(settings.upz_voll) + 1e-9:
        reductions = float(settings.altersermaessigung or 0) + float(settings.anrechnungsstunden or 0)
        expected = max(0.0, float(settings.upz_voll) - reductions)
        hints.append(
            "UPZ tatsächlich plus Altersermäßigung und Anrechnungsstunden übersteigt die Vollzeit-UPZ. "
            "Hast du die Ermäßigungen schon abgezogen? Bei Vollzeit wäre UPZ tatsächlich = "
            f"{_de_num(expected)} ({settings.upz_voll} − {_de_num(reductions)}). "
            "Bis zur Korrektur rechnet das Soll mit Vollzeit, und Berichte zeigen die Angaben so wie eingetragen."
        )
    if settings.bundesland != "BY":
        hints.append("Feiertage und Ferienlogik sind nur für Bayern umgesetzt.")
    return hints


def settings_dict(settings: Settings) -> dict[str, Any]:
    """All editable fields plus derived values (never the auth hashes)."""
    data = {field: getattr(settings, field) for field in sorted(EDITABLE_FIELDS)}
    data["teilzeitfaktor"] = round(teilzeitfaktor(settings), 6)
    data["altersermaessigung_berechnet"] = compute_altersermaessigung(
        settings.geburtsdatum, school_year_start_year()
    )
    data["api_token_configured"] = bool(settings.api_token_hash)
    data["hinweise"] = settings_hints(settings)
    data["updated_at"] = settings.updated_at
    return data


# --------------------------------------------------------------------------- validation


def _num(field: str, value: Any, lo: float, hi: float, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SettingsValidationError(field, f"{field}: Zahl erwartet.")
    if integer and int(value) != value:
        raise SettingsValidationError(field, f"{field}: ganze Zahl erwartet.")
    if not lo <= value <= hi:
        raise SettingsValidationError(field, f"{field}: Wert muss zwischen {lo:g} und {hi:g} liegen.")
    return int(value) if integer else float(value)


def _half_steps(field: str, value: float) -> float:
    if round(value * 2) != value * 2:
        raise SettingsValidationError(field, f"{field}: nur ganze oder halbe Stunden erlaubt.")
    return value


def _bool(field: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise SettingsValidationError(field, f"{field}: true oder false erwartet.")
    return value


def _text(field: str, value: Any, max_len: int, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise SettingsValidationError(field, f"{field}: Text erwartet.")
    value = value.strip()
    if len(value) > max_len:
        raise SettingsValidationError(field, f"{field}: höchstens {max_len} Zeichen.")
    return value


def _str_list(field: str, value: Any, max_len: int = 64) -> list[str]:
    if not isinstance(value, list | tuple) or not all(isinstance(v, str) for v in value):
        raise SettingsValidationError(field, f"{field}: Liste von Texten erwartet.")
    result: list[str] = []
    for item in value:
        item = item.strip()
        if not item:
            continue
        if len(item) > max_len:
            raise SettingsValidationError(field, f"{field}: Eintrag „{item[:20]}…“ ist zu lang.")
        if item not in result:
            result.append(item)
    return result


def _date(field: str, value: Any) -> date | None:
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise SettingsValidationError(field, f"{field}: Datum im Format JJJJ-MM-TT erwartet.")


def _zeitraster(value: Any) -> list[dict[str, Any]]:
    field = "zeitraster"
    if not isinstance(value, list):
        raise SettingsValidationError(field, "Zeitraster: Liste von Stunden erwartet.")
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    for slot in value:
        if not isinstance(slot, dict):
            raise SettingsValidationError(field, "Zeitraster: jede Stunde braucht slot_no, start und end.")
        try:
            slot_no = int(slot["slot_no"])
            start, end = str(slot["start"]), str(slot["end"])
        except (KeyError, TypeError, ValueError):
            raise SettingsValidationError(field, "Zeitraster: jede Stunde braucht slot_no, start und end.") from None
        if not (_HHMM.match(start) and _HHMM.match(end)):
            raise SettingsValidationError(field, f"Zeitraster: Uhrzeit im Format HH:MM erwartet (Stunde {slot_no}).")
        if end <= start:
            raise SettingsValidationError(field, f"Zeitraster: Ende muss nach dem Beginn liegen (Stunde {slot_no}).")
        if slot_no in seen:
            raise SettingsValidationError(field, f"Zeitraster: Stunde {slot_no} ist doppelt.")
        seen.add(slot_no)
        label = slot.get("label") or f"{slot_no}. Stunde"
        result.append({"slot_no": slot_no, "start": start, "end": end, "label": str(label)[:50]})
    return sorted(result, key=lambda s: s["slot_no"])


def _schaetzung(value: Any) -> dict[str, float]:
    field = "ferienarbeit_schaetzung"
    if not isinstance(value, dict):
        raise SettingsValidationError(field, "Ferienarbeit-Schätzung: Zuordnung Ferien → Stunden erwartet.")
    result: dict[str, float] = {}
    for key, hours in value.items():
        if hours is None:
            continue
        result[str(key)] = float(_num(field, hours, 0, 1000))
    return result


def _validate(settings: Settings, fields: dict[str, Any]) -> dict[str, Any]:
    """Return normalised values; cross-field checks use the merged state."""
    clean: dict[str, Any] = {}
    for field, value in fields.items():
        if field not in EDITABLE_FIELDS:
            raise SettingsValidationError(field, f"Unbekannte oder nicht änderbare Einstellung: {field}.")
        match field:
            case "name" | "schule":
                clean[field] = _text(field, value, 200)
            case "anrechnungs_grund":
                clean[field] = _text(field, value, 500, nullable=True) or None
            case "bundesland":
                value = _text(field, value, 2)
                if value != "BY":
                    raise SettingsValidationError(field, "Derzeit wird nur Bayern (BY) unterstützt.")
                clean[field] = value
            case "schulart":
                clean[field] = _text(field, value, 8) or "GS"
            case "upz_voll":
                clean[field] = _num(field, value, 1, 40, integer=True)
            case "upz_ist" | "anrechnungsstunden":
                clean[field] = _half_steps(field, _num(field, value, 0, 40))
            case "altersermaessigung":
                clean[field] = _half_steps(field, _num(field, value, 0, 10))
            case "altersermaessigung_manuell" | "grosse_pause_zaehlt" | "krank_neutralisiert" | "uebertrag_verrechnen":
                clean[field] = _bool(field, value)
            case "geburtsdatum":
                d = _date(field, value)
                if d is not None and not date(1900, 1, 1) <= d <= today_local():
                    raise SettingsValidationError(field, "Geburtsdatum ist nicht plausibel.")
                clean[field] = d
            case "erfassung_ab":
                d = _date(field, value)
                if d is not None and not date(2000, 1, 1) <= d <= date(2100, 12, 31):
                    raise SettingsValidationError(field, "„Erfassung ab“ ist nicht plausibel.")
                clean[field] = d
            case "wochen_soll_voll_h":
                clean[field] = _num(field, value, 1, 60)
            case "urlaubstage":
                clean[field] = _num(field, value, 0, 60, integer=True)
            case "unterrichtsstunde_min":
                clean[field] = _num(field, value, 1, 120, integer=True)
            case "kontoperiode":
                if value not in {k.value for k in PeriodKind}:
                    raise SettingsValidationError(field, "Kontoperiode muss „schuljahr“ oder „kalenderjahr“ sein.")
                clean[field] = value
            case "periode_start_md":
                m = _MD.match(value) if isinstance(value, str) else None
                try:
                    if m is None:
                        raise ValueError
                    date(2001, int(m.group(1)), int(m.group(2)))
                except ValueError:
                    raise SettingsValidationError(field, "Periodenbeginn im Format MM-TT erwartet (z. B. 08-01).") from None
                clean[field] = value
            case "urlaubs_modus":
                if value not in {m.value for m in LeaveMode}:
                    raise SettingsValidationError(
                        field, "Urlaubsmodus muss „sommerferien_zuerst“, „proportional“ oder „manuell“ sein."
                    )
                clean[field] = value
            case "regionale_feiertage":
                items = _str_list(field, value)
                unknown = [i for i in items if i not in REGIONAL_OPTIONS]
                if unknown:
                    raise SettingsValidationError(
                        field,
                        f"Unbekannter regionaler Feiertag: {', '.join(unknown)} "
                        f"(erlaubt: {', '.join(REGIONAL_OPTIONS)}).",
                    )
                clean[field] = items
            case "klassen":
                clean[field] = _str_list(field, value, 16)
            case "faecher":
                clean[field] = _str_list(field, value, 64)
            case "zeitraster":
                clean[field] = _zeitraster(value)
            case "klassenfahrt_variante":
                if value not in {v.value for v in KlassenfahrtVariante}:
                    raise SettingsValidationError(field, "Klassenfahrt-Variante muss „A“ oder „B“ sein.")
                clean[field] = value
            case "klassenfahrt_cap_h":
                clean[field] = _num(field, value, 1, 24)
            case "prognose_schultage":
                clean[field] = _num(field, value, 1, 200, integer=True)
            case "ferienarbeit_schaetzung":
                clean[field] = _schaetzung(value)
            case "ui_default_location_rule":
                if not isinstance(value, dict):
                    raise SettingsValidationError(field, "Ortsregel: Objekt erwartet.")
                for key in ("inside", "outside"):
                    if key in value and value[key] not in {loc.value for loc in Location}:
                        raise SettingsValidationError(field, f"Ortsregel: unbekannter Ort „{value[key]}“.")
                for key in ("from", "to"):
                    if key in value and not (isinstance(value[key], str) and _HHMM.match(value[key])):
                        raise SettingsValidationError(field, "Ortsregel: Uhrzeit im Format HH:MM erwartet.")
                clean[field] = dict(value)

    upz_voll = clean.get("upz_voll", settings.upz_voll)
    upz_ist = clean.get("upz_ist", settings.upz_ist)
    if upz_ist > upz_voll:
        raise SettingsValidationError("upz_ist", "UPZ-Ist darf die Vollzeit-UPZ nicht übersteigen.")
    if clean.get("anrechnungsstunden", settings.anrechnungsstunden or 0) > upz_voll:
        raise SettingsValidationError("anrechnungsstunden", "Anrechnungsstunden übersteigen die Vollzeit-UPZ.")
    return clean


# --------------------------------------------------------------------------- write


def closed_month_figures(db: Session) -> dict[str, tuple]:
    """Per closed month: (calendar signature, Soll, Ist) as the current settings compute them (no writes)."""
    from sqlalchemy import select

    from app.db.models import MonthClose, Period
    from app.services import calendar_service as cal
    from app.services import ledger

    closed = set(db.scalars(select(MonthClose.year_month)))
    if not closed:
        return {}
    calendar_sig = cal.closed_month_signature(db)
    figures: dict[str, tuple[int, int]] = {}
    lo, hi = min(closed), max(closed)
    for period in db.scalars(select(Period).order_by(Period.start)):
        if f"{period.end:%Y-%m}" < lo or f"{period.start:%Y-%m}" > hi:
            continue
        ctx = ledger.load_context(db, period)
        for m in ctx.months:
            ym = f"{m.year:04d}-{m.month:02d}"
            if ym in closed:
                figures[ym] = (m.soll, m.ist)
    return {ym: (calendar_sig.get(ym), figures.get(ym)) for ym in closed}


def update_settings(db: Session, *, today: date | None = None, reason: str | None = None,
                    **fields: Any) -> Settings:
    """Validate and apply ``fields``; rematerialises all periods when calendar-relevant values changed.

    Altersermäßigung: passing ``altersermaessigung`` marks it as manual override (unless
    ``altersermaessigung_manuell=False`` is passed as well); ``altersermaessigung_manuell=False``
    restores the value computed from ``geburtsdatum``.

    Closed months (Konzept L4/§4): if a change of :data:`GUARDED_FIELDS` alters the Soll, the Ist or the
    calendar of a closed month, ``reason`` is required (otherwise ``CalendarLocked``, HTTP 423, nothing is
    saved); with a reason the affected months are marked ``nachtraeglich_korrigiert``.
    """
    settings = _row(db)
    clean = _validate(settings, fields)

    if "altersermaessigung" in clean and "altersermaessigung_manuell" not in clean:
        clean["altersermaessigung_manuell"] = True
    manual = clean.get("altersermaessigung_manuell", settings.altersermaessigung_manuell)
    if not manual:
        geburtsdatum = clean.get("geburtsdatum", settings.geburtsdatum)
        clean["altersermaessigung"] = float(
            compute_altersermaessigung(geburtsdatum, school_year_start_year(today))
        )

    changed = {f for f, v in clean.items() if getattr(settings, f) != v}
    before = closed_month_figures(db) if changed & GUARDED_FIELDS else {}
    for field in changed:
        setattr(settings, field, clean[field])
    if before:
        db.flush()
        after = closed_month_figures(db)
        affected = sorted(ym for ym, sig in before.items() if after.get(ym) != sig)
        if affected:
            from app.services import calendar_service as cal

            reason = (reason or "").strip() or None
            if reason is None:
                db.rollback()
                raise cal.CalendarLocked(LOCKED_SETTINGS_MESSAGE.format(months=", ".join(affected)))
            cal.mark_closed_months_corrected(db, affected, f"Einstellungen geändert: {reason}", kind="einstellungen")
    db.commit()

    if changed & CALENDAR_FIELDS:
        from app.services.calendar_service import rematerialize_all

        rematerialize_all(db)
    return settings


def set_ferienarbeit_schaetzung(db: Session, planned: dict[str, float | None]) -> dict[str, float]:
    """Merge per-ferien estimates (hours; ``None`` removes the estimate) into the settings."""
    settings = _row(db)
    merged = dict(settings.ferienarbeit_schaetzung or {})
    for key, hours in planned.items():
        if hours is None:
            merged.pop(str(key), None)
        else:
            merged[str(key)] = float(_num("ferienarbeit_schaetzung", hours, 0, 1000))
    settings.ferienarbeit_schaetzung = merged  # new object so SQLAlchemy detects the change
    db.commit()
    return merged


# --------------------------------------------------------------------------- API token


def _hash_token(token: str) -> str:
    try:
        from app.security import hash_api_token
    except ImportError:  # pragma: no cover - security module being rewritten
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hash_api_token(token)


def _new_token() -> tuple[str, str]:
    try:
        from app.security import generate_api_token as _generate
    except ImportError:  # pragma: no cover - security module being rewritten
        token = secrets.token_urlsafe(32)
        return token, hashlib.sha256(token.encode("utf-8")).hexdigest()
    token, token_hash = _generate()
    return token, token_hash


def generate_api_token(db: Session) -> str:
    """Create (or rotate) the sensor API token; returns the plaintext once, stores only the hash."""
    settings = _row(db)
    token, token_hash = _new_token()
    settings.api_token_hash = token_hash
    db.commit()
    return token


def clear_api_token(db: Session) -> None:
    settings = _row(db)
    settings.api_token_hash = None
    db.commit()


def api_token_configured(db: Session) -> bool:
    return bool(_row(db).api_token_hash)


def verify_api_token(db: Session, token: str) -> bool:
    """Constant-time comparison of ``token`` against the stored hash."""
    stored = _row(db).api_token_hash
    if not stored or not token:
        return False
    return secrets.compare_digest(stored, _hash_token(token))
