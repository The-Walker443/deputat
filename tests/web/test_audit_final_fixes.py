"""Final audit fixes: smaller UI/flow items (S chip note, subcategory flags, Fortbildung hint, first-open pre-fill,
calendar marks, plain German texts, teaching statistics)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Category, TimeEntry
from app.services import calendar_service as cal
from app.services import entries as entries_svc
from app.services import timetable as tts
from app.util import local_to_utc, today_local
from app.web.templating import ampel_de, cron_de


def cat(db, code: str, parent: str | None = None, name: str | None = None) -> Category:
    if name is None:
        return db.scalars(select(Category).where(Category.code == code, Category.parent_id.is_(None))).one()
    main = cat(db, code)
    return db.scalars(select(Category).where(Category.parent_id == main.id, Category.name == name)).one()


def school_year_label(today: date) -> str:
    y = today.year if today.month >= 8 else today.year - 1
    return f"{y}/{str(y + 1)[2:]}"


# --------------------------------------------------------------------------- S chip needs a note


def test_sonstiges_chip_asks_for_a_note(client):
    html = client.get("/heute").text
    assert 'data-timer-field="note"' in html
    s_chip = html[html.index('data-code="S"') - 300: html.index('data-code="S"') + 300]
    assert 'data-requires-note="1"' in s_chip


def test_timer_start_with_note_for_sonstiges(db):
    from app.services import timer as timer_svc

    st = timer_svc.start(db, cat(db, "S").id, note="Elternbrief entworfen",
                         started_at=datetime(2026, 10, 20, 9, 0), now=datetime(2026, 10, 20, 9, 0))
    assert st.running is not None and st.running.note == "Elternbrief entworfen"


# --------------------------------------------------------------------------- subcategory flags


def _create(db, code, sub_name, flags=()):
    start = local_to_utc(date(2026, 10, 20), time(8))
    main = cat(db, code)
    sub = cat(db, code, name=sub_name)
    return entries_svc.create_entry(db, {"start_at": start, "end_at": start + timedelta(hours=1),
                                         "category_id": main.id, "subcategory_id": sub.id, "flags": list(flags)},
                                    source="manuell")[0]


def test_klassenfahrt_subcategory_sets_the_flag(db):
    entry = _create(db, "SV", "Klassenfahrt")
    assert "klassenfahrt" in entry.flags


def test_vertretung_subcategory_defaults_to_angeordnet(db):
    entry = _create(db, "U", "Vertretung (gehalten)")
    assert "vertretung_angeordnet" in entry.flags
    other = entries_svc.create_entry(db, {
        "start_at": local_to_utc(date(2026, 10, 21), time(8)), "end_at": local_to_utc(date(2026, 10, 21), time(9)),
        "category_id": cat(db, "U").id, "subcategory_id": cat(db, "U", name="Vertretung (gehalten)").id,
        "flags": ["vertretung_genehmigt"]}, source="manuell")[0]
    assert "vertretung_genehmigt" in other.flags and "vertretung_angeordnet" not in other.flags


# --------------------------------------------------------------------------- Fortbildung


def test_fortbildung_day_prompts_for_the_entry(client, db):
    today = today_local()
    cal.create_period(db, school_year_label(today))
    db.commit()
    d = today - timedelta(days=1)
    while d.isoweekday() > 5:
        d -= timedelta(days=1)
    resp = client.post(f"/kalender/tag/{d.isoformat()}", data={"absence_type": "fortbildung"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].endswith(f"/kalender/tag/{d.isoformat()}")
    page = client.get(f"/kalender/tag/{d.isoformat()}").text
    assert "Noch keine Fortbildungszeit erfasst" in page
    assert f"kategorie={cat(db, 'F').id}" in page
    assert "zusätzlich als Eintrag" in page  # the flash


# --------------------------------------------------------------------------- first open of the day


def test_heute_creates_todays_suggestions(client, db):
    today = today_local()
    if today.isoweekday() > 5:
        pytest.skip("weekend")
    period = cal.create_period(db, school_year_label(today))
    db.commit()
    tts.create_timetable(db, valid_from=period.start, slots=[
        {"weekday": today.isoweekday(), "slot_no": 1, "klasse": "3b"},
    ])
    db.commit()
    row = db.get(__import__("app.db.models", fromlist=["CalendarDay"]).CalendarDay, today)
    if row is None or row.day_type != "schultag":
        pytest.skip("today is no school day")
    html = client.get("/heute").text
    assert "Stundenplan heute" in html
    db.expire_all()
    assert db.scalars(select(TimeEntry).where(TimeEntry.day == today, TimeEntry.status == "vorschlag")).all()


# --------------------------------------------------------------------------- texts


def test_calendar_marks_do_not_reuse_category_codes(client, db):
    today = today_local()
    cal.create_period(db, school_year_label(today))
    db.commit()
    html = client.get("/kalender").text
    assert "Url Urlaub" in html and "kr krank" in html and "Fb Fortbildung" in html
    assert "U Urlaub" not in html and "K krank" not in html


def test_plain_german_helpers():
    assert cron_de("30 0 * * *") == "jede Nacht um 00:30 Uhr"
    assert cron_de("*/5 * * * *") == "nach Zeitplan"
    assert ampel_de("gruen") == "grün"


def test_export_page_has_no_cron_or_paths(client):
    html = client.get("/export").text
    assert "Zeitplan <code>" not in html and "Proxmox" not in html and "Ablage:" not in html


def test_csv_status_is_german(db):
    from app.services import export as export_svc

    start = local_to_utc(date(2026, 10, 20), time(8))
    entries_svc.create_entry(db, {"start_at": start, "end_at": start + timedelta(hours=1),
                                  "category_id": cat(db, "K").id}, source="manuell")
    data = export_svc.export_csv(db, date_from=date(2026, 10, 1), date_to=date(2026, 10, 31)).decode("utf-8-sig")
    assert ";aktiv;" in data and ";active;" not in data


def test_jahr_shows_expected_teaching(client, db):
    today = today_local()
    cal.create_period(db, school_year_label(today))
    db.commit()
    html = client.get("/jahr").text
    assert "data-teaching" in html and "UStd" in html and "Altersermäßigung" in html
