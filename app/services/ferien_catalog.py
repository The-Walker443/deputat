"""Bundled catalogue of the official Bavarian school holidays (``config/ferien_bayern.yaml``).

The app takes these dates over automatically when a period is created (see
:func:`app.services.calendar_service.apply_ferien_catalog`), so nobody has to import an ICS file. ICS import and
manual entry stay available as fallback, e.g. for school years the catalogue does not cover yet.

File lookup: environment variable ``FERIEN_CATALOG_FILE`` (absolute, or relative to the project root), else the
shipped ``config/ferien_bayern.yaml``. An updated file can therefore live in the data volume
(``FERIEN_CATALOG_FILE=/data/ferien_bayern.yaml``). Results are cached per (path, mtime) – failures too, so a
missing or broken file is read, validated and logged once per change, not on every request.

File format (see the shipped file)::

    bundesland: BY
    source_url: "https://..."          # Pflichtfeld (http/https)
    source_title: "..."                # Pflichtfeld
    source_short: "Bayerisches Kultusministerium"
    retrieved: 2026-10-30              # Pflichtfeld (Stand)
    note: "..."
    partial_school_years: ["2030/31"]  # school years with only some blocks fixed so far
    blocks:
      - {school_year: "2026/27", name: "Herbstferien 2026", kind: herbst, start: 2026-11-02, end: 2026-11-06}

``kind`` is a :class:`~app.core.types.FerienKind` value; without it the kind follows the name
(:func:`app.core.calendar.classify_ferien`). ``start``/``end`` are the first and last holiday (inclusive).

Coverage: a school year ``YYYY/YY`` (01.08.–31.07.) counts as covered when it is listed in ``blocks`` and not in
``partial_school_years``; every complete school year must contain Herbst-, Weihnachts-, Winter/Frühjahrs-,
Oster-, Pfingst- and Sommerferien. A period is covered when every school year it touches is covered.
:func:`blocks_for_period` clips blocks to the period: the summer holidays straddle the 01.08. boundary (e.g.
Sommerferien 2028 start on 31.07.2028, which belongs to the period 2027/28).

Only Bavaria is supported (``settings.bundesland == 'BY'``); for other values the public helpers return nothing.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from app.config import PROJECT_ROOT
from app.core.calendar import classify_ferien
from app.core.types import FerienKind

log = logging.getLogger("app.services.ferien_catalog")

ENV_VAR = "FERIEN_CATALOG_FILE"
DEFAULT_FILE = PROJECT_ROOT / "config" / "ferien_bayern.yaml"
SUPPORTED_BUNDESLAND = "BY"

# Kinds every complete school year must contain (SONSTIGE is not part of the official Ferienordnung).
REQUIRED_KINDS = frozenset(
    {FerienKind.HERBST, FerienKind.WEIHNACHTEN, FerienKind.WINTER, FerienKind.OSTERN, FerienKind.PFINGSTEN,
     FerienKind.SOMMER}
)
MAX_BLOCK_DAYS = 120  # same limit as a ferien row (calendar_service.MAX_FERIEN_DAYS)
MAX_BLOCKS = 500

_SCHOOL_YEAR_RE = re.compile(r"^(\d{4})/(\d{2})$")

# (path, mtime or None when missing) -> the catalogue or the error message of the failed load (one entry).
_cache: dict[tuple[str, float | None], Catalog | str] = {}
_mismatch_logged: set[tuple[str, str]] = set()  # (path, bundesland) of catalogues already logged as mismatch


class FerienCatalogError(ValueError):
    """The catalogue file is missing, unreadable or invalid (German message)."""


@dataclass(frozen=True)
class CatalogBlock:
    school_year: str
    name: str
    kind: FerienKind
    start: date
    end: date
    official: str = ""
    # Set by blocks_for_period when the block was cut at the period boundary: the official dates.
    full_start: date | None = None
    full_end: date | None = None

    @property
    def clipped(self) -> bool:
        return self.full_start is not None and (self.full_start, self.full_end) != (self.start, self.end)

    def as_dict(self) -> dict[str, Any]:
        return {
            "school_year": self.school_year,
            "name": self.name,
            "kind": self.kind.value,
            "start": self.start,
            "end": self.end,
            "official": self.official,
            "clipped": self.clipped,
            "full_start": self.full_start or self.start,
            "full_end": self.full_end or self.end,
        }


@dataclass(frozen=True)
class Coverage:
    first: str | None  # first complete school year, e.g. "2024/25"
    last: str | None
    school_years: tuple[str, ...]  # complete school years
    partial: tuple[str, ...]

    @property
    def text(self) -> str:
        if not self.first:
            return "keine Schuljahre"
        return self.first if self.first == self.last else f"{self.first} bis {self.last}"


@dataclass(frozen=True)
class Catalog:
    path: str
    bundesland: str
    source_url: str
    source_title: str
    source_short: str
    source_page: str | None
    retrieved: date
    note: str
    blocks: tuple[CatalogBlock, ...]
    partial_school_years: frozenset[str] = field(default_factory=frozenset)

    @property
    def complete_school_years(self) -> tuple[str, ...]:
        years = {b.school_year for b in self.blocks} - self.partial_school_years
        return tuple(sorted(years))

    def coverage(self) -> Coverage:
        years = self.complete_school_years
        return Coverage(
            first=years[0] if years else None,
            last=years[-1] if years else None,
            school_years=years,
            partial=tuple(sorted(self.partial_school_years)),
        )

    def covers(self, start: date, end: date) -> bool:
        """True when every school year touched by ``[start, end]`` is complete in the catalogue."""
        if end < start:
            return False
        complete = {int(sy[:4]) for sy in self.complete_school_years}
        return all(y in complete for y in range(_school_year_of(start), _school_year_of(end) + 1))

    def blocks_for_period(self, start: date, end: date) -> list[CatalogBlock]:
        """Blocks overlapping ``[start, end]``, clipped to it (official dates kept in ``full_start/full_end``)."""
        result = []
        for b in self.blocks:
            if b.end < start or b.start > end:
                continue
            result.append(replace(b, start=max(b.start, start), end=min(b.end, end), full_start=b.start,
                                  full_end=b.end))
        return sorted(result, key=lambda b: (b.start, b.end))

    @property
    def retrieved_de(self) -> str:
        return self.retrieved.strftime("%d.%m.%Y")


def _school_year_of(d: date) -> int:
    """Start year of the Bavarian school year (01.08.–31.07., Art. 5 BayEUG) containing ``d``."""
    return d.year if d.month >= 8 else d.year - 1


# --------------------------------------------------------------------------- file lookup


def catalog_path() -> Path:
    custom = (os.environ.get(ENV_VAR) or "").strip()
    if custom:
        path = Path(custom)
        return path if path.is_absolute() else PROJECT_ROOT / path
    return DEFAULT_FILE


# --------------------------------------------------------------------------- validation


def _date(value: Any, what: str) -> date:
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise FerienCatalogError(f"{what}: „{value}“ ist kein Datum (JJJJ-MM-TT).")


def _text(value: Any, what: str, *, required: bool = True, max_len: int = 500) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise FerienCatalogError(f"{what} fehlt.")
        return ""
    if isinstance(value, bool | dict | list):
        raise FerienCatalogError(f"{what} muss Text sein.")
    text = str(value).strip()
    if len(text) > max_len:
        raise FerienCatalogError(f"{what} ist zu lang (höchstens {max_len} Zeichen).")
    return text


def _school_year(value: Any, what: str) -> tuple[str, int]:
    text = _text(value, what, max_len=7)
    m = _SCHOOL_YEAR_RE.match(text)
    if not m or (int(m.group(1)) + 1) % 100 != int(m.group(2)):
        raise FerienCatalogError(f"{what}: „{text}“ ist kein Schuljahr wie „2026/27“.")
    return text, int(m.group(1))


def _block(raw: Any, index: int) -> CatalogBlock:
    what = f"Block {index}"
    if not isinstance(raw, dict):
        raise FerienCatalogError(f"{what} muss eine Zuordnung sein.")
    school_year, year = _school_year(raw.get("school_year"), f"{what} (school_year)")
    name = _text(raw.get("name"), f"{what} (name)", max_len=100)
    what = f"Block {index} „{name}“"
    by_name = classify_ferien(name)
    raw_kind = raw.get("kind")
    if raw_kind in (None, ""):
        kind = by_name
    else:
        try:
            kind = FerienKind(str(raw_kind))
        except ValueError:
            raise FerienCatalogError(f"{what}: unbekannte Ferienart „{raw_kind}“.") from None
        if by_name is not FerienKind.SONSTIGE and by_name is not kind:
            raise FerienCatalogError(f"{what}: Ferienart „{kind.value}“ passt nicht zum Namen ({by_name.value}).")
    if kind is FerienKind.SONSTIGE:
        raise FerienCatalogError(f"{what}: Ferienart nicht erkennbar – bitte „kind“ angeben.")
    start = _date(raw.get("start"), f"{what} (start)")
    end = _date(raw.get("end"), f"{what} (end)")
    if end < start:
        raise FerienCatalogError(f"{what}: Ende liegt vor dem Beginn.")
    if (end - start).days + 1 > MAX_BLOCK_DAYS:
        raise FerienCatalogError(f"{what}: länger als {MAX_BLOCK_DAYS} Tage.")
    # The summer holidays opening a school year may start in July; everything else lies within 01.08.–30.09.+1.
    if start < date(year, 7, 1) or end > date(year + 1, 9, 30):
        raise FerienCatalogError(f"{what}: liegt nicht im Schuljahr {school_year}.")
    official = _text(raw.get("official"), f"{what} (official)", required=False)
    return CatalogBlock(school_year=school_year, name=name, kind=kind, start=start, end=end, official=official)


def validate(data: Any, path: str = "") -> Catalog:
    """Validate parsed YAML data and build a :class:`Catalog` (raises :class:`FerienCatalogError`)."""
    if not isinstance(data, dict):
        raise FerienCatalogError("Der Ferienkatalog muss eine Zuordnung mit „blocks“ sein.")
    bundesland = _text(data.get("bundesland") or SUPPORTED_BUNDESLAND, "bundesland", max_len=2).upper()
    source_url = _text(data.get("source_url"), "source_url")
    if not source_url.startswith(("https://", "http://")):
        raise FerienCatalogError("source_url muss mit http:// oder https:// beginnen.")
    source_page = _text(data.get("source_page"), "source_page", required=False) or None
    if source_page and not source_page.startswith(("https://", "http://")):
        raise FerienCatalogError("source_page muss mit http:// oder https:// beginnen.")
    source_title = _text(data.get("source_title"), "source_title")
    source_short = _text(data.get("source_short"), "source_short", required=False) or source_title
    retrieved = _date(data.get("retrieved"), "retrieved")
    note = _text(data.get("note"), "note", required=False)
    raw_partial = data.get("partial_school_years") or []
    if not isinstance(raw_partial, list):
        raise FerienCatalogError("partial_school_years muss eine Liste sein.")
    partial = frozenset(_school_year(p, "partial_school_years")[0] for p in raw_partial)

    raw_blocks = data.get("blocks")
    if not isinstance(raw_blocks, list):
        raise FerienCatalogError("„blocks“ fehlt oder ist keine Liste.")
    if len(raw_blocks) > MAX_BLOCKS:
        raise FerienCatalogError(f"Zu viele Blöcke (höchstens {MAX_BLOCKS}).")
    blocks = sorted((_block(raw, i) for i, raw in enumerate(raw_blocks, start=1)), key=lambda b: (b.start, b.end))

    for prev, cur in zip(blocks, blocks[1:], strict=False):
        if cur.start <= prev.end:
            raise FerienCatalogError(
                f"„{cur.name}“ ({cur.school_year}) überschneidet sich mit „{prev.name}“ ({prev.school_year})."
            )
    by_year: dict[str, set[FerienKind]] = {}
    for b in blocks:
        by_year.setdefault(b.school_year, set()).add(b.kind)
    for school_year, kinds in sorted(by_year.items()):
        if school_year in partial:
            continue
        missing = REQUIRED_KINDS - kinds
        if missing:
            names = ", ".join(sorted(k.value for k in missing))
            raise FerienCatalogError(
                f"Schuljahr {school_year} ist unvollständig (es fehlen: {names}) – "
                "bitte ergänzen oder unter partial_school_years eintragen."
            )
    return Catalog(
        path=path,
        bundesland=bundesland,
        source_url=source_url,
        source_title=source_title,
        source_short=source_short,
        source_page=source_page,
        retrieved=retrieved,
        note=note,
        blocks=tuple(blocks),
        partial_school_years=partial,
    )


def _read(file: Path, mtime: float | None) -> Catalog:
    if mtime is None:
        raise FerienCatalogError(f"Ferienkatalog nicht gefunden: {file}")
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise FerienCatalogError(f"Ferienkatalog ist nicht lesbar: {exc}") from None
    return validate(data, str(file))


def load_catalog(path: Path | str | None = None) -> Catalog:
    """Load and validate the catalogue file. Raises :class:`FerienCatalogError`.

    Cached per (path, mtime), a failure as well: a missing or broken file is read, validated and logged once per
    change of the file, every further call raises the cached error without touching the file again.
    """
    file = Path(path) if path is not None else catalog_path()
    try:
        mtime: float | None = file.stat().st_mtime
    except OSError:
        mtime = None
    key = (str(file), mtime)
    cached = _cache.get(key)
    if cached is None:
        try:
            cached = _read(file, mtime)
        except FerienCatalogError as exc:
            cached = str(exc)
            log.error("Ferienkatalog nicht verfügbar: %s", cached)
        _cache.clear()
        _cache[key] = cached
    if isinstance(cached, str):
        raise FerienCatalogError(cached)  # a fresh exception: re-raising one instance would grow its traceback
    return cached


def get_catalog(bundesland: str | None = SUPPORTED_BUNDESLAND) -> Catalog | None:
    """The catalogue for ``bundesland`` or ``None`` (other Bundesland, or the file is missing/invalid – logged)."""
    if (bundesland or "").upper() != SUPPORTED_BUNDESLAND:
        return None
    try:
        catalog = load_catalog()
    except FerienCatalogError:
        return None  # logged by load_catalog (once per change of the file)
    if catalog.bundesland != SUPPORTED_BUNDESLAND:
        if (catalog.path, catalog.bundesland) not in _mismatch_logged:
            _mismatch_logged.add((catalog.path, catalog.bundesland))
            log.error("Ferienkatalog %s gilt für %s, nicht für %s", catalog.path, catalog.bundesland,
                      SUPPORTED_BUNDESLAND)
        return None
    return catalog


# --------------------------------------------------------------------------- public helpers


def coverage(bundesland: str | None = SUPPORTED_BUNDESLAND) -> Coverage:
    """First/last complete school year (empty for other Bundesländer or without a catalogue)."""
    catalog = get_catalog(bundesland)
    if catalog is None:
        return Coverage(first=None, last=None, school_years=(), partial=())
    return catalog.coverage()


def covers_period(start: date, end: date, bundesland: str | None = SUPPORTED_BUNDESLAND) -> bool:
    catalog = get_catalog(bundesland)
    return catalog is not None and catalog.covers(start, end)


def blocks_for_period(
    start: date, end: date, bundesland: str | None = SUPPORTED_BUNDESLAND
) -> list[CatalogBlock]:
    """Catalogue blocks overlapping ``[start, end]``, clipped to it; ``[]`` for other Bundesländer."""
    catalog = get_catalog(bundesland)
    return catalog.blocks_for_period(start, end) if catalog is not None else []
