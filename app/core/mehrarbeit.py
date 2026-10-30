"""Mehrarbeit in the narrow sense (Art. 87 BayBG, KMBek), Konzept §6.6.

Only teaching entries flagged ``vertretung_angeordnet`` or
``vertretung_genehmigt`` count; the caller passes the teaching entries
(category ``is_unterricht``). Teaching hours (UStd) = minutes / 45, rounded
to the nearest 0.5 (halves up). Each figure is rounded from the raw seconds
(no rounding before summation), so ``total`` may differ from
``angeordnet + genehmigt`` by 0.5. An entry carrying both flags counts as
angeordnet.

Bagatellgrenze: the month is compensable only if total > 3 UStd, then from
the first hour.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from .types import FLAG_VERTRETUNG_ANGEORDNET, FLAG_VERTRETUNG_GENEHMIGT

UNTERRICHTSSTUNDE_MIN = 45
BAGATELLGRENZE_USTD = 3

EntryTuple = tuple[date, int, Iterable[str]]


@dataclass(frozen=True)
class MehrarbeitMonth:
    ustd_angeordnet: float
    ustd_genehmigt: float
    total: float
    ausgleichsfaehig: float


def seconds_to_ustd(seconds: int, unit_minutes: int = UNTERRICHTSSTUNDE_MIN) -> float:
    """Seconds -> teaching hours rounded to 0.5 (halves up)."""
    halves = Fraction(seconds * 2, unit_minutes * 60)
    return float(Fraction(int((halves + Fraction(1, 2)) // 1), 2))


def month_mehrarbeit(
    entries: Iterable[EntryTuple],
    unit_minutes: int = UNTERRICHTSSTUNDE_MIN,
    bagatellgrenze: float = BAGATELLGRENZE_USTD,
) -> MehrarbeitMonth:
    """Aggregate the given entries (one month) into UStd figures."""
    angeordnet = genehmigt = 0
    for _day, duration, flags in entries:
        flags = frozenset(flags or ())
        if FLAG_VERTRETUNG_ANGEORDNET in flags:
            angeordnet += duration
        elif FLAG_VERTRETUNG_GENEHMIGT in flags:
            genehmigt += duration
    total = seconds_to_ustd(angeordnet + genehmigt, unit_minutes)
    return MehrarbeitMonth(
        ustd_angeordnet=seconds_to_ustd(angeordnet, unit_minutes),
        ustd_genehmigt=seconds_to_ustd(genehmigt, unit_minutes),
        total=total,
        ausgleichsfaehig=total if total > bagatellgrenze else 0.0,
    )


def mehrarbeit_by_month(
    entries: Iterable[EntryTuple], unit_minutes: int = UNTERRICHTSSTUNDE_MIN
) -> dict[tuple[int, int], MehrarbeitMonth]:
    """Group entries by (year, month) of their day and evaluate each month."""
    groups: dict[tuple[int, int], list[EntryTuple]] = defaultdict(list)
    for entry in entries:
        groups[(entry[0].year, entry[0].month)].append(entry)
    return {k: month_mehrarbeit(v, unit_minutes) for k, v in sorted(groups.items())}
