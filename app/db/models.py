"""ORM models – the complete schema (Konzept §4 + docs/ARCHITECTURE.md).

Conventions:
- Timestamps are naive UTC ``DateTime`` columns (convert via ``app.util``).
- Durations are ``int`` seconds.
- String IDs are UUIDv7 (``app.util.uuid7``) in ``String(36)`` columns.
- Enum-like values are stored as plain strings. Calendar enums live in ``app.core.types``
  (DayType, AbsenceType, FerienKind, ...); DB-only enums are defined below.
- Only the scaffold step changes this file; schema changes need a new Alembic migration.
"""

from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.util import now_utc, uuid7

# --------------------------------------------------------------------------- DB-level enums


class EntryStatus(StrEnum):
    ACTIVE = "active"
    VORSCHLAG = "vorschlag"  # timetable suggestion, does not count until confirmed
    VERWORFEN = "verworfen"


class EntrySource(StrEnum):
    TIMER = "timer"
    MANUELL = "manuell"
    STUNDENPLAN = "stundenplan"
    IMPORT = "import"


class Location(StrEnum):
    SCHULE = "schule"
    ZUHAUSE = "zuhause"
    UNTERWEGS = "unterwegs"
    EXTERN = "extern"


class SlotKind(StrEnum):
    UNTERRICHT = "unterricht"
    AUFSICHT = "aufsicht"
    SPRECHSTUNDE = "sprechstunde"
    KONFERENZ_FIX = "konferenz_fix"
    FREI = "frei"


class DocumentType(StrEnum):
    GELTENDMACHUNG = "geltendmachung"
    UEBERLASTUNGSANZEIGE = "ueberlastungsanzeige"
    ANTRAG_FREIZEITAUSGLEICH = "antrag_freizeitausgleich"
    PRIORITAETSBITTE = "prioritaetsbitte"


class MonthCloseStatus(StrEnum):
    ABGESCHLOSSEN = "abgeschlossen"
    NACHTRAEGLICH_KORRIGIERT = "nachtraeglich_korrigiert"


class MehrarbeitStatus(StrEnum):
    OFFEN = "offen"
    BEANTRAGT = "beantragt"
    AUSGEGLICHEN = "ausgeglichen"


class PeriodKind(StrEnum):
    SCHULJAHR = "schuljahr"
    KALENDERJAHR = "kalenderjahr"


# Entry flags (Konzept §4). Stored as JSON list of strings in time_entry.flags.
ENTRY_FLAGS = (
    "ferienarbeit",
    "wochenende",
    "feiertag",
    "nachtraeglich",
    "korrigiert",
    "vertretung_angeordnet",
    "vertretung_genehmigt",
    "klassenfahrt",
    "nachtbereitschaft",
    "ohne_zeitgrenzen",
)


def _list() -> list:
    return []


def _dict() -> dict:
    return {}


# --------------------------------------------------------------------------- settings


class Settings(Base):
    """Singleton (id=1) with all user-editable domain settings."""

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    # Person / school (report header)
    name: Mapped[str] = mapped_column(String(200), default="")
    schule: Mapped[str] = mapped_column(String(200), default="")
    bundesland: Mapped[str] = mapped_column(String(2), default="BY")
    schulart: Mapped[str] = mapped_column(String(8), default="GS")

    # Workload
    upz_voll: Mapped[int] = mapped_column(Integer, default=28)
    upz_ist: Mapped[float] = mapped_column(Float, default=28.0)
    anrechnungsstunden: Mapped[float] = mapped_column(Float, default=0.0)
    anrechnungs_grund: Mapped[str | None] = mapped_column(Text, nullable=True)
    altersermaessigung: Mapped[float] = mapped_column(Float, default=0.0)
    altersermaessigung_manuell: Mapped[bool] = mapped_column(Boolean, default=False)
    geburtsdatum: Mapped[date | None] = mapped_column(Date, nullable=True)
    wochen_soll_voll_h: Mapped[float] = mapped_column(Float, default=40.0)
    urlaubstage: Mapped[int] = mapped_column(Integer, default=30)
    unterrichtsstunde_min: Mapped[int] = mapped_column(Integer, default=45)

    # Account period and rules
    kontoperiode: Mapped[str] = mapped_column(String(16), default=PeriodKind.SCHULJAHR.value)
    periode_start_md: Mapped[str] = mapped_column(String(5), default="08-01")
    urlaubs_modus: Mapped[str] = mapped_column(String(32), default="sommerferien_zuerst")
    grosse_pause_zaehlt: Mapped[bool] = mapped_column(Boolean, default=False)
    krank_neutralisiert: Mapped[bool] = mapped_column(Boolean, default=True)
    uebertrag_verrechnen: Mapped[bool] = mapped_column(Boolean, default=False)
    # Start of recording (migration 0004): earlier days are neutral in the account (Ist = Soll).
    erfassung_ab: Mapped[date | None] = mapped_column(Date, nullable=True)
    regionale_feiertage: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["mariae_himmelfahrt"])

    # Master data for pickers
    klassen: Mapped[list[str]] = mapped_column(JSON, default=_list)
    faecher: Mapped[list[str]] = mapped_column(JSON, default=_list)
    zeitraster: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_list)

    # Klassenfahrt / Prognose
    klassenfahrt_variante: Mapped[str] = mapped_column(String(1), default="B")
    klassenfahrt_cap_h: Mapped[float] = mapped_column(Float, default=10.0)
    prognose_schultage: Mapped[int] = mapped_column(Integer, default=20)
    ferienarbeit_schaetzung: Mapped[dict[str, float]] = mapped_column(JSON, default=_dict)

    # UI defaults
    ui_default_location_rule: Mapped[dict[str, Any]] = mapped_column(JSON, default=_dict)

    # Sensor API (no login: the token only protects /api/sensors)
    api_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)


