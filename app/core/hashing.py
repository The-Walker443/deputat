"""Canonical serialisation and SHA-256 of the entries of a closed month (Konzept §10.1).

Canonical form (version 1):

* only the fields in :data:`HASH_FIELDS`, in exactly that order; missing
  fields are ``null``. Bookkeeping fields (``locked``, ``updated_at``) are
  excluded so that closing a month does not change its own hash.
* datetimes in UTC as ``YYYY-MM-DDTHH:MM:SSZ`` (microseconds appended only
  if non-zero; naive datetimes are taken as UTC), dates as ISO, enums by value,
  sets/lists of flags sorted, nested mappings with sorted keys.
* entries sorted by ``(start_at, id)``.
* compact JSON (no whitespace), UTF-8, wrapped as
  ``{"v":1,"entries":[...]}``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

HASH_VERSION = 1

HASH_FIELDS: tuple[str, ...] = (
    "id",
    "day",
    "start_at",
    "end_at",
    "duration_seconds",
    "has_bounds",
    "category_id",
    "subcategory_id",
    "klasse",
    "fach",
    "location",
    "note",
    "flags",
    "source",
    "status",
    "import_batch_id",
    "created_at",
    "deleted_at",
)


def _norm(value: Any) -> Any:
    if isinstance(value, datetime):
        dt = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if dt.microsecond else "%Y-%m-%dT%H:%M:%SZ"
        return dt.strftime(fmt)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _norm(value.value)
    if isinstance(value, Mapping):
        return {str(k): _norm(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (set, frozenset)):
        return sorted(_norm(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    return value


def canonical_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    result = {field: _norm(entry.get(field)) for field in HASH_FIELDS}
    if isinstance(result["flags"], list):
        result["flags"] = sorted(result["flags"])
    return result


def canonical_entries(entries: Iterable[Mapping[str, Any]]) -> bytes:
    """Canonical UTF-8 JSON bytes of ``entries`` (see module docstring)."""
    rows = [canonical_entry(e) for e in entries]
    rows.sort(key=lambda r: (r["start_at"] or "", str(r["id"] or "")))
    payload = {"v": HASH_VERSION, "entries": rows}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def entries_hash(entries: Iterable[Mapping[str, Any]]) -> str:
    """SHA-256 hex digest of :func:`canonical_entries`."""
    return hashlib.sha256(canonical_entries(entries)).hexdigest()
