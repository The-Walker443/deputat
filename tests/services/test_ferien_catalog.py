"""Official Bavarian ferien (bundled catalogue ``config/ferien_bayern.yaml``, app/services/ferien_catalog.py) and
their automatic takeover in calendar_service (create_period, apply_ferien_catalog)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from app.core.calendar import bavarian_holidays, easter_sunday
from app.core.types import FerienKind
from app.db.models import CalendarDay, Ferien, MonthClose, Settings
from app.services import calendar_service as cal
from app.services import entries as entries_svc
from app.services import ferien_catalog as fc

ROOT = Path(__file__).resolve().parents[2]
CATALOG_FILE = ROOT / "config" / "ferien_bayern.yaml"
EXAMPLE_ICS = ROOT / "examples" / "ferien_bayern_BEISPIEL.ics"
SOURCE_URL = "https://www.verkuendung-bayern.de/baymbl/2022-747/"
COMPLETE_YEARS = ("2024/25", "2025/26", "2026/27", "2027/28", "2028/29", "2029/30")

# Months a block of each kind may start / end in (Bavarian Ferienordnung).
PLAUSIBLE_MONTHS = {
    FerienKind.HERBST: ({10, 11}, {10, 11}),
    FerienKind.WEIHNACHTEN: ({12}, {1}),
    FerienKind.WINTER: ({2, 3}, {2, 3}),
    FerienKind.OSTERN: ({3, 4}, {4}),
    FerienKind.PFINGSTEN: ({5, 6}, {5, 6}),
    FerienKind.SOMMER: ({7, 8}, {9}),
}


@pytest.fixture
def catalog() -> fc.Catalog:
    return fc.load_catalog(CATALOG_FILE)


def _raw() -> dict:
    return yaml.safe_load(CATALOG_FILE.read_text(encoding="utf-8"))


def _ferien(db, period) -> list[tuple[str, date, date, str]]:
    return [(f.name, f.start, f.end, f.source) for f in cal.list_ferien(db, period)]


# =========================================================================== dataset integrity


def test_header_names_the_official_source(catalog):
    assert catalog.bundesland == "BY"
    assert catalog.source_url == SOURCE_URL
    assert "BayMBl. 2022 Nr. 747" in catalog.source_title
    assert catalog.source_short == "Bayerisches Kultusministerium"
    assert catalog.retrieved == date(2026, 10, 30) and catalog.retrieved_de == "30.10.2026"
    assert catalog.note == "Amtliche Termine des Bayerischen Kultusministeriums; vor Gebrauch nicht verändert"


def test_coverage(catalog):
    cov = catalog.coverage()
    assert (cov.first, cov.last) == ("2024/25", "2029/30")
    assert cov.school_years == COMPLETE_YEARS
    assert cov.partial == ("2030/31",)
    assert cov.text == "2024/25 bis 2029/30"


def test_every_complete_school_year_has_all_six_blocks(catalog):
    for school_year in COMPLETE_YEARS:
        kinds = sorted(b.kind.value for b in catalog.blocks if b.school_year == school_year)
        assert kinds == sorted(k.value for k in fc.REQUIRED_KINDS), school_year


def test_blocks_lie_in_plausible_months(catalog):
    for b in catalog.blocks:
        start_months, end_months = PLAUSIBLE_MONTHS[b.kind]
        assert b.start.month in start_months, b
        assert b.end.month in end_months, b
        assert b.end >= b.start
        assert (b.end - b.start).days + 1 <= 50, b


def test_weekdays_are_sane(catalog):
    for b in catalog.blocks:
        assert b.start.isoweekday() <= 6, f"{b.name} beginnt an einem Sonntag"
        assert b.end.isoweekday() <= 5, f"{b.name} endet am Wochenende"


def test_no_overlaps(catalog):
    blocks = sorted(catalog.blocks, key=lambda b: b.start)
    for prev, cur in zip(blocks, blocks[1:], strict=False):
        assert cur.start > prev.end, (prev.name, cur.name)


def test_easter_and_pentecost_fall_into_the_right_blocks(catalog):
    for b in catalog.blocks:
        if b.kind is FerienKind.OSTERN:
            assert b.start <= easter_sunday(b.start.year) <= b.end, b.name
        if b.kind is FerienKind.PFINGSTEN:  # Bavaria: the day after Pfingstmontag
            assert b.start == easter_sunday(b.start.year) + timedelta(days=51), b.name


def test_buss_und_bettag_is_no_block_but_computed_by_the_core(catalog):
    assert not any("buß" in b.name.casefold() or "bettag" in b.name.casefold() for b in catalog.blocks)
    for year, day in ((2025, 19), (2026, 18), (2027, 17), (2028, 22), (2029, 21)):
        d = date(year, 11, day)
        assert bavarian_holidays(year)[d] == ("Buß- und Bettag", "unterrichtsfrei")
        assert not any(b.start <= d <= b.end for b in catalog.blocks)


def test_names_map_to_their_kind(catalog):
    from app.core.calendar import classify_ferien

    for b in catalog.blocks:
        assert classify_ferien(b.name) is b.kind


def test_yaml_matches_the_published_dates():
    """Spot checks against BayMBl. 2022 Nr. 747 (first and last day inclusive)."""
    blocks = {(b["school_year"], b["kind"]): (b["start"], b["end"]) for b in _raw()["blocks"]}
    assert blocks[("2026/27", "sommer")] == (date(2026, 8, 3), date(2026, 9, 14))
    assert blocks[("2026/27", "weihnachten")] == (date(2026, 12, 24), date(2027, 1, 8))
    assert blocks[("2027/28", "herbst")] == (date(2027, 11, 2), date(2027, 11, 5))
    assert blocks[("2028/29", "weihnachten")] == (date(2028, 12, 23), date(2029, 1, 5))  # Saturday, as printed
    assert blocks[("2030/31", "sommer")] == (date(2030, 7, 29), date(2030, 9, 9))


# =========================================================================== validation


def _valid_data() -> dict:
    return _raw()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["blocks"][1].update(start=d["blocks"][0]["end"]), "überschneidet"),
        (lambda d: d["blocks"][1].update(end=date(2024, 10, 1)), "vor dem Beginn"),
        (lambda d: d["blocks"][1].update(start="28.10.2024"), "kein Datum"),
        (lambda d: d["blocks"][1].update(kind="ostern"), "passt nicht zum Namen"),
        (lambda d: d["blocks"][1].update(kind="ferien"), "unbekannte Ferienart"),
        (lambda d: d["blocks"][1].update(school_year="2024/26"), "kein Schuljahr"),
        (lambda d: d["blocks"][1].update(school_year="2026/27"), "nicht im Schuljahr"),
        (lambda d: d["blocks"].pop(1), "unvollständig"),
        (lambda d: d.update(source_url="ftp://x"), "source_url"),
        (lambda d: d.pop("retrieved"), "retrieved"),
        (lambda d: d.update(blocks="x"), "blocks"),
    ],
)
def test_validation_rejects_broken_files(mutate, message):
    data = _valid_data()
    mutate(data)
    with pytest.raises(fc.FerienCatalogError, match=message):
        fc.validate(data)


def test_kind_follows_the_name_when_omitted():
    data = _valid_data()
    for b in data["blocks"]:
        b.pop("kind")
    catalog = fc.validate(data)
    assert {b.kind for b in catalog.blocks} == set(fc.REQUIRED_KINDS)


def test_partial_school_year_is_not_covered():
    data = _valid_data()
    data["partial_school_years"] = ["2030/31", "2029/30"]
    catalog = fc.validate(data)
    assert catalog.coverage().last == "2028/29"
    assert not catalog.covers(date(2029, 8, 1), date(2030, 7, 31))


# =========================================================================== coverage and clipping


@pytest.mark.parametrize(
    ("start", "end", "covered"),
    [
        (date(2023, 8, 1), date(2024, 7, 31), False),
        (date(2024, 8, 1), date(2025, 7, 31), True),
        (date(2029, 8, 1), date(2030, 7, 31), True),
        (date(2030, 8, 1), date(2031, 7, 31), False),  # only the summer holidays are fixed
        (date(2027, 1, 1), date(2027, 12, 31), True),  # calendar year: 2026/27 and 2027/28
        (date(2030, 1, 1), date(2030, 12, 31), False),  # reaches into 2030/31
        (date(2026, 9, 1), date(2027, 8, 31), True),  # periode_start_md 09-01
    ],
)
def test_covers(catalog, start, end, covered):
    assert catalog.covers(start, end) is covered


def test_blocks_are_clipped_at_the_period_boundary(catalog):
    p2728 = catalog.blocks_for_period(date(2027, 8, 1), date(2028, 7, 31))
    assert [(b.name, b.start, b.end) for b in p2728][0] == ("Sommerferien 2027", date(2027, 8, 2), date(2027, 9, 13))
    last = p2728[-1]
    assert (last.name, last.start, last.end, last.clipped) == (
        "Sommerferien 2028", date(2028, 7, 31), date(2028, 7, 31), True)
    assert (last.full_start, last.full_end) == (date(2028, 7, 31), date(2028, 9, 11))
    assert len(p2728) == 7

    p2829 = catalog.blocks_for_period(date(2028, 8, 1), date(2029, 7, 31))
    first = p2829[0]
    assert (first.name, first.start, first.end, first.clipped) == (
        "Sommerferien 2028", date(2028, 8, 1), date(2028, 9, 11), True)
    assert (p2829[-1].name, p2829[-1].start, p2829[-1].end) == ("Sommerferien 2029", date(2029, 7, 30),
                                                                date(2029, 7, 31))

    p2627 = catalog.blocks_for_period(date(2026, 8, 1), date(2027, 7, 31))
    assert [b.kind.value for b in p2627] == ["sommer", "herbst", "weihnachten", "winter", "ostern", "pfingsten"]
    assert not any(b.clipped for b in p2627)


def test_only_bavaria(catalog):
    assert fc.get_catalog("BW") is None
    assert fc.blocks_for_period(date(2026, 8, 1), date(2027, 7, 31), "BW") == []
    assert fc.coverage("BW").first is None
    assert not fc.covers_period(date(2026, 8, 1), date(2027, 7, 31), "NW")
    assert fc.covers_period(date(2026, 8, 1), date(2027, 7, 31), "BY")


# =========================================================================== env override


def test_env_override_file(tmp_path, monkeypatch, db):
    data = _valid_data()
    for b in data["blocks"]:
        if b["school_year"] == "2026/27" and b["kind"] == "herbst":
            b["end"] = date(2026, 11, 5)
    data["retrieved"] = date(2027, 1, 15)
    custom = tmp_path / "ferien_bayern.yaml"
    custom.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("FERIEN_CATALOG_FILE", str(custom))
    assert fc.catalog_path() == custom
    assert fc.load_catalog().retrieved == date(2027, 1, 15)

    period = cal.create_period(db, "2026/27")
    herbst = next(f for f in cal.list_ferien(db, period) if f.kind == "herbst")
    assert (herbst.start, herbst.end) == (date(2026, 11, 2), date(2026, 11, 5))


def test_env_override_relative_path(monkeypatch):
    monkeypatch.setenv("FERIEN_CATALOG_FILE", "config/ferien_bayern.yaml")
    assert fc.catalog_path() == CATALOG_FILE
    monkeypatch.delenv("FERIEN_CATALOG_FILE")
    assert fc.catalog_path() == fc.DEFAULT_FILE == CATALOG_FILE


def test_broken_override_file_means_no_catalogue(tmp_path, monkeypatch, db):
    broken = tmp_path / "kaputt.yaml"
    broken.write_text("blocks: [", encoding="utf-8")
    monkeypatch.setenv("FERIEN_CATALOG_FILE", str(broken))
    assert fc.get_catalog() is None
    period = cal.create_period(db, "2026/27")  # still works, just without ferien
    assert cal.list_ferien(db, period) == []
    status = cal.ferien_catalog_status(db, period)
    assert not status.available and status.hint == cal.CATALOG_UNAVAILABLE_HINT


def test_failed_load_is_cached_and_logged_once_per_change(tmp_path, monkeypatch):
    import os

    data = _valid_data()
    data.pop("source_url")
    broken = tmp_path / "kaputt.yaml"
    broken.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("FERIEN_CATALOG_FILE", str(broken))
    validated: list[str] = []
    real_validate = fc.validate
    monkeypatch.setattr(fc, "validate", lambda d, p="": validated.append(p) or real_validate(d, p))
    logged: list[str] = []
    monkeypatch.setattr(fc.log, "error", lambda msg, *args: logged.append(msg % args))

    def touch(seconds: int) -> None:
        st = broken.stat()
        os.utime(broken, (st.st_atime, st.st_mtime + seconds))

    for _ in range(3):
        assert fc.get_catalog() is None
        with pytest.raises(fc.FerienCatalogError, match="source_url fehlt"):
            fc.load_catalog()
    assert validated == [str(broken)]  # read and validated once
    assert logged == ["Ferienkatalog nicht verfügbar: source_url fehlt."]

    data["source_url"] = "ftp://x"  # still broken, but changed: read and logged again
    broken.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    touch(10)
    assert fc.get_catalog() is None and fc.get_catalog() is None
    assert len(validated) == 2 and len(logged) == 2 and "source_url muss mit http://" in logged[1]

    data["source_url"] = SOURCE_URL  # fixed
    broken.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    touch(20)
    assert fc.get_catalog() is not None and fc.get_catalog() is not None
    assert len(validated) == 3 and len(logged) == 2

    broken.unlink()  # missing file: logged once as well
    assert fc.get_catalog() is None and fc.get_catalog() is None
    assert logged[2:] == [f"Ferienkatalog nicht verfügbar: Ferienkatalog nicht gefunden: {broken}"]


# =========================================================================== takeover on period creation


def test_new_period_gets_the_official_ferien(db):
    period = cal.create_period(db, "2026/27")
    rows = cal.list_ferien(db, period)
    assert [(f.name, f.start, f.end) for f in rows] == [
        ("Sommerferien 2026", date(2026, 8, 3), date(2026, 9, 14)),
        ("Herbstferien 2026", date(2026, 11, 2), date(2026, 11, 6)),
        ("Weihnachtsferien 2026/27", date(2026, 12, 24), date(2027, 1, 8)),
        ("Frühjahrsferien 2027", date(2027, 2, 8), date(2027, 2, 12)),
        ("Osterferien 2027", date(2027, 3, 22), date(2027, 4, 2)),
        ("Pfingstferien 2027", date(2027, 5, 18), date(2027, 5, 28)),
    ]
    assert {(f.source, f.quelle_url) for f in rows} == {("katalog", SOURCE_URL)}
    assert db.get(CalendarDay, date(2026, 11, 3)).day_type == "ferientag"
    assert db.get(CalendarDay, date(2026, 11, 18)).day_type == "unterrichtsfrei"  # Buß- und Bettag, full soll
    assert db.get(CalendarDay, date(2026, 11, 18)).soll_seconds > 0
    assert db.get(CalendarDay, date(2026, 10, 30)).day_type == "schultag"

    status = cal.ferien_catalog_status(db, period)
    assert status.covered and status.in_sync and status.matches and status.offer is None
    assert status.katalog_count == 6 and not status.can_replace


def test_create_period_without_catalogue(db):
    period = cal.create_period(db, "2026/27", apply_catalog=False)
    assert cal.list_ferien(db, period) == []
    assert cal.ferien_catalog_status(db, period).offer == "apply"


def test_summer_holidays_split_between_neighbouring_periods(db):
    p2728 = cal.create_period(db, "2027/28")
    p2829 = cal.create_period(db, "2028/29")
    tail = cal.list_ferien(db, p2728)[-1]
    head = cal.list_ferien(db, p2829)[0]
    assert (tail.name, tail.start, tail.end) == ("Sommerferien 2028", date(2028, 7, 31), date(2028, 7, 31))
    assert (head.name, head.start, head.end) == ("Sommerferien 2028", date(2028, 8, 1), date(2028, 9, 11))
    assert db.get(CalendarDay, date(2028, 7, 31)).ferien_id == tail.id
    assert db.get(CalendarDay, date(2028, 8, 1)).ferien_id == head.id
    assert db.get(CalendarDay, date(2028, 7, 31)).period_id == p2728.id


def test_calendar_year_period_gets_blocks_of_two_school_years(db):
    period = cal.create_period(db, "2027")
    names = [f.name for f in cal.list_ferien(db, period)]
    assert names == ["Weihnachtsferien 2026/27", "Frühjahrsferien 2027", "Osterferien 2027", "Pfingstferien 2027",
                     "Sommerferien 2027", "Herbstferien 2027", "Weihnachtsferien 2027/28"]
    assert cal.list_ferien(db, period)[0].start == date(2027, 1, 1)  # clipped
    assert cal.list_ferien(db, period)[-1].end == date(2027, 12, 31)


def test_uncovered_school_year_gets_no_ferien_and_a_hint(db):
    period = cal.create_period(db, "2023/24")
    assert cal.list_ferien(db, period) == []
    status = cal.ferien_catalog_status(db, period)
    assert status.available and not status.covered and status.offer is None
    assert status.hint == (
        "Für dieses Schuljahr sind keine amtlichen Termine hinterlegt – bitte per ICS importieren oder manuell erfassen."
    )
    with pytest.raises(cal.CalendarError, match="keine amtlichen Termine"):
        cal.apply_ferien_catalog(db, period)
    # the fallbacks stay available
    cal.create_ferien(db, period, name="Herbstferien 2023", start=date(2023, 10, 30), end=date(2023, 11, 3))
    assert len(cal.list_ferien(db, period)) == 1


def test_other_bundesland_gets_nothing(db):
    db.get(Settings, 1).bundesland = "BW"
    db.commit()
    period = cal.create_period(db, "2026/27")
    assert cal.list_ferien(db, period) == []
    status = cal.ferien_catalog_status(db, period)
    assert not status.available and status.hint == cal.CATALOG_BUNDESLAND_HINT and status.offer is None
    with pytest.raises(cal.CalendarError, match="nur für Bayern"):
        cal.apply_ferien_catalog(db, period)


# =========================================================================== apply / replace


def test_apply_is_idempotent(db):
    period = cal.create_period(db, "2026/27", apply_catalog=False)
    first = cal.apply_ferien_catalog(db, period)
    assert len(first.created) == 6 and not first.unchanged and first.replaced == []
    ids = [f.id for f in cal.list_ferien(db, period)]
    again = cal.apply_ferien_catalog(db, period)
    assert again.unchanged and again.created == [] and again.message == "Die amtlichen Ferien sind bereits übernommen."
    again = cal.apply_ferien_catalog(db, period, replace=True)
    assert again.unchanged
    assert [f.id for f in cal.list_ferien(db, period)] == ids


def test_apply_refuses_to_touch_other_ferien_without_replace(db):
    period = cal.create_period(db, "2026/27", apply_catalog=False)
    cal.create_ferien(db, period, name="Herbstferien", start=date(2026, 10, 26), end=date(2026, 10, 30))
    with pytest.raises(cal.CalendarConflict, match="bitte bestätigen"):
        cal.apply_ferien_catalog(db, period)
    assert _ferien(db, period) == [("Herbstferien", date(2026, 10, 26), date(2026, 10, 30), "manuell")]
    assert cal.ferien_catalog_status(db, period).can_replace


def test_replace_example_ferien_keeps_matching_rows(db):
    period = cal.create_period(db, "2026/27", apply_catalog=False)
    cal.commit_ics(db, period, EXAMPLE_ICS.read_bytes())
    cal.commit_ics(db, period, EXAMPLE_ICS.read_bytes(), selected=[2])  # also the "Buß- und Bettag" event
    example = {(f.start, f.end): f.id for f in cal.list_ferien(db, period)}
    assert any("Buß" in f.name for f in cal.list_ferien(db, period))
    herbst_id = example[(date(2026, 11, 2), date(2026, 11, 6))]
    settings = db.get(Settings, 1)
    settings.ferienarbeit_schaetzung = {herbst_id: 6.0}
    db.commit()

    status = cal.ferien_catalog_status(db, period)
    assert status.has_example and status.offer == "replace" and not status.in_sync

    result = cal.apply_ferien_catalog(db, period, replace=True)
    assert result.replaced == ["BEISPIEL – Buß- und Bettag"]
    assert len(result.updated) == 6 and result.created == []
    rows = cal.list_ferien(db, period)
    assert {f.source for f in rows} == {"katalog"} and not any("BEISPIEL" in f.name for f in rows)
    assert herbst_id in {f.id for f in rows}  # same dates: row kept, estimate survives
    assert db.get(Settings, 1).ferienarbeit_schaetzung == {herbst_id: 6.0}
    assert db.get(CalendarDay, date(2026, 11, 18)).day_type == "unterrichtsfrei"
    assert cal.ferien_catalog_status(db, period).offer is None


def _close(db, ym: str) -> None:
    db.add(MonthClose(year_month=ym, closed_at=datetime(2026, 11, 2, 10),
                      entries_hash_sha256=entries_svc.month_entries_hash(db, ym),
                      soll_seconds=0, ist_seconds=0, saldo_seconds=0))
    db.commit()


def test_replace_touching_a_closed_month_needs_a_reason(db):
    period = cal.create_period(db, "2026/27", apply_catalog=False)
    cal.create_ferien(db, period, name="Herbstferien", start=date(2026, 10, 26), end=date(2026, 10, 30))
    _close(db, "2026-10")
    before = _ferien(db, period)
    assert cal.ferien_catalog_closed_months(db, period) == ["2026-10"]  # dry run: the UI asks for a reason

    with pytest.raises(cal.CalendarLocked, match="2026-10"):
        cal.apply_ferien_catalog(db, period, replace=True)
    assert _ferien(db, period) == before
    assert db.get(MonthClose, "2026-10").status == "abgeschlossen"
    assert db.get(CalendarDay, date(2026, 10, 27)).day_type == "ferientag"

    result = cal.apply_ferien_catalog(db, period, replace=True, reason="Amtliche Termine statt eigener Eingabe")
    assert result.changed_months == ["2026-10"] and result.replaced == ["Herbstferien"]
    mc = db.get(MonthClose, "2026-10")
    assert mc.status == "nachtraeglich_korrigiert"
    assert mc.hash_history[-1]["reason"] == "Kalenderänderung: Amtliche Termine statt eigener Eingabe"
    assert db.get(CalendarDay, date(2026, 10, 27)).day_type == "schultag"
    assert len(cal.list_ferien(db, period)) == 6


def test_replace_outside_closed_months_needs_no_reason(db):
    period = cal.create_period(db, "2026/27", apply_catalog=False)
    cal.create_ferien(db, period, name="Pfingstferien", start=date(2027, 5, 24), end=date(2027, 5, 28))
    _close(db, "2026-10")  # October holds no official ferien: its days do not change
    assert cal.ferien_catalog_closed_months(db, period) == []  # dry run: no reason needed
    result = cal.apply_ferien_catalog(db, period, replace=True)
    assert result.changed_months == [] and result.replaced == ["Pfingstferien"]
    assert db.get(MonthClose, "2026-10").status == "abgeschlossen"


def test_apply_without_ferien_into_a_closed_month_needs_a_reason(db):
    # A month can be closed while the period has no ferien yet; taking them over then changes it.
    period = cal.create_period(db, "2026/27", apply_catalog=False)
    _close(db, "2026-11")  # Herbstferien 02.–06.11.2026
    assert cal.ferien_catalog_status(db, period).offer == "apply"
    assert cal.ferien_catalog_closed_months(db, period) == ["2026-11"]

    with pytest.raises(cal.CalendarLocked, match="2026-11"):
        cal.apply_ferien_catalog(db, period)
    assert cal.list_ferien(db, period) == []
    assert db.get(MonthClose, "2026-11").status == "abgeschlossen"

    result = cal.apply_ferien_catalog(db, period, reason="Amtliche Ferien nachgetragen")
    assert result.changed_months == ["2026-11"] and len(result.created) == 6
    mc = db.get(MonthClose, "2026-11")
    assert mc.status == "nachtraeglich_korrigiert"
    assert mc.hash_history[-1]["reason"] == "Kalenderänderung: Amtliche Ferien nachgetragen"
    assert db.get(CalendarDay, date(2026, 11, 3)).day_type == "ferientag"
    assert cal.ferien_catalog_closed_months(db, period) == []  # nothing left to take over


def test_dry_run_finds_closed_months_outside_the_period(db):
    # An ICS row of 2027/28 reaches into 2028/29; replacing it changes August 2028 (a month of the next period).
    p2728 = cal.create_period(db, "2027/28", apply_catalog=False)
    cal.create_period(db, "2028/29", apply_catalog=False)
    cal.create_ferien(db, p2728, name="Sommerferien 2028", start=date(2028, 7, 31), end=date(2028, 9, 11),
                      source="ics")
    assert db.get(CalendarDay, date(2028, 8, 16)).day_type == "ferientag"
    _close(db, "2028-08")
    assert cal.ferien_catalog_closed_months(db, p2728) == ["2028-08"]
    assert db.get(Ferien, cal.list_ferien(db, p2728)[0].id).end == date(2028, 9, 11)  # dry run wrote nothing

    with pytest.raises(cal.CalendarLocked, match="2028-08"):
        cal.apply_ferien_catalog(db, p2728, replace=True)
    result = cal.apply_ferien_catalog(db, p2728, replace=True, reason="Amtliche Termine")
    assert result.changed_months == ["2028-08"] and result.replaced == ["Sommerferien 2028"]
    assert db.get(CalendarDay, date(2028, 8, 16)).day_type == "schultag"
    assert db.get(MonthClose, "2028-08").status == "nachtraeglich_korrigiert"


def test_dry_run_without_closed_months_or_catalogue(db):
    period = cal.create_period(db, "2026/27", apply_catalog=False)
    assert cal.ferien_catalog_closed_months(db, period) == []  # no closed month at all
    _close(db, "2026-11")
    db.get(Settings, 1).bundesland = "BW"
    db.commit()
    assert cal.ferien_catalog_closed_months(db, period) == []  # nothing can be taken over


@pytest.mark.parametrize(
    "change",
    [
        {"name": "Herbstferien 2026 (umbenannt)"},
        {"kind": "sonstige"},
        {"end": date(2026, 11, 5)},
        {"start": date(2026, 11, 3)},
    ],
)
def test_editing_an_official_row_drops_the_badge(db, change):
    period = cal.create_period(db, "2026/27")
    herbst = next(f for f in cal.list_ferien(db, period) if f.kind == "herbst")
    # Saving unchanged values (as the edit form does) keeps the official row.
    cal.update_ferien(db, herbst.id, name=herbst.name, kind=herbst.kind, start=herbst.start, end=herbst.end,
                      quelle_url=herbst.quelle_url)
    assert db.get(Ferien, herbst.id).source == "katalog"
    assert cal.ferien_catalog_status(db, period).in_sync

    cal.update_ferien(db, herbst.id, **change)
    assert db.get(Ferien, herbst.id).source == "manuell"
    status = cal.ferien_catalog_status(db, period)
    assert not status.in_sync and status.can_replace and status.offer is None
    cal.apply_ferien_catalog(db, period, replace=True)
    assert db.scalars(select(Ferien).where(Ferien.period_id == period.id, Ferien.source != "katalog")).all() == []
    assert cal.ferien_catalog_status(db, period).in_sync
