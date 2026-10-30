"""Benchmarks (Konzept §9.4): reference values from published working-time studies.

The values live in an editable YAML file, never in code. Lookup order for the file:

1. environment variable ``BENCHMARKS_FILE`` (absolute, or relative to the project root),
2. ``<DATA_DIR>/benchmarks.yaml`` if it exists (lets the container volume hold a custom file),
3. ``config/benchmarks.yaml`` in the project root (shipped default).

File format::

    benchmarks:
      - label: "Grundschule Niedersachsen"
        hours_per_week: "47:58"        # number (47.97 / "47,97") or "h:mm"
        source: "Kooperationsstelle Universität Göttingen"   # Pflichtfeld
        year: "2015/16"                                      # Pflichtfeld
        kind: studie                   # studie | verbandsschaetzung | sonstiges (default studie)
        note: "optional"

Public API
----------
- :func:`load_benchmarks` -> ``list[dict]`` with ``label, hours_per_week (float), source, year, kind,
  note`` plus ``kind_label``, ``hint`` ("Studienwert, nicht Norm") and ``hours_text`` ("47:58").
  Invalid entries are skipped (logged); ``strict=True`` raises :class:`BenchmarkError` instead.
- :func:`validate` -> ``(items, errors)`` for already parsed YAML data (used by tests / a settings UI).
- :func:`benchmarks_path` -> the file that is used.

Results are cached per (path, mtime), so edits to the file apply without a restart.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

from app.config import PROJECT_ROOT, get_config

log = logging.getLogger("app.services.benchmarks")

ENV_VAR = "BENCHMARKS_FILE"
DEFAULT_FILE = PROJECT_ROOT / "config" / "benchmarks.yaml"
DATA_FILE_NAME = "benchmarks.yaml"

HINT = "Studienwert, nicht Norm"
KINDS: dict[str, str] = {
    "studie": "Studienwert, nicht Norm",
    "verbandsschaetzung": "Verbandsschätzung, nicht Norm",
    "sonstiges": "Vergleichswert, nicht Norm",
}
MIN_HOURS, MAX_HOURS = 1.0, 100.0
MAX_ENTRIES = 50
MAX_TEXT = 300

_HM_RE = re.compile(r"^\s*(\d{1,3}):([0-5]\d)\s*$")

_cache: dict[tuple[str, float], list[dict[str, Any]]] = {}


class BenchmarkError(ValueError):
    """The benchmarks file is missing, unreadable or invalid (message in German)."""


# --------------------------------------------------------------------------- file lookup


def benchmarks_path() -> Path:
    """The YAML file in use (see module docstring for the lookup order)."""
    custom = (os.environ.get(ENV_VAR) or "").strip()
    if custom:
        path = Path(custom)
        return path if path.is_absolute() else PROJECT_ROOT / path
    try:
        data_file = get_config().data_dir / DATA_FILE_NAME
    except Exception:  # config not loadable (e.g. tooling) -> shipped default
        data_file = None
    if data_file is not None and data_file.is_file():
        return data_file
    return DEFAULT_FILE


# --------------------------------------------------------------------------- parsing


def parse_hours(value: Any) -> float:
    """``47.97`` / ``"47,97"`` / ``"47:58"`` -> hours as float. Raises ValueError."""
    if isinstance(value, bool):
        raise ValueError("keine Zahl")
    if isinstance(value, int | float):
        hours = float(value)
    elif isinstance(value, str):
        match = _HM_RE.match(value)
        if match:
            hours = int(match.group(1)) + int(match.group(2)) / 60
        else:
            hours = float(value.strip().replace(",", "."))
    else:
        raise ValueError("keine Zahl")
    if hours != hours or not MIN_HOURS <= hours <= MAX_HOURS:  # NaN or out of range
        raise ValueError(f"muss zwischen {MIN_HOURS:g} und {MAX_HOURS:g} Stunden liegen")
    return hours


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool | dict | list):
        raise ValueError("muss Text sein")
    text = str(value).strip()
    if len(text) > MAX_TEXT:
        raise ValueError(f"ist zu lang (höchstens {MAX_TEXT} Zeichen)")
    return text


def _hours_text(hours: float) -> str:
    minutes = round(hours * 60)
    return f"{minutes // 60}:{minutes % 60:02d}"


def _entry(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Eintrag ist kein Objekt")
    label = _text(raw.get("label"))
    if not label:
        raise ValueError("„label“ fehlt")
    try:
        hours = parse_hours(raw.get("hours_per_week"))
    except ValueError as exc:
        raise ValueError(f"„hours_per_week“ {exc}") from None
    source = _text(raw.get("source"))
    if not source:
        raise ValueError("Pflichtfeld „source“ fehlt")
    year = _text(raw.get("year"))
    if not year:
        raise ValueError("Pflichtfeld „year“ fehlt")
    kind = (_text(raw.get("kind")) or "studie").lower()
    if kind not in KINDS:
        raise ValueError(f"„kind“ muss einer von {', '.join(KINDS)} sein")
    return {
        "label": label,
        "hours_per_week": round(hours, 4),
        "hours_text": _hours_text(hours),
        "source": source,
        "year": year,
        "kind": kind,
        "kind_label": KINDS[kind],
        "hint": HINT,
        "note": _text(raw.get("note")),
    }


def validate(data: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate parsed YAML (``{"benchmarks": [...]}`` or a bare list). Returns (valid items, errors)."""
    if data is None:
        return [], []
    items = data.get("benchmarks") if isinstance(data, dict) else data
    if items is None:
        return [], []
    if not isinstance(items, list):
        return [], ["„benchmarks“ muss eine Liste sein."]
    result: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, raw in enumerate(items, start=1):
        if len(result) >= MAX_ENTRIES:
            errors.append(f"Mehr als {MAX_ENTRIES} Einträge – der Rest wird ignoriert.")
            break
        try:
            result.append(_entry(raw))
        except ValueError as exc:
            label = raw.get("label") if isinstance(raw, dict) else None
            errors.append(f"Eintrag {i}{f' ({label})' if label else ''}: {exc}.")
    return result, errors


def load_benchmarks(path: Path | str | None = None, *, strict: bool = False) -> list[dict[str, Any]]:
    """Load and validate the benchmarks file. Missing file -> ``[]`` (or BenchmarkError when strict)."""
    file = Path(path) if path is not None else benchmarks_path()
    try:
        mtime = file.stat().st_mtime
    except OSError:
        if strict:
            raise BenchmarkError(f"Benchmark-Datei nicht gefunden: {file}") from None
        log.warning("Benchmark file not found: %s", file)
        return []
    key = (str(file), mtime)
    if not strict and key in _cache:
        return [dict(item) for item in _cache[key]]
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        if strict:
            raise BenchmarkError(f"Benchmark-Datei ist nicht lesbar: {exc}") from None
        log.warning("Benchmark file %s unreadable: %s", file, exc)
        return []
    items, errors = validate(data)
    if errors:
        if strict:
            raise BenchmarkError(" ".join(errors))
        for err in errors:
            log.warning("Benchmark file %s: %s", file, err)
    _cache.clear()
    _cache[key] = items
    return [dict(item) for item in items]
