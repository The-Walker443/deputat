"""Working-time account: ledger rows, saldo, forecasts and key figures (Konzept §6.3–§6.5).

Conventions
-----------
* All amounts are integer seconds; forecasts are computed with exact
  fractions and rounded half up once at the end.
* ``today`` is part of the past: ``saldo_at(ledger, today)`` includes today,
  projections start tomorrow. The per-school-day average for projections uses
  school days strictly before ``today`` (today is usually still incomplete).
* "Ferienarbeit" is recorded time (``ist_erfasst``) on ferien days: days of
  type ``ferientag`` and the weekends/holidays inside a ferien block. A day
  inside a block that was turned into a school day by an override is not
  ferien (see :func:`is_ferien_row`).
* Comparisons against 48 h use recorded time (``ist_erfasst``), not the
  sick-day credit.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction
from itertools import groupby

from .soll import day_solls, round_half_up, tagessoll_exact
from .types import AbsenceType, Ampel, Day, DayLedger, DayType, FerienKind, FerienRange, SollParams

DEFAULT_PROGNOSE_SCHULTAGE = 20
EU_WINDOW_DAYS = 365
EU_WEEKS = 52
EU_GELB_AB_H = 44
EU_ROT_UEBER_H = 48


def _round(value: Fraction) -> int:
    """Round half up (towards +inf on exact halves), also for negative values."""
    return round_half_up(value)


# --------------------------------------------------------------------------- ledger


def build_ledger(
    days: Iterable[Day],
    p: SollParams,
    ist_by_day: Mapping[date, int],
    krank_neutralisiert: bool = True,
    krank_partial: Mapping[date, int] | None = None,
    erfassung_ab: date | None = None,
) -> list[DayLedger]:
    """Build one account row per day (Konzept §6.3).

    ``ist_by_day`` holds recorded seconds per local day, already evaluated
    with the Klassenfahrt variant and *without* Krank pseudo entries.
    ``krank_partial`` holds the seconds of Krank pseudo entries per day.

    With ``krank_neutralisiert``: a day with absence ``krank`` is credited its
    full soll (ist = recorded + soll, Konzept §6.3); otherwise Krank pseudo
    entries only fill the gap between recorded time and soll ("neutralises
    only the soll"), so a partial sick credit never creates a surplus.

    ``erfassung_ab`` (start of recording): earlier days are neutral – ist = soll, nothing recorded counts,
    ``vor_erfassung=True`` (they are no sample for projections). Starting in the middle of a school year
    therefore does not turn every past school day into "0 h worked".

    Day targets come from :func:`app.core.soll.day_solls`, so
    ``Σ row.soll == period_soll(days)`` exactly, also for part-time factors
    whose day target is not a whole number of seconds.
    """
    krank_partial = krank_partial or {}
    ordered = sorted(days, key=lambda d: d.date)
    rows: list[DayLedger] = []
    for day, soll in zip(ordered, day_solls(ordered, p), strict=True):
        if erfassung_ab is not None and day.date < erfassung_ab:
            rows.append(
                DayLedger(
                    date=day.date,
                    soll=soll,
                    ist=soll,
                    day_type=day.day_type,
                    absence=day.absence,
                    ferien_id=day.ferien_id,
                    is_holiday=day.is_holiday,
                    vor_erfassung=True,
                )
            )
            continue
        erfasst = int(ist_by_day.get(day.date, 0))
        credit = 0
        if krank_neutralisiert:
            if day.absence == AbsenceType.KRANK:
                credit = soll
            elif day.date in krank_partial:
                credit = min(int(krank_partial[day.date]), max(0, soll - erfasst))
        rows.append(
            DayLedger(
                date=day.date,
                soll=soll,
                ist=erfasst + credit,
                day_type=day.day_type,
                absence=day.absence,
                ferien_id=day.ferien_id,
                ist_erfasst=erfasst,
                krank_gutschrift=credit,
                is_holiday=day.is_holiday,
            )
        )
    return rows


def j_soll(ledger: Iterable[DayLedger]) -> int:
    """J_soll = Σ soll over the period."""
    return sum(r.soll for r in ledger)


def saldo_series(ledger: Iterable[DayLedger]) -> list[tuple[date, int]]:
    """Cumulative saldo after each day."""
    series: list[tuple[date, int]] = []
    running = 0
    for row in sorted(ledger, key=lambda r: r.date):
        running += row.delta
        series.append((row.date, running))
    return series


def saldo_at(ledger: Iterable[DayLedger], t: date) -> int:
    """saldo(t) = Σ_{d ≤ t} (ist(d) − soll(d))."""
    return sum(r.delta for r in ledger if r.date <= t)


def saldo_tage(saldo: int, p: SollParams) -> float:
    """Saldo expressed in working days of this teacher (0.0 if the day target is 0)."""
    per_day = tagessoll_exact(p)
    return float(saldo / per_day) if per_day else 0.0


def ist_bisher(ledger: Iterable[DayLedger], today: date) -> int:
    return sum(r.ist for r in ledger if r.date <= today)


def is_ferien_row(r: DayLedger) -> bool:
    """True for ferien days: ``ferientag`` or a non-school day inside a ferien block."""
    return r.day_type == DayType.FERIENTAG or (
        r.ferien_id is not None and r.day_type != DayType.SCHULTAG
    )


def ferienarbeit(ledger: Iterable[DayLedger], until: date | None = None) -> int:
    """Recorded seconds on ferien days (optionally up to ``until``)."""
    return sum(
        r.ist_erfasst for r in ledger if is_ferien_row(r) and (until is None or r.date <= until)
    )


# --------------------------------------------------------------------- projections


def ist_pro_schultag(
    ledger: Iterable[DayLedger], today: date, n_schultage: int = DEFAULT_PROGNOSE_SCHULTAGE
) -> Fraction | None:
    """Mean ist over the last ``n_schultage`` school days before ``today``.

    Only regular school days without absence count. ``None`` if there is no
    such day yet (projections then assume ist = soll, i.e. a neutral account).
    """
    past = [
        r.ist
        for r in sorted(ledger, key=lambda r: r.date)
        if r.date < today and r.day_type == DayType.SCHULTAG and r.absence is None and not r.vor_erfassung
    ]
    sample = past[-n_schultage:] if n_schultage > 0 else []
    return Fraction(sum(sample), len(sample)) if sample else None


def _projected_delta(
    ledger: Iterable[DayLedger],
    after: date,
    before: date | None,
    avg: Fraction | None,
) -> Fraction:
    """Expected saldo change for days in (after, before)."""
    total = Fraction(0)
    for r in ledger:
        if r.date <= after or (before is not None and r.date >= before):
            continue
        if r.day_type == DayType.SCHULTAG and r.soll > 0:
            total += (avg if avg is not None else r.soll) - r.soll
        else:
            total -= r.soll  # no work expected on ferien / school-free days
    return total


def prognose_ende(
    ledger: Sequence[DayLedger],
    today: date,
    n_schultage: int = DEFAULT_PROGNOSE_SCHULTAGE,
    geplante_ferienarbeit: int = 0,
) -> int:
    """Forecast saldo at the end of the period (Konzept §6.4)."""
    avg = ist_pro_schultag(ledger, today, n_schultage)
    value = saldo_at(ledger, today) + _projected_delta(ledger, today, None, avg) + geplante_ferienarbeit
    return _round(value)


@dataclass(frozen=True)
class HolidayForecast:
    ferien_id: str
    name: str
    kind: FerienKind
    start: date
    end: date
    prognose_bis: int  # expected saldo change between today and the start of F
    saldo_vor: int  # saldo when entering F (for a running block: saldo today)
    abbau_kapazitaet: int  # Σ soll of the (remaining) days in F
    erwartete_ferienarbeit: int  # expected work in the (remaining) days of F
    saldo_nach: int
    ferienarbeit_bisher: int = 0  # work already recorded in F (running block), part of saldo_vor


FerienHistory = Mapping[FerienKind, Sequence[int]]


def ferien_work_history(
    ledger: Iterable[DayLedger], ferien: Iterable[FerienRange], until: date
) -> dict[FerienKind, list[int]]:
    """Recorded work per completed ferien block, grouped by kind (oldest first).

    Only blocks that ended on or before ``until`` *and* whose every date is
    covered by ``ledger`` are included, so a block without data never enters
    an average as 0. The ledger may span several periods: it is used for the
    history only, never for a saldo.
    """
    rows = list(ledger)
    covered = {r.date for r in rows if not r.vor_erfassung}
    work_by_id: dict[str, int] = {}
    for r in rows:
        if r.ferien_id is not None and is_ferien_row(r):
            work_by_id[r.ferien_id] = work_by_id.get(r.ferien_id, 0) + r.ist_erfasst

    history: dict[FerienKind, list[int]] = {}
    for f in sorted(ferien, key=lambda f: (f.start, f.end, f.id)):
        span = (f.end - f.start).days + 1
        if f.end > until or any(f.start + timedelta(days=i) not in covered for i in range(span)):
            continue
        history.setdefault(FerienKind(f.kind), []).append(work_by_id.get(f.id, 0))
    return history


def default_ferienarbeit(history: FerienHistory, kind: FerienKind) -> int:
    """Mean recorded work of the past blocks of ``kind`` in ``history`` (0 without history)."""
    totals = list(history.get(kind, ()))
    return _round(Fraction(sum(totals), len(totals))) if totals else 0


def holiday_forecast(
    ledger: Sequence[DayLedger],
    today: date,
    ferien: Iterable[FerienRange],
    planned_work: Mapping[str, int] | None = None,
    n_schultage: int = DEFAULT_PROGNOSE_SCHULTAGE,
    history: FerienHistory | None = None,
) -> list[HolidayForecast]:
    """Ferien-Guthaben-Rechner for every ferien block that has not ended yet.

    saldo_nach_F = saldo_heute + prognose_bis_F − abbau_kapazitaet(F) + erwartete_ferienarbeit(F)

    ``ledger`` is the ledger of the current period only (it defines the saldo,
    Konzept §1.3). Blocks are chained chronologically: the forecast for a later
    block starts from the ``saldo_nach`` of the previous one.

    ``planned_work`` maps ferien ids to the user's estimate of the work in the
    whole block. Missing entries default to the mean of past blocks of the same
    kind taken from ``history`` (kind → recorded seconds per past block, see
    :func:`ferien_work_history`; the previous period's ledger is needed for
    that because each kind usually occurs once per school year). Without
    ``history`` the completed blocks within ``ledger`` are used.

    For a block that is already running only the days after ``today`` count:
    the capacity is their soll, and the expected work is the block estimate
    minus the work already recorded in the block (that work is in saldo_heute).
    """
    planned_work = planned_work or {}
    ferien_all = sorted(ferien, key=lambda f: (f.start, f.end, f.id))
    if history is None:
        history = ferien_work_history(ledger, ferien_all, today)
    avg = ist_pro_schultag(ledger, today, n_schultage)

    running = Fraction(saldo_at(ledger, today))
    cursor = today
    result: list[HolidayForecast] = []
    for f in ferien_all:
        if f.end <= today:
            continue
        prognose = _projected_delta(ledger, cursor, f.start, avg) if f.start > cursor else Fraction(0)
        running += prognose
        saldo_vor = running
        block = [r for r in ledger if r.ferien_id == f.id and is_ferien_row(r)]
        abbau = sum(r.soll for r in block if r.date > today)
        done = sum(r.ist_erfasst for r in block if r.date <= today)
        total = planned_work.get(f.id)
        if total is None:
            total = default_ferienarbeit(history, FerienKind(f.kind))
        expected = max(0, int(total) - done)
        running = running - abbau + expected
        result.append(
            HolidayForecast(
                ferien_id=f.id,
                name=f.name,
                kind=FerienKind(f.kind),
                start=f.start,
                end=f.end,
                prognose_bis=_round(prognose),
                saldo_vor=_round(saldo_vor),
                abbau_kapazitaet=abbau,
                erwartete_ferienarbeit=expected,
                saldo_nach=_round(running),
                ferienarbeit_bisher=done,
            )
        )
        cursor = max(cursor, f.end)
    return result


# ------------------------------------------------------------------------- budget


def schulwochen_gesamt(ledger: Iterable[DayLedger]) -> Fraction:
    """Number of school weeks of the period, normalised to 5 school days per week.

    Partial weeks (e.g. with a holiday) count proportionally.
    """
    return Fraction(sum(1 for r in ledger if r.day_type == DayType.SCHULTAG), 5)


def w_max(ledger: Sequence[DayLedger], today: date, ferienarbeit_geplant: int = 0) -> int:
    """Admissible seconds per school week (Konzept §6.4).

    W_max = (J_soll − ferienarbeit_bisher − ferienarbeit_geplant) / schulwochen_gesamt
    """
    weeks = schulwochen_gesamt(ledger)
    if weeks == 0:
        return 0
    budget = j_soll(ledger) - ferienarbeit(ledger, until=today) - ferienarbeit_geplant
    return _round(Fraction(budget) / weeks)


# ------------------------------------------------------------------- EU 48 h limit


def eu_ampel(hours: float | Fraction) -> Ampel:
    """gruen < 44 h ≤ gelb ≤ 48 h < rot."""
    if hours > EU_ROT_UEBER_H:
        return Ampel.ROT
    if hours >= EU_GELB_AB_H:
        return Ampel.GELB
    return Ampel.GRUEN


@dataclass(frozen=True)
class EuMittel:
    hours: float
    ampel: Ampel
    ist_seconds: int
    absence_days: int  # urlaub + krank working days (Mon–Fri, no holiday) in the window
    divisor_weeks: float  # 52 − urlaubswochen_im_fenster
    window_start: date
    window_end: date


def eu_mittel(
    ist_by_day: Mapping[date, int],
    absences_by_day: Mapping[date, AbsenceType | str | None],
    today: date,
    holidays: Collection[date] = frozenset(),
) -> EuMittel:
    """12-month mean of weekly working time (Konzept §6.5).

    Window: the 365 days ending with ``today``. ``ist_by_day`` must be recorded
    time *without* the sick-day credit, because urlaub and krank days are
    neutralised by shrinking the divisor:
    divisor = 52 − (urlaub + krank working days in the window) / 5.

    Only working days count as absent: Mon–Fri dates that are no public
    holiday (``holidays``). The 52 weeks already contain weekends and public
    holidays, so an absence marked on such a day must not shrink the divisor.
    """
    start = today - timedelta(days=EU_WINDOW_DAYS - 1)
    ist = sum(s for d, s in ist_by_day.items() if start <= d <= today)
    neutral = {AbsenceType.URLAUB, AbsenceType.KRANK}
    absent = sum(
        1
        for d, a in absences_by_day.items()
        if start <= d <= today
        and a is not None
        and AbsenceType(a) in neutral
        and d.isoweekday() <= 5
        and d not in holidays
    )
    divisor = EU_WEEKS - Fraction(absent, 5)
    hours = Fraction(ist, 3600) / divisor if divisor > 0 else Fraction(0)
    return EuMittel(
        hours=float(hours),
        ampel=eu_ampel(hours),
        ist_seconds=ist,
        absence_days=absent,
        divisor_weeks=float(divisor),
        window_start=start,
        window_end=today,
    )


# ------------------------------------------------------------------- aggregations


@dataclass(frozen=True)
class WeekSummary:
    iso_year: int
    iso_week: int
    monday: date
    soll: int
    ist: int
    ist_erfasst: int
    schultage: int

    @property
    def delta(self) -> int:
        return self.ist - self.soll

    @property
    def is_schulwoche(self) -> bool:
        return self.schultage > 0


@dataclass(frozen=True)
class MonthSummary:
    year: int
    month: int
    soll: int
    ist: int
    ist_erfasst: int
    ferienarbeit: int
    saldo_end: int  # cumulative saldo at the last day of the month

    @property
    def delta(self) -> int:
        return self.ist - self.soll


def week_summary(ledger: Iterable[DayLedger]) -> list[WeekSummary]:
    """Aggregate per ISO week (Mon–Sun); weeks cut by the period edges are partial."""
    rows = sorted(ledger, key=lambda r: r.date)
    result: list[WeekSummary] = []
    for (iso_year, iso_week), group in groupby(rows, key=lambda r: r.date.isocalendar()[:2]):
        items = list(group)
        monday = items[0].date - timedelta(days=items[0].date.isoweekday() - 1)
        result.append(
            WeekSummary(
                iso_year=iso_year,
                iso_week=iso_week,
                monday=monday,
                soll=sum(r.soll for r in items),
                ist=sum(r.ist for r in items),
                ist_erfasst=sum(r.ist_erfasst for r in items),
                schultage=sum(1 for r in items if r.day_type == DayType.SCHULTAG),
            )
        )
    return result


def month_summary(ledger: Iterable[DayLedger]) -> list[MonthSummary]:
    rows = sorted(ledger, key=lambda r: r.date)
    result: list[MonthSummary] = []
    running = 0
    for (year, month), group in groupby(rows, key=lambda r: (r.date.year, r.date.month)):
        items = list(group)
        running += sum(r.delta for r in items)
        result.append(
            MonthSummary(
                year=year,
                month=month,
                soll=sum(r.soll for r in items),
                ist=sum(r.ist for r in items),
                ist_erfasst=sum(r.ist_erfasst for r in items),
                ferienarbeit=sum(r.ist_erfasst for r in items if is_ferien_row(r)),
                saldo_end=running,
            )
        )
    return result


def schulwochen_ueber(ledger: Iterable[DayLedger], threshold_h: float = 48) -> int:
    """Number of school weeks (≥ 1 school day) with recorded time above ``threshold_h``."""
    limit = threshold_h * 3600
    return sum(1 for w in week_summary(ledger) if w.is_schulwoche and w.ist_erfasst > limit)
