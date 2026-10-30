"""Idempotent seed: settings singleton, timer_state singleton, category catalogue (Konzept §5).

Existing rows are never modified, so user edits (names, colors, active flags) survive restarts.
Missing rows are added, which also lets later app versions extend the catalogue.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Category, Settings, TimerState

DEFAULT_KLASSEN = ["1a", "1b", "2a", "2b", "3a", "3b", "4a", "4b"]
DEFAULT_FAECHER = [
    "Deutsch",
    "Mathematik",
    "HSU",
    "Englisch",
    "Kunst",
    "Musik",
    "Sport",
    "Religion/Ethik",
    "Werken/Gestalten",
]

# Typical Bavarian primary school bell schedule; editable in Einstellungen.
DEFAULT_ZEITRASTER = [
    {"slot_no": 1, "start": "08:00", "end": "08:45", "label": "1. Stunde"},
    {"slot_no": 2, "start": "08:45", "end": "09:30", "label": "2. Stunde"},
    {"slot_no": 3, "start": "09:50", "end": "10:35", "label": "3. Stunde"},
    {"slot_no": 4, "start": "10:35", "end": "11:20", "label": "4. Stunde"},
    {"slot_no": 5, "start": "11:35", "end": "12:20", "label": "5. Stunde"},
    {"slot_no": 6, "start": "12:20", "end": "13:05", "label": "6. Stunde"},
]

# Konzept §8.1: Mo–Fr 07:00–14:00 -> Schule, otherwise Zuhause.
DEFAULT_LOCATION_RULE = {
    "weekdays": [1, 2, 3, 4, 5],
    "from": "07:00",
    "to": "14:00",
    "inside": "schule",
    "outside": "zuhause",
}


@dataclass(frozen=True)
class CategorySeed:
    code: str
    name: str
    color: str
    subcategories: tuple[str, ...] = ()
    is_unterricht: bool = False
    counts_as_mehrarbeit_candidate: bool = False
    requires_note: bool = False
    is_pseudo: bool = False


# Colors: distinct hues, all >= 4.5:1 contrast against white (usable as text and dots).
CATEGORY_CATALOGUE: tuple[CategorySeed, ...] = (
    CategorySeed("U", "Unterricht", "#2563EB",
                 ("Regelunterricht", "Förderunterricht", "Differenzierung", "Vertretung (gehalten)"),
                 is_unterricht=True, counts_as_mehrarbeit_candidate=True),
    CategorySeed("VN", "Vor-/Nachbereitung", "#0E7490",
                 ("Stundenplanung", "Material erstellen", "Kopieren", "Digitale Tafel vorbereiten",
                  "Reflexion")),
    CategorySeed("K", "Korrektur & Leistungsbewertung", "#DC2626",
                 ("Proben", "Hefte", "Lernzielkontrollen", "Zeugnisse/LEG-Bögen", "Noten eintragen")),
    CategorySeed("E", "Elternarbeit", "#C2410C",
                 ("Elternabend", "Sprechtag", "Einzelgespräch", "Elternbriefe", "Telefonate",
                  "Elternbeirat")),
    CategorySeed("KO", "Konferenzen & Zusammenarbeit", "#7C3AED",
                 ("Lehrerkonferenz", "Jahrgangsteam", "Dienstbesprechung", "Schulentwicklung",
                  "Absprachen")),
    CategorySeed("AV", "Aufsichten & Präsenz", "#A16207",
                 ("Pausenaufsicht", "Busaufsicht", "Frühaufsicht", "Randstunden-Betreuung")),
    CategorySeed("KV", "Klassenleitung & Verwaltung", "#15803D",
                 ("Klassenbuch/Schülerakten", "Listen", "Statistik", "Schülerbögen",
                  "Organisation Klassenkasse")),
    CategorySeed("B", "Beratung, Förderung, Inklusion", "#DB2777",
                 ("Förderpläne", "MSD/Schulpsychologie", "Übertrittsberatung", "Gespräche mit Kind")),
    CategorySeed("F", "Fortbildung", "#92400E",
                 ("Schilf", "Externe Fortbildung", "Selbststudium (dienstlich veranlasst)")),
    CategorySeed("SV", "Schulveranstaltungen & Fahrten", "#0F766E",
                 ("Schulfest", "Wandertag", "Projektwoche", "Klassenfahrt", "Gottesdienst",
                  "Einschulung")),
    CategorySeed("FS", "Funktionen & Sonderaufgaben", "#4338CA",
                 ("Fachbetreuung", "Medien-/Sicherheitsbeauftragte", "Praktikumsbetreuung", "Mentorat")),
    CategorySeed("DW", "Dienstwege", "#4D7C0F",
                 ("Wege zwischen Dienstorten", "Fahrt zur Fortbildung")),
    CategorySeed("S", "Sonstiges", "#475569", requires_note=True),
    # Pseudo category for partial sick days: neutralizes soll only, excluded from statistics.
    CategorySeed("KR", "Krank (Teil)", "#94A3B8", is_pseudo=True),
)

MAIN_CATEGORY_CODES = tuple(c.code for c in CATEGORY_CATALOGUE if not c.is_pseudo)


def _seed_settings(db: Session) -> None:
    if db.get(Settings, 1) is not None:
        return
    db.add(
        Settings(
            id=1,
            name="",
            schule="",
            klassen=list(DEFAULT_KLASSEN),
            faecher=list(DEFAULT_FAECHER),
            zeitraster=[dict(slot) for slot in DEFAULT_ZEITRASTER],
            ui_default_location_rule=dict(DEFAULT_LOCATION_RULE),
            regionale_feiertage=["mariae_himmelfahrt"],
            ferienarbeit_schaetzung={},
        )
    )


def _seed_timer_state(db: Session) -> None:
    if db.get(TimerState, 1) is None:
        db.add(TimerState(id=1))


def _seed_categories(db: Session) -> None:
    existing = {c.code: c for c in db.scalars(select(Category))}
    for main_sort, spec in enumerate(CATEGORY_CATALOGUE, start=1):
        parent = existing.get(spec.code)
        if parent is None:
            parent = Category(
                code=spec.code,
                name=spec.name,
                color=spec.color,
                sort=main_sort * 10,
                is_unterricht=spec.is_unterricht,
                counts_as_mehrarbeit_candidate=spec.counts_as_mehrarbeit_candidate,
                requires_note=spec.requires_note,
                is_pseudo=spec.is_pseudo,
                active=True,
            )
            db.add(parent)
            db.flush()
            existing[spec.code] = parent

        child_names = {c.name for c in existing.values() if c.parent_id == parent.id}
        for sub_sort, sub_name in enumerate(spec.subcategories, start=1):
            code = f"{spec.code}.{sub_sort}"
            if sub_name in child_names or code in existing:
                continue
            child = Category(
                code=code,
                name=sub_name,
                parent_id=parent.id,
                color=spec.color,
                sort=sub_sort * 10,
                is_unterricht=spec.is_unterricht,
                counts_as_mehrarbeit_candidate=spec.counts_as_mehrarbeit_candidate,
                active=True,
            )
            db.add(child)
            existing[code] = child


def seed(db: Session) -> None:
    """Insert missing singletons and catalogue rows; commits."""
    _seed_settings(db)
    _seed_timer_state(db)
    _seed_categories(db)
    db.commit()
