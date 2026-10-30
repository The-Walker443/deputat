"""Day confirmation screen (/tage, /tag/{date}) – Konzept §3.1 #3, §7.2 (audit: no UI for deviations)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.db.models import DayConfirmation, MonthClose, TimeEntry
from app.services import calendar_service as cal
from app.services import timetable as tts
from app.util import today_local


def last_weekday_before(d: date, weekday: int) -> date:
    """The latest date < d with isoweekday == weekday."""
    x = d - timedelta(days=1)
    while x.isoweekday() != weekday:
        x -= timedelta(days=1)
    return x


@pytest.fixture
def setup(db):
    today = today_local()
    period = cal.create_period(db, f"{today.year if today.month >= 8 else today.year - 1}/"
                                   f"{str((today.year if today.month >= 8 else today.year - 1) + 1)[2:]}")
    db.commit()
    day = last_weekday_before(today, 2)  # a past Tuesday
    if day < period.start:
        pytest.skip("no past Tuesday in the current school year yet")
    tts.create_timetable(db, valid_from=period.start, slots=[
        {"weekday": 2, "slot_no": 1, "klasse": "3b", "fach": "Deutsch"},
        {"weekday": 2, "slot_no": 2, "klasse": "3b", "fach": "Mathematik"},
        {"weekday": 2, "slot_no": 3, "kind": "frei"},
    ])
    db.commit()
    return day


def entries_of(db, d: date) -> list[TimeEntry]:
    db.expire_all()
    return list(db.scalars(select(TimeEntry).where(TimeEntry.day == d, TimeEntry.deleted_at.is_(None))
                           .order_by(TimeEntry.start_at)))


def test_open_days_lists_unconfirmed_days(client, setup):
    page = client.get("/tage")
    assert page.status_code == 200
    assert f'href="/tag/{setup.isoformat()}?next=/tage"' in page.text
    assert "Stundenplan nicht bestätigt" in page.text


def test_day_page_prefills_and_shows_deviation_choices(client, db, setup):
    page = client.get(f"/tag/{setup.isoformat()}")
    assert page.status_code == 200
    text = page.text
    for label in ("wie geplant", "entfallen", "Vertretung gehalten", "Exkursion", "Zeit angepasst"):
        assert label in text
    assert "Deutsch" in text and "Mathematik" in text
    # opening the day created the suggestions (first open of the day)
    assert [e.status for e in entries_of(db, setup)] == ["vorschlag", "vorschlag"]


def test_confirm_with_deviations(client, db, setup):
    page = client.get(f"/tag/{setup.isoformat()}").text
    keys = [k for k in __import__("re").findall(r'name="key" value="([^"]+)"', page)]
    assert len(keys) == 3  # two lessons + the free slot
    data = {"key": keys, "next": "/tage"}
    data["typ_0"] = "entfallen"
    data["typ_1"] = "zeit_angepasst"
    data["start_1"] = "08:50"
    data["end_1"] = "09:40"
    data["typ_2"] = "vertretung"
    data["klasse_2"] = "4a"
    data["art_2"] = "angeordnet"
    resp = client.post(f"/tag/{setup.isoformat()}", data=data, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/tage"
    rows = entries_of(db, setup)
    statuses = sorted(e.status for e in rows)
    assert statuses == ["active", "active", "verworfen"]
    vertretung = next(e for e in rows if e.klasse == "4a")
    assert "vertretung_angeordnet" in (vertretung.flags or [])
    assert db.get(DayConfirmation, setup) is not None
    # the confirmed day is no longer open
    assert f"/tag/{setup.isoformat()}" not in client.get("/tage").text


def test_confirm_error_keeps_input(client, db, setup):
    page = client.get(f"/tag/{setup.isoformat()}").text
    keys = __import__("re").findall(r'name="key" value="([^"]+)"', page)
    resp = client.post(f"/tag/{setup.isoformat()}", data={"key": keys, "typ_0": "zeit_angepasst"})
    assert resp.status_code == 422
    assert "Beginn und Ende" in resp.text
    assert db.get(DayConfirmation, setup) is None


def test_confirm_in_closed_month_needs_reason(client, db, setup):
    db.add(MonthClose(year_month=f"{setup:%Y-%m}", closed_at=__import__("datetime").datetime(2026, 1, 1),
                      entries_hash_sha256="0" * 64, soll_seconds=0, ist_seconds=0,
                      saldo_seconds=0, status="abgeschlossen"))
    db.commit()
    page = client.get(f"/tag/{setup.isoformat()}").text
    assert 'name="grund"' in page


def test_links_to_the_day_page(client, db, setup):
    week = client.get(f"/woche?datum={setup.isoformat()}").text
    assert f'/tag/{setup.isoformat()}?next=' in week
    assert f'/kalender/tag/{setup.isoformat()}' in week
    heute = client.get("/heute").text
    assert f'/kalender/tag/{today_local().isoformat()}' in heute  # "Krank / abwesend" shortcut
