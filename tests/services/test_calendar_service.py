"""calendar_service: periods, ferien CRUD, ICS import, holidays, overrides, leave, rematerialize."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.types import DayType, FerienKind, LeaveMode
from app.db.models import CalendarDay, DayOverride, Ferien, Holiday
from app.services import calendar_service as cal
from app.services import settings_service as settings_svc

H = 3600
EXAMPLE_ICS = Path(__file__).resolve().parents[2] / "examples" / "ferien_bayern_BEISPIEL.ics"


@pytest.fixture
def ics_bytes() -> bytes:
    return EXAMPLE_ICS.read_bytes()


@pytest.fixture
def year(db, ics_bytes):
    """School year 2026/27 with the example ferien imported."""
    period = cal.create_period(db, "2026/27")
    cal.commit_ics(db, period, ics_bytes)
    return period


def _ics(*events: tuple[str, str, str | None]) -> str:
    """Build a small ICS; each event is (summary, dtstart-line-value, dtend-line-value)."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//DE"]
    for i, (summary, start, end) in enumerate(events):
        lines += ["BEGIN:VEVENT", f"UID:t{i}@test", "DTSTAMP:20261001T000000Z", f"SUMMARY:{summary}"]
        lines.append(f"DTSTART{start}")
        if end is not None:
            lines.append(f"DTEND{end}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def _day(db, d: date) -> CalendarDay:
    row = db.get(CalendarDay, d)
    assert row is not None, d
    return row


def _snapshot(db) -> list[tuple]:
    return [
        (r.date, r.weekday, r.day_type, r.absence_type, r.ferien_id, r.holiday_name, r.soll_seconds, r.note, r.period_id)
        for r in db.scalars(select(CalendarDay).order_by(CalendarDay.date))
    ]


# =========================================================================== periods


def test_create_school_year_from_label(db):
    period = cal.create_period(db, "2026/27")
    assert (period.start, period.end, period.kind) == (date(2026, 8, 1), date(2027, 7, 31), "schuljahr")
    assert db.scalar(select(func.count()).select_from(CalendarDay)) == 365
    assert _day(db, date(2026, 8, 1)).day_type == DayType.WOCHENENDE


def test_create_calendar_year_and_custom_period(db):
    p = cal.create_period(db, "2030")
    assert (p.start, p.end, p.kind) == (date(2030, 1, 1), date(2030, 12, 31), "kalenderjahr")
    custom = cal.create_period(db, start=date(2031, 2, 1), end=date(2031, 7, 31), kind="schuljahr")
    assert custom.label == "2031/32"


def test_period_label_respects_periode_start_md(db):
    settings_svc.update_settings(db, periode_start_md="09-01")
    p = cal.create_period(db, "2026/27")
    assert (p.start, p.end) == (date(2026, 9, 1), date(2027, 8, 31))


@pytest.mark.parametrize("label", ["2026/28", "26/27", "Schuljahr", "2026-27"])
def test_invalid_period_label(db, label):
    with pytest.raises(cal.CalendarError):
        cal.create_period(db, label)


def test_overlapping_period_rejected(db):
    cal.create_period(db, "2026/27")
    with pytest.raises(cal.CalendarError, match="überschneidet"):
        cal.create_period(db, "2027")


def test_period_without_label_uses_today(db):
    p = cal.create_period(db, today=date(2027, 3, 1))
    assert p.label == "2026/27"
    assert cal.suggest_period_label(db, date(2027, 8, 2)) == "2027/28"


def test_resolve_period_and_no_period(db):
    with pytest.raises(cal.NoPeriodError) as exc:
        cal.resolve_period(db, None, date(2026, 10, 1))
    assert "Bitte zuerst ein Schuljahr anlegen" in str(exc.value)
    assert cal.get_current_period(db, date(2026, 10, 1)) is None

    p1 = cal.create_period(db, "2025/26")
    p2 = cal.create_period(db, "2026/27")
    assert cal.resolve_period(db, None, date(2026, 10, 1)).id == p2.id
    assert cal.resolve_period(db, "2025/26").id == p1.id
    assert cal.resolve_period(db, p1.id).id == p1.id
    assert cal.resolve_period(db, "current", date(2025, 9, 1)).id == p1.id
    # Outside every period: latest started, else the earliest.
    assert cal.resolve_period(db, None, date(2030, 1, 1)).id == p2.id
    assert cal.resolve_period(db, None, date(2020, 1, 1)).id == p1.id
    with pytest.raises(cal.NotFoundError):
        cal.resolve_period(db, "1999/00")


def test_delete_period_removes_days_and_ferien(db, year):
    cal.delete_period(db, year.id)
    assert db.scalar(select(func.count()).select_from(CalendarDay)) == 0
    assert db.scalar(select(func.count()).select_from(Ferien)) == 0


# =========================================================================== holidays / days


def test_holidays_materialised_with_source_berechnet(db, year):
    rows = {h.date: h for h in cal.list_holidays(db, year)}
    assert rows[date(2026, 12, 25)].name == "1. Weihnachtstag"
    assert rows[date(2026, 8, 15)].scope == "regional"  # Mariä Himmelfahrt (default on)
    assert rows[date(2027, 3, 26)].name == "Karfreitag"
    assert rows[date(2026, 11, 18)].scope == "unterrichtsfrei"
    assert {h.source for h in rows.values()} == {"berechnet"}


def test_buss_und_bettag_unterrichtsfrei_with_full_soll(db, year):
    row = _day(db, date(2026, 11, 18))
    assert row.day_type == DayType.UNTERRICHTSFREI
    assert row.holiday_name == "Buß- und Bettag"
    assert row.soll_seconds == 8 * H
    assert row.absence_type is None


def test_day_types_and_soll_of_example_year(db, year):
    assert _day(db, date(2026, 10, 14)).day_type == DayType.SCHULTAG
    assert _day(db, date(2026, 10, 14)).soll_seconds == 8 * H
    assert _day(db, date(2026, 12, 25)).day_type == DayType.FEIERTAG
    assert _day(db, date(2026, 12, 25)).soll_seconds == 0
    herbst = _day(db, date(2026, 11, 4))
    assert herbst.day_type == DayType.FERIENTAG and herbst.ferien_id and herbst.soll_seconds == 8 * H
    # Weekend inside a ferien block keeps the ferien id (Ferienarbeit attribution).
    assert _day(db, date(2026, 11, 7)).ferien_id is None  # day after Herbstferien
    assert _day(db, date(2026, 12, 26)).ferien_id is not None


def test_j_soll_plausible_for_full_time_school_year(db, year):
    total = db.scalar(select(func.sum(CalendarDay.soll_seconds)).where(CalendarDay.period_id == year.id))
    # 260 weekdays − 8 weekday holidays − 30 leave days = 222 days × 8 h (2026/27: five holidays on weekends)
    assert total == 222 * 8 * H
    assert 1750 * H <= total <= 1780 * H


def test_leave_assigned_in_summer(db, year):
    leave = [r.date for r in db.scalars(select(CalendarDay).where(CalendarDay.absence_type == "urlaub"))]
    assert len(leave) == 30
    assert min(leave) == date(2026, 8, 3) and max(leave) == date(2026, 9, 11)
    assert all(_day(db, d).soll_seconds == 0 for d in leave)
    assert _day(db, date(2026, 9, 14)).absence_type is None  # 31st summer weekday stays a ferien day
    info = cal.leave_plan_info(db, year)
    assert info.is_complete and info.assigned == 30 and info.noch_zuzuordnen == 0
    summer = next(f for f in info.per_ferien if f.kind == FerienKind.SOMMER)
    assert (summer.kandidaten, summer.urlaubstage) == (31, 30)
    assert info.ausserhalb_ferien == 0


def test_proportional_mode_distributes_over_blocks(db, year):
    settings_svc.update_settings(db, urlaubs_modus="proportional")
    info = cal.leave_plan_info(db, year)
    assert info.assigned == 30
    per = {f.kind: f.urlaubstage for f in info.per_ferien}
    assert per["sommer"] < 30 and sum(per.values()) == 30
    assert sum(1 for r in db.scalars(select(CalendarDay).where(CalendarDay.absence_type == "urlaub"))) == 30
    assert _day(db, date(2026, 11, 2)).absence_type == "urlaub"  # first Herbst day


def test_rematerialize_idempotent(db, year):
    before = _snapshot(db)
    holidays_before = [(h.date, h.name, h.scope) for h in db.scalars(select(Holiday).order_by(Holiday.date))]
    cal.rematerialize(db, year)
    cal.rematerialize(db, year)
    assert _snapshot(db) == before
    assert [(h.date, h.name, h.scope) for h in db.scalars(select(Holiday).order_by(Holiday.date))] == holidays_before


def test_regional_holiday_toggle(db):
    p = cal.create_period(db, "2025/26")  # 15.08.2025 is a Friday
    assert _day(db, date(2025, 8, 15)).day_type == DayType.FEIERTAG
    assert _day(db, date(2025, 8, 15)).soll_seconds == 0

    settings_svc.update_settings(db, regionale_feiertage=[])
    assert db.get(Holiday, date(2025, 8, 15)) is None
    assert _day(db, date(2025, 8, 15)).day_type == DayType.SCHULTAG
    assert _day(db, date(2025, 8, 15)).soll_seconds == 8 * H

    settings_svc.update_settings(db, regionale_feiertage=["mariae_himmelfahrt", "friedensfest"])
    assert db.get(Holiday, date(2025, 8, 8)).name == "Augsburger Hohes Friedensfest"
    assert _day(db, date(2025, 8, 8)).soll_seconds == 0
    assert _day(db, date(2025, 8, 15)).soll_seconds == 0
    assert cal.list_holidays(db, p)[0].date == date(2025, 8, 8)


def test_manual_holiday_row_survives_sync(db, year):
    db.add(Holiday(date=date(2026, 10, 14), name="Schulfest (manuell)", scope="gesetzlich", source="manuell"))
    db.commit()
    cal.rematerialize(db, year)
    assert db.get(Holiday, date(2026, 10, 14)).source == "manuell"
    assert _day(db, date(2026, 10, 14)).day_type == DayType.FEIERTAG


def test_part_time_soll_and_reduction_add_back(db, year):
    settings_svc.update_settings(db, upz_ist=21)
    assert _day(db, date(2026, 10, 14)).soll_seconds == 6 * H
    # Altersermäßigung is already deducted from upz_ist and must not lower the soll (Konzept §6.1).
    settings_svc.update_settings(db, upz_ist=26, altersermaessigung=2)
    assert _day(db, date(2026, 10, 14)).soll_seconds == 8 * H


# =========================================================================== ferien CRUD


def test_create_update_delete_ferien(db):
    p = cal.create_period(db, "2026/27")
    f = cal.create_ferien(db, p, name="Herbstferien", start=date(2026, 11, 2), end=date(2026, 11, 6))
    assert f.kind == "herbst" and f.source == "manuell"
    assert _day(db, date(2026, 11, 3)).day_type == DayType.FERIENTAG

    f = cal.update_ferien(db, f.id, name="Weihnachtsferien", start=date(2026, 12, 23), end=date(2027, 1, 5))
    assert f.kind == "weihnachten"  # follows the name
    assert _day(db, date(2026, 11, 3)).day_type == DayType.SCHULTAG
    assert _day(db, date(2026, 12, 23)).ferien_id == f.id

    f = cal.update_ferien(db, f.id, kind="sonstige")
    assert f.kind == "sonstige"
    f = cal.update_ferien(db, f.id, name="Winterpause")
    assert f.kind == "sonstige"  # explicit kind is kept

    settings_svc.set_ferienarbeit_schaetzung(db, {f.id: 5})
    cal.delete_ferien(db, f.id)
    assert _day(db, date(2026, 12, 23)).day_type == DayType.SCHULTAG
    assert _day(db, date(2026, 12, 23)).ferien_id is None
    assert f.id not in settings_svc.get_settings(db).ferienarbeit_schaetzung


@pytest.mark.parametrize(
    ("name", "start", "end", "message"),
    [
        ("", date(2026, 11, 2), date(2026, 11, 6), "Namen"),
        ("Herbst", date(2026, 11, 6), date(2026, 11, 2), "vor dem Beginn"),
        ("Herbst", date(2028, 11, 2), date(2028, 11, 6), "außerhalb"),
        ("Lang", date(2026, 9, 1), date(2027, 3, 1), "höchstens"),
        ("Überlappung", date(2026, 11, 3), date(2026, 11, 10), "Überschneidung"),
        ("Herbst", date(2026, 11, 2), date(2026, 11, 6), None),
    ],
)
def test_ferien_validation(db, name, start, end, message):
    p = cal.create_period(db, "2026/27")
    cal.create_ferien(db, p, name="Herbstferien", start=date(2026, 11, 2), end=date(2026, 11, 3))
    if message is None:
        with pytest.raises(cal.CalendarError, match="Überschneidung"):
            cal.create_ferien(db, p, name=name, start=start, end=end)
        return
    with pytest.raises(cal.CalendarError, match=message):
        cal.create_ferien(db, p, name=name, start=start, end=end)


def test_invalid_ferien_kind(db):
    p = cal.create_period(db, "2026/27")
    with pytest.raises(cal.CalendarError, match="Ferienart"):
        cal.create_ferien(db, p, name="X", start=date(2026, 11, 2), end=date(2026, 11, 3), kind="karneval")


def test_ferien_crossing_period_end_used_by_both_periods(db):
    p1 = cal.create_period(db, "2026/27")
    p2 = cal.create_period(db, "2027/28")
    f = cal.create_ferien(db, p2, name="Sommerferien 2027", start=date(2027, 7, 29), end=date(2027, 9, 10))
    assert _day(db, date(2027, 7, 29)).ferien_id == f.id
    assert _day(db, date(2027, 7, 29)).period_id == p1.id
    assert _day(db, date(2027, 7, 29)).day_type == DayType.FERIENTAG


# =========================================================================== ICS


def test_parse_example_ics(ics_bytes):
    events, warnings = cal.parse_ics(ics_bytes)
    assert warnings == []
    assert all(e.name.startswith("BEISPIEL – ") for e in events)
    summer = events[0]
    assert (summer.start, summer.end) == (date(2026, 8, 3), date(2026, 9, 14))  # DTEND exclusive


def test_parse_ics_variants():
    content = _ics(
        ("Ein Tag ohne Ende", ";VALUE=DATE:20261118", None),
        ("Mit Uhrzeit", ":20261102T000000", ":20261107T000000"),
        ("UTC-Zeit", ":20261223T230000Z", ":20270105T230000Z"),
    ) .replace("END:VCALENDAR", "BEGIN:VEVENT\r\nUID:r@test\r\nSUMMARY:Serie\r\nDTSTART;VALUE=DATE:20261001\r\n"
               "RRULE:FREQ=WEEKLY\r\nEND:VEVENT\r\nEND:VCALENDAR")
    events, warnings = cal.parse_ics(content)
    by_name = {e.name: e for e in events}
    assert (by_name["Ein Tag ohne Ende"].start, by_name["Ein Tag ohne Ende"].end) == (date(2026, 11, 18),) * 2
    assert (by_name["Mit Uhrzeit"].start, by_name["Mit Uhrzeit"].end) == (date(2026, 11, 2), date(2026, 11, 6))
    # 23:00 UTC = midnight in Berlin (winter): exclusive end.
    assert (by_name["UTC-Zeit"].start, by_name["UTC-Zeit"].end) == (date(2026, 12, 24), date(2027, 1, 5))
    assert "Serie" not in by_name and any("Serientermin" in w for w in warnings)


@pytest.mark.parametrize("content", ["", "kein ics", "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"])
def test_parse_ics_errors(content):
    with pytest.raises(cal.CalendarError):
        cal.parse_ics(content)


def test_ics_preview_statuses(db, ics_bytes):
    p = cal.create_period(db, "2026/27")
    preview = cal.preview_ics(db, p, ics_bytes)
    status = {i.name.removeprefix("BEISPIEL – "): i.status for i in preview.items}
    assert status["Sommerferien 2026"] == "neu"
    assert status["Buß- und Bettag"] == "feiertag"
    assert status["Sommerferien 2027"] == "ausserhalb"
    assert preview.importable_count == 6
    summer = preview.items[0]
    assert summer.kind == "sommer" and summer.werktage == 31 and summer.tage == 43
    assert db.scalar(select(func.count()).select_from(Ferien)) == 0  # preview writes nothing


def test_ics_commit_and_reimport(db, ics_bytes):
    p = cal.create_period(db, "2026/27")
    result = cal.commit_ics(db, p, ics_bytes, quelle_url="https://example.invalid/ferien.ics")
    assert len(result.created) == 6
    assert {f.source for f in result.created} == {"ics"}
    assert {f.kind for f in result.created} == {"sommer", "herbst", "weihnachten", "winter", "ostern", "pfingsten"}
    assert all(f.quelle_url == "https://example.invalid/ferien.ics" for f in result.created)

    again = cal.preview_ics(db, p, ics_bytes)
    assert {i.status for i in again.items} == {"vorhanden", "feiertag", "ausserhalb"}
    assert cal.commit_ics(db, p, ics_bytes).created == []


def test_ics_explicit_selection_and_replace(db):
    p = cal.create_period(db, "2026/27")
    cal.create_ferien(db, p, name="Herbstferien", start=date(2026, 11, 2), end=date(2026, 11, 4))
    content = _ics(
        ("Herbstferien", ";VALUE=DATE:20261102", ";VALUE=DATE:20261107"),
        ("Buß- und Bettag", ";VALUE=DATE:20261118", ";VALUE=DATE:20261119"),
    )
    preview = cal.preview_ics(db, p, content)
    assert [i.status for i in preview.items] == ["ueberschneidung", "feiertag"]
    assert not preview.items[0].importierbar

    # Without replace the conflicting block is skipped; the 'feiertag' item can be selected explicitly.
    result = cal.commit_ics(db, p, content, selected=[0, 1])
    assert [f.name for f in result.created] == ["Buß- und Bettag"]
    assert result.skipped and result.skipped[0][0] == 0
    assert _day(db, date(2026, 11, 18)).day_type == DayType.FERIENTAG

    result = cal.commit_ics(db, p, content, replace=True)
    assert [f.name for f in result.created] == ["Herbstferien"]
    assert result.replaced == ["Herbstferien"]
    assert _day(db, date(2026, 11, 6)).day_type == DayType.FERIENTAG

    with pytest.raises(cal.CalendarError, match="Unbekannte Auswahl"):
        cal.commit_ics(db, p, content, selected=[7])


# =========================================================================== overrides


def test_overrides_change_soll(db, year):
    d = date(2026, 10, 14)
    cal.set_override(db, d, absence_type="dienstbefreiung", note="Personalversammlung")
    assert (_day(db, d).absence_type, _day(db, d).soll_seconds, _day(db, d).note) == (
        "dienstbefreiung",
        0,
        "Personalversammlung",
    )
    cal.set_override(db, d, absence_type="krank")
    assert (_day(db, d).absence_type, _day(db, d).soll_seconds) == ("krank", 8 * H)
    cal.set_override(db, d, absence_type="fortbildung")
    assert _day(db, d).soll_seconds == 8 * H

    # Päd. Tag: unterrichtsfrei, soll stays, becomes a leave candidate.
    cal.set_override(db, date(2026, 10, 15), day_type="unterrichtsfrei", note="Päd. Tag")
    assert _day(db, date(2026, 10, 15)).day_type == DayType.UNTERRICHTSFREI
    assert _day(db, date(2026, 10, 15)).soll_seconds == 8 * H

    # Clearing all fields removes the override.
    assert cal.set_override(db, d) is None
    assert db.get(DayOverride, d) is None
    assert (_day(db, d).absence_type, _day(db, d).soll_seconds) == (None, 8 * H)


def test_override_schultag_in_ferien(db, year):
    d = date(2026, 11, 4)
    cal.set_override(db, d, day_type="schultag", note="Nachprüfung")
    row = _day(db, d)
    assert row.day_type == DayType.SCHULTAG and row.ferien_id is None


def test_override_range_weekdays_only(db, year):
    rows = cal.set_override_range(db, date(2026, 10, 12), date(2026, 10, 18), absence_type="krank")
    assert [r.date for r in rows] == [date(2026, 10, 12) + timedelta(days=i) for i in range(5)]
    assert _day(db, date(2026, 10, 16)).absence_type == "krank"
    assert _day(db, date(2026, 10, 17)).absence_type is None
    cal.set_override_range(db, date(2026, 10, 12), date(2026, 10, 18))
    assert cal.list_overrides(db, date(2026, 10, 1), date(2026, 10, 31)) == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"day_type": "feiertag"}, "Tagestyp"),
        ({"day_type": "wochenende"}, "Tagestyp"),
        ({"absence_type": "urlaub"}, "Urlaubszuordnung"),
        ({"absence_type": "faulenzen"}, "Unbekannte Abwesenheit"),
        ({"absence_type": "krank", "urlaub_manuell": True}, "gleichzeitig"),
        ({"urlaub_manuell": True}, "kein Urlaubstag"),
        ({"note": "x" * 501}, "zu lang"),
    ],
)
def test_override_validation(db, year, kwargs, message):
    with pytest.raises(cal.CalendarError, match=message):
        cal.set_override(db, date(2026, 10, 14), **kwargs)