# --------------------------------------------------------------------------- calendar


class Period(Base):
    __tablename__ = "period"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    label: Mapped[str] = mapped_column(String(32))
    start: Mapped[date] = mapped_column(Date, unique=True)
    end: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(16), default=PeriodKind.SCHULJAHR.value)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    ferien: Mapped[list[Ferien]] = relationship(
        back_populates="period", cascade="all, delete-orphan", order_by="Ferien.start"
    )


class Ferien(Base):
    __tablename__ = "ferien"
    __table_args__ = (Index("ix_ferien_start_end", "start", "end"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    period_id: Mapped[str] = mapped_column(ForeignKey("period.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(16))  # FerienKind
    start: Mapped[date] = mapped_column(Date)
    end: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(16), default="manuell")  # 'manuell' | 'ics' | 'katalog'
    quelle_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    period: Mapped[Period] = relationship(back_populates="ferien")


class Holiday(Base):
    __tablename__ = "holiday"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    scope: Mapped[str] = mapped_column(String(16))  # HolidayScope
    source: Mapped[str] = mapped_column(String(16), default="berechnet")  # 'berechnet' | 'manuell' | 'ics'


class DayOverride(Base):
    """User input per day; ``calendar_day`` is materialized from this plus ferien/holidays/leave."""

    __tablename__ = "day_override"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    absence_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # AbsenceType
    day_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # DayType, e.g. unterrichtsfrei
    urlaub_manuell: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)


class CalendarDay(Base):
    __tablename__ = "calendar_day"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    weekday: Mapped[int] = mapped_column(Integer)  # ISO 1=Mo .. 7=So
    day_type: Mapped[str] = mapped_column(String(16))  # DayType
    absence_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ferien_id: Mapped[str | None] = mapped_column(
        ForeignKey("ferien.id", ondelete="SET NULL"), nullable=True, index=True
    )
    holiday_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    soll_seconds: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    period_id: Mapped[str | None] = mapped_column(
        ForeignKey("period.id", ondelete="CASCADE"), nullable=True, index=True
    )


# --------------------------------------------------------------------------- categories


class Category(Base):
    __tablename__ = "category"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)  # stable: 'U', 'VN', ...; subcats 'U.1'
    name: Mapped[str] = mapped_column(String(100))
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("category.id", ondelete="CASCADE"), nullable=True, index=True
    )
    color: Mapped[str] = mapped_column(String(7), default="#64748B")
    sort: Mapped[int] = mapped_column(Integer, default=0)
    is_unterricht: Mapped[bool] = mapped_column(Boolean, default=False)
    counts_as_mehrarbeit_candidate: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_pseudo: Mapped[bool] = mapped_column(Boolean, default=False)  # 'KR' neutralizes soll only
    requires_note: Mapped[bool] = mapped_column(Boolean, default=False)

    parent: Mapped[Category | None] = relationship(remote_side="Category.id", back_populates="children")
    children: Mapped[list[Category]] = relationship(back_populates="parent", order_by="Category.sort")


class CategoryMapping(Base):
    """Remembered import mapping: free-text category -> catalogue code."""

    __tablename__ = "category_mapping"

    source_text: Mapped[str] = mapped_column(String(200), primary_key=True)
    category_code: Mapped[str] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)


# --------------------------------------------------------------------------- time entries


