"""Evaluation of recorded time per day with the Klassenfahrt variants, Konzept §6.7.

Variant A counts every entry including night standby. Variant B (default,
conservative) applies to every *day* with at least one entry flagged
``klassenfahrt``: entries flagged ``nachtbereitschaft`` are dropped and the
sum of all remaining entries of that day (flagged or not) is capped at
``cap_h`` hours. Days without a Klassenfahrt entry count normally.

The result is the ``ist_by_day`` mapping that :func:`app.core.account.build_ledger`
expects. Pseudo entries (Krank) must be filtered out by the caller.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date

from .types import FLAG_KLASSENFAHRT, FLAG_NACHTBEREITSCHAFT, KlassenfahrtVariante

DEFAULT_CAP_H = 10.0

# (day, duration_seconds, flags)
EntryTuple = tuple[date, int, Iterable[str]]


def evaluate_ist(
    entries: Iterable[EntryTuple],
    variante: KlassenfahrtVariante | str = KlassenfahrtVariante.B,
    cap_h: float = DEFAULT_CAP_H,
) -> dict[date, int]:
    """Sum recorded seconds per day, applying the Klassenfahrt variant."""
    variante = KlassenfahrtVariante(variante)
    cap = round(cap_h * 3600)
    totals: dict[date, int] = defaultdict(int)
    trip_days: set[date] = set()

    for day, duration, flags in entries:
        flags = frozenset(flags or ())
        if variante is KlassenfahrtVariante.B:
            if FLAG_KLASSENFAHRT in flags:
                trip_days.add(day)
            if FLAG_NACHTBEREITSCHAFT in flags:
                totals.setdefault(day, 0)
                continue
        totals[day] += duration

    return {day: min(s, cap) if day in trip_days else s for day, s in sorted(totals.items())}