def test_override_outside_period_rejected(db, year):
    with pytest.raises(cal.CalendarError, match="Schuljahr anlegen"):
        cal.set_override(db, date(2030, 1, 7), absence_type="krank")
    with pytest.raises(cal.NotFoundError):
        cal.delete_override(db, date(2026, 10, 14))


def test_manual_leave_mode(db, year):
    settings_svc.update_settings(db, urlaubs_modus="manuell")
    info = cal.leave_plan_info(db, year)
    assert info.assigned == 0 and info.noch_zuzuordnen == 30
    # Unassigned days are an open to-do (info), not an error: enough candidate days exist.
    assert info.info == ["Noch zuzuordnen: 30 Urlaubstag(e)."]
    assert info.hinweise == [] and info.shortfall == 0
    assert db.scalar(select(func.count()).select_from(CalendarDay).where(CalendarDay.absence_type == "urlaub")) == 0

    cal.set_override(db, date(2026, 11, 2), urlaub_manuell=True)
    assert _day(db, date(2026, 11, 2)).absence_type == "urlaub"
    assert _day(db, date(2026, 11, 2)).soll_seconds == 0
    assert cal.leave_plan_info(db, year).noch_zuzuordnen == 29


def test_manual_leave_limit_and_adopt(db, year):
    settings_svc.update_settings(db, urlaubs_modus="manuell", urlaubstage=2)
    cal.set_override(db, date(2026, 11, 2), urlaub_manuell=True)
    cal.set_override(db, date(2026, 11, 3), urlaub_manuell=True)
    with pytest.raises(cal.CalendarError, match="bereits zugeordnet"):
        cal.set_override(db, date(2026, 11, 4), urlaub_manuell=True)
    cal.set_override(db, date(2026, 11, 3), urlaub_manuell=True, note="geändert")  # same day again is fine

    settings_svc.update_settings(db, urlaubstage=30)
    info = cal.adopt_leave_plan(db, year, LeaveMode.SOMMERFERIEN_ZUERST)
    assert info.mode == "manuell" and info.is_complete
    assert min(info.days) == date(2026, 8, 3)
    assert db.get(DayOverride, date(2026, 11, 2)) is None  # old manual selection replaced
    assert db.get(DayOverride, date(2026, 11, 3)).note == "geändert"
    assert db.get(DayOverride, date(2026, 11, 3)).urlaub_manuell is False
    with pytest.raises(cal.CalendarError):
        cal.adopt_leave_plan(db, year, LeaveMode.MANUELL)


def test_leave_shortfall_reported_without_ferien(db):
    p = cal.create_period(db, "2026/27")
    info = cal.leave_plan_info(db, p)
    assert info.assigned == 1  # only Buß- und Bettag is a candidate
    assert info.shortfall == 29 and not info.is_complete
    assert info.hinweise and "Ferien erfasst" in info.hinweise[0]