class TimeEntry(Base):
    __tablename__ = "time_entry"
    __table_args__ = (
        Index("ix_time_entry_day_status", "day", "status"),
        Index("ix_time_entry_end_at", "end_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    start_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # NULL = timer running
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    has_bounds: Mapped[bool] = mapped_column(Boolean, default=True)
    day: Mapped[date] = mapped_column(Date, index=True)  # local date of start_at

    category_id: Mapped[int] = mapped_column(ForeignKey("category.id"), index=True)
    subcategory_id: Mapped[int | None] = mapped_column(ForeignKey("category.id"), nullable=True)
    klasse: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fach: Mapped[str | None] = mapped_column(String(64), nullable=True)
    location: Mapped[str | None] = mapped_column(String(16), nullable=True)  # Location
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    flags: Mapped[list[str]] = mapped_column(JSON, default=_list)

    source: Mapped[str] = mapped_column(String(16), default=EntrySource.MANUELL.value)
    status: Mapped[str] = mapped_column(String(16), default=EntryStatus.ACTIVE.value)
    import_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("import_batch.id"), nullable=True, index=True
    )
    timetable_slot_id: Mapped[str | None] = mapped_column(
        ForeignKey("timetable_slot.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)

    category: Mapped[Category] = relationship(foreign_keys=[category_id])
    subcategory: Mapped[Category | None] = relationship(foreign_keys=[subcategory_id])
    revisions: Mapped[list[TimeEntryRevision]] = relationship(
        back_populates="entry", order_by="TimeEntryRevision.revision_no"
    )


class TimeEntryRevision(Base):
    """Append-only history; ``snapshot_json`` is the full state *before* the change."""

    __tablename__ = "time_entry_revision"
    __table_args__ = (UniqueConstraint("entry_id", "revision_no"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    entry_id: Mapped[str] = mapped_column(ForeignKey("time_entry.id"), index=True)
    revision_no: Mapped[int] = mapped_column(Integer)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON)

    entry: Mapped[TimeEntry] = relationship(back_populates="revisions")


class TimerState(Base):
    """Singleton (id=1) remembering the paused category for "Pause / Weiter"."""

    __tablename__ = "timer_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    paused_category_id: Mapped[int | None] = mapped_column(
        ForeignKey("category.id", ondelete="SET NULL"), nullable=True
    )
    paused_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)


# --------------------------------------------------------------------------- timetable


class Timetable(Base):
    __tablename__ = "timetable"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    label: Mapped[str] = mapped_column(String(100), default="")
    valid_from: Mapped[date] = mapped_column(Date, index=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    slots: Mapped[list[TimetableSlot]] = relationship(
        back_populates="timetable",
        cascade="all, delete-orphan",
        order_by=lambda: (TimetableSlot.weekday, TimetableSlot.slot_no),
    )


class TimetableSlot(Base):
    __tablename__ = "timetable_slot"
    __table_args__ = (Index("ix_timetable_slot_timetable_weekday", "timetable_id", "weekday"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    timetable_id: Mapped[str] = mapped_column(ForeignKey("timetable.id", ondelete="CASCADE"))
    weekday: Mapped[int] = mapped_column(Integer)  # 1=Mo .. 5=Fr
    slot_no: Mapped[int] = mapped_column(Integer)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    kind: Mapped[str] = mapped_column(String(16), default=SlotKind.UNTERRICHT.value)
    klasse: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fach: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id"), nullable=True)

    timetable: Mapped[Timetable] = relationship(back_populates="slots")


class DayConfirmation(Base):
    __tablename__ = "day_confirmation"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    deviations_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=_dict)  # per slot


# --------------------------------------------------------------------------- months / reports


class MehrarbeitMonth(Base):
    __tablename__ = "mehrarbeit_month"

    year_month: Mapped[str] = mapped_column(String(7), primary_key=True)  # 'YYYY-MM'
    ustd_angeordnet: Mapped[float] = mapped_column(Float, default=0.0)
    ustd_genehmigt: Mapped[float] = mapped_column(Float, default=0.0)
    ustd_vertretung_erhalten: Mapped[float] = mapped_column(Float, default=0.0)
    ustd_ausgleichsfaehig: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default=MehrarbeitStatus.OFFEN.value)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, onupdate=now_utc)


class MonthClose(Base):
    __tablename__ = "month_close"

    year_month: Mapped[str] = mapped_column(String(7), primary_key=True)
    closed_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    entries_hash_sha256: Mapped[str] = mapped_column(String(64))
    soll_seconds: Mapped[int] = mapped_column(Integer)
    ist_seconds: Mapped[int] = mapped_column(Integer)
    saldo_seconds: Mapped[int] = mapped_column(Integer)
    report_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=MonthCloseStatus.ABGESCHLOSSEN.value)
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Previous hashes after corrections: [{"hash": ..., "closed_at": ..., "replaced_at": ...}, ...]
    hash_history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_list)
    # Forced close (migration 0002): the reason and the overridden open items [{"kind","message","date"}]
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    forced_items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_list, server_default=text("'[]'"))


class ImportBatch(Base):
    __tablename__ = "import_batch"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    filename: Mapped[str] = mapped_column(String(255))
    stored_file: Mapped[str | None] = mapped_column(Text, nullable=True)  # /data/imports/<id>/original.*
    mapping_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=_dict)
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_imported: Mapped[int] = mapped_column(Integer, default=0)
    rows_skipped: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Document(Base):
    __tablename__ = "document"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    type: Mapped[str] = mapped_column(String(32), index=True)  # DocumentType
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    period_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=_dict)  # placeholder values used
    rendered_md: Mapped[str] = mapped_column(Text, default="")
    rendered_pdf: Mapped[str | None] = mapped_column(Text, nullable=True)  # file path
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class BackupLog(Base):
    __tablename__ = "backup_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    file: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    kind: Mapped[str] = mapped_column(String(16), default="auto")  # 'auto' | 'manuell'
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
