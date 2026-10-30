"""Plain data types shared by the calculation core and the service layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class DayType(StrEnum):
    SCHULTAG = "schultag"
    FERIENTAG = "ferientag"
    UNTERRICHTSFREI = "unterrichtsfrei"
    FEIERTAG = "feiertag"
    WOCHENENDE = "wochenende"


class AbsenceType(StrEnum):
    URLAUB = "urlaub"
    KRANK = "krank"
    FORTBILDUNG = "fortbildung"
    DIENSTBEFREIUNG = "dienstbefreiung"
    SONDERURLAUB = "sonderurlaub"
    ELTERNZEIT = "elternzeit"


class FerienKind(StrEnum):
    HERBST = "herbst"
    WEIHNACHTEN = "weihnachten"
    WINTER = "winter"  # Bavarian "Frühjahrs-/Faschingsferien"
    OSTERN = "ostern"
    PFINGSTEN = "pfingsten"
    SOMMER = "sommer"
    SONSTIGE = "sonstige"


class HolidayScope(StrEnum):
    GESETZLICH = "gesetzlich"
    REGIONAL = "regional"
    UNTERRICHTSFREI = "unterrichtsfrei"  # e.g. Buß- und Bettag: no holiday, soll stays


class LeaveMode(StrEnum):
    SOMMERFERIEN_ZUERST = "sommerferien_zuerst"
    PROPORTIONAL = "proportional"
    MANUELL = "manuell"


class KlassenfahrtVariante(StrEnum):
    A = "A"  # all hours incl. night standby
    B = "B"  # capped per day, without night standby


class Ampel(StrEnum):
    GRUEN = "gruen"
    GELB = "gelb"
    ROT = "rot"


# Entry flags (Konzept §4) used by the core.
FLAG_VERTRETUNG_ANGEORDNET = "vertretung_angeordnet"
FLAG_VERTRETUNG_GENEHMIGT = "vertretung_genehmigt"
FLAG_KLASSENFAHRT = "klassenfahrt"
FLAG_NACHTBEREITSCHAFT = "nachtbereitschaft"
FLAG_NACHTRAEGLICH = "nachtraeglich"
FLAG_OHNE_ZEITGRENZEN = "ohne_zeitgrenzen"


@dataclass(frozen=True)
class FerienRange:
    """A holiday block (both bounds inclusive)."""

    id: str
    name: str
    kind: FerienKind
    start: date
    end: date

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


@dataclass(frozen=True)
class Override:
    """User input for a single day (``day_override`` row)."""

    date: date
    day_type: DayType | None = None
    absence: AbsenceType | None = None
    urlaub_manuell: bool = False
    note: str | None = None


@dataclass(frozen=True)
class Day:
    """A materialised calendar day."""

    date: date
    day_type: DayType
    absence: AbsenceType | None = None
    ferien_id: str | None = None
    holiday_name: str | None = None
    is_holiday: bool = False  # statutory or enabled regional holiday
    note: str | None = None

    @property
    def weekday(self) -> int:
        """ISO weekday, 1 = Monday ... 7 = Sunday."""
        return self.date.isoweekday()


@dataclass(frozen=True)
class SollParams:
    wochen_soll_voll_h: float = 40.0
    upz_voll: float = 28
    upz_ist: float = 28

    def __post_init__(self) -> None:
        if self.upz_voll <= 0:
            raise ValueError("upz_voll must be positive")
        if self.upz_ist < 0:
            raise ValueError("upz_ist must not be negative")
        if self.wochen_soll_voll_h < 0:
            raise ValueError("wochen_soll_voll_h must not be negative")


@dataclass(frozen=True)
class DayLedger:
    """One account row.

    ``ist`` is the value the account works with (recorded time after the
    Klassenfahrt variant plus sick-day neutralisation); ``ist_erfasst`` is the
    recorded time alone and ``krank_gutschrift`` the neutralisation credit.
    """

    date: date
    soll: int
    ist: int
    day_type: DayType
    absence: AbsenceType | None = None
    ferien_id: str | None = None
    ist_erfasst: int = 0
    krank_gutschrift: int = 0
    is_holiday: bool = False
    vor_erfassung: bool = False  # before settings.erfassung_ab: neutral (ist = soll), no sample for projections

    @property
    def delta(self) -> int:
        return self.ist - self.soll
