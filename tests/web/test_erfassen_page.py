"""Home screen "Erfassen" (app/web/erfassen.py, partials/erfassen_week.html)."""

from __future__ import annotations

import re
from datetime import date, time as dtime, timedelta

from app.services import quick_entry as qe
from app.util import local_to_utc, today_local

H = 3600


def _active_tabs(html: str) -> list[str]:
    return re.findall(r'<a href="([^"]+)" class="bottom-nav-item is-active"', html)


def test_home_redirects_to_erfassen(client):
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "/erfassen"


def test_render_without_period(client):
    res = client.get("/erfassen")
    assert res.status_code == 200
    html = res.text
    today = today_local()
    assert qe.week_label(qe.monday_of(today)) in html
    inputs = re.findall(r"<input [^>]*data-qe-input[^>]*>", html, flags=re.S)
    assert len(inputs) == 7
    assert all('inputmode="decimal"' in i and 'type="text"' in i for i in inputs)
    assert 'enterkeyhint="done"' in inputs[-1] and 'enterkeyhint="next"' in inputs[0]
    first = qe.monday_of(today)
    assert f'aria-label="Montag, {first.day}. ' in html
    # one hint only: the global reminder ("Einrichten") already links the setup
    assert "data-qe-no-period" in html and "banner-info" not in html.split("data-qe-root", 1)[1]
    assert "/static/js/erfassen.js" in html and "/static/js/offline-queue.js" in html
    assert "/static/css/erfassen.css" in html
    assert "Diese Woche" not in html  # already the current week
    assert '<script>' not in html.split("<main", 1)[1]  # CSP: no inline script


def test_nav_labels_and_active_tab(client):
    html = client.get("/erfassen").text
    nav = html.split('class="bottom-nav"', 1)[1]
    labels = re.findall(r"<span>([^<]+)</span>", nav)
    assert labels == ["Erfassen", "Timer", "Konto", "Jahr", "Mehr"]
    assert _active_tabs(html) == ["/erfassen"]
    assert _active_tabs(client.get("/heute").text) == ["/heute"]
    assert _active_tabs(client.get("/woche").text) == ["/erfassen"]
    assert 'href="/woche"' in client.get("/mehr").text


def test_week_navigation_and_partial(client):
    res = client.get("/erfassen/2026-W45")
    assert res.status_code == 200
    html = res.text
    assert "KW 45 · 02.–08.11.2026" in html
    assert 'href="/erfassen/2026-W44"' in html and 'href="/erfassen/2026-W46"' in html
    if qe.week_ref(today_local()) != "2026-W45":
        assert "Diese Woche" in html
    assert 'href="/woche/2026-W45"' in html
    part = client.get("/erfassen/2026-W46", headers={"X-Erfassen-Partial": "1"})
    assert part.status_code == 200 and "<html" not in part.text and 'data-ref="2026-W46"' in part.text
    assert part.headers["cache-control"] == "no-store"
    assert client.get("/erfassen/2026-W60").status_code == 404
    assert client.get("/erfassen/irgendwas").status_code == 404


def test_values_detail_and_running_timer(client):
    from app.db.models import Category
    from app.db.session import SessionLocal
    from app.services import entries as entries_svc

    day = date(2026, 11, 3)
    with SessionLocal() as s:
        u = s.query(Category).filter_by(code="U", parent_id=None).one().id
        a = local_to_utc(day, dtime(8))
        entries_svc.create_entry(s, {"start_at": a, "end_at": a + timedelta(hours=4), "category_id": u},
                                 source="manuell")
        qe.set_day_total(s, day, 7 * H + 30 * 60)
        entries_svc.create_entry(s, {"start_at": local_to_utc(today_local(), dtime(0, 1)), "category_id": u},
                                 source="timer")
    html = client.get("/erfassen/2026-W45").text
    assert 'value="7:30"' in html
    assert "inkl. <span class=\"num\">4:00</span> einzeln" in html
    assert 'href="/eintraege?von=2026-11-03&amp;bis=2026-11-03"' in html
    current = client.get("/erfassen").text
    assert "data-qe-timer" in current and "Timer läuft · U" in current


def test_render_with_period_soll_badges(client, no_ferien_catalog):
    assert client.post("/api/calendar/periods", json={"label": "2026/27"}).status_code == 201
    html = client.get("/erfassen/2026-W52").text  # 21.–27.12.2026
    assert "Soll 8:00" in html
    assert "1. Weihnachtstag" in html and "qe-badge-feiertag" in html
    assert "von\n        <span class=\"num\">32:00</span> (Soll)" in html  # Mo–Do (24.12. is a workday), 25.12. holiday
    assert "data-qe-no-period" not in html


def test_locked_day_is_read_only(client):
    from app.db.models import MonthClose
    from app.db.session import SessionLocal
    from app.util import now_utc

    with SessionLocal() as s:
        s.add(MonthClose(year_month="2026-09", closed_at=now_utc(), entries_hash_sha256="x", soll_seconds=0,
                         ist_seconds=0, saldo_seconds=0, hash_history=[]))
        s.commit()
    html = client.get("/erfassen/2026-W40").text  # 28.09.–04.10.2026
    rows = re.findall(r'<li class="qe-day[^"]*"\s+data-qe-day data-date="([^"]+)"', html)
    assert rows[0] == "2026-09-28"
    tue = html.split('data-date="2026-09-29"', 1)[1].split("</li>", 1)[0]
    assert "readonly" in tue and "Monat abgeschlossen" in tue and "korrigieren" in tue
    thu = html.split('data-date="2026-10-01"', 1)[1].split("</li>", 1)[0]
    assert "readonly" not in thu


def test_timer_page_title(client):
    assert "<title>Timer · Deputat</title>" in client.get("/heute").text


def test_week_screen_details_after_review(client):
    """No per-row "nachträglich" badge, Friday ends the Enter flow when the weekend is empty, the prompt
    for entries added after the day total, the hint shows the iPhone-friendly 7,30."""
    from app.db.models import Category
    from app.db.session import SessionLocal
    from app.services import entries as entries_svc
    from app.util import now_utc

    day = date(2026, 10, 27)  # entered late -> flagged in the data, not on this screen
    with SessionLocal() as s:
        qe.set_day_total(s, day, 8 * H)
        u = s.query(Category).filter_by(code="U", parent_id=None).one().id
        a = local_to_utc(day, dtime(8))
        entries_svc.create_entry(s, {"start_at": a, "end_at": a + timedelta(hours=5), "category_id": u},
                                 source="manuell", now=now_utc() + timedelta(seconds=5))
    html = client.get("/erfassen/2026-W44").text
    assert "qe-badge-late" not in html and "nachträglich" not in html
    inputs = re.findall(r"<input [^>]*data-qe-input[^>]*>", html, flags=re.S)
    assert 'enterkeyhint="done"' in inputs[4] and 'enterkeyhint="next"' in inputs[3]
    tue = html.split('data-date="2026-10-27"', 1)[1].split("</li>", 1)[0]
    assert "Nach deiner Tagessumme kamen" in tue
    assert 'data-qe-set="28800"' in tue and "Auf 8:00 zurück" in tue
    assert 'data-qe-set="46800"' in tue and "13:00 stimmt" in tue
    assert "<kbd>7,30</kbd>" in html and "<kbd>7:30</kbd>" not in html


def test_saturday_value_keeps_the_enter_flow_going(client):
    with __import__("app.db.session", fromlist=["SessionLocal"]).SessionLocal() as s:
        qe.set_day_total(s, date(2026, 11, 14), 2 * H)
    inputs = re.findall(r"<input [^>]*data-qe-input[^>]*>", client.get("/erfassen/2026-W46").text, flags=re.S)
    assert 'enterkeyhint="next"' in inputs[4] and 'enterkeyhint="done"' in inputs[6]


def test_wizard_mid_year_starts_recording_today_and_ends_on_erfassen(client):
    """A new user who just taps through the wizard must not start at -288 h."""
    from app.db.models import Period, Settings
    from app.db.session import SessionLocal

    today = today_local()
    r = client.post("/kalender/perioden", data={"wizard": "1"}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as s:
        period = s.query(Period).one()
        expected = today if period.start < today <= period.end else None
        assert s.get(Settings, 1).erfassung_ab == expected
    step4 = client.get(f"/kalender/einrichtung?periode={period.id}&schritt=4").text
    assert 'form="w4-erfassung-form" name="fertig" value="1"' in step4 and 'id="w4-erfassung-form"' in step4
    assert "/einstellungen#upz" in step4  # part-time hint
    r = client.post("/kalender/erfassung-ab", data={"erfassung_ab": today.isoformat(), "fertig": "1",
                                                    "next": "/kalender"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/erfassen"
    with SessionLocal() as s:
        assert s.get(Settings, 1).erfassung_ab == today


def test_wizard_keeps_the_period_start_when_work_was_recorded_before(client):
    from app.db.models import Category, Period, Settings
    from app.db.session import SessionLocal
    from app.services import entries as entries_svc

    today = today_local()
    with SessionLocal() as s:
        u = s.query(Category).filter_by(code="U", parent_id=None).one().id
        a = local_to_utc(today - timedelta(days=20), dtime(8))
        entries_svc.create_entry(s, {"start_at": a, "end_at": a + timedelta(hours=2), "category_id": u},
                                 source="import")
    client.post("/kalender/perioden", data={"wizard": "1"})
    with SessionLocal() as s:
        period = s.query(Period).one()
        if period.start < today - timedelta(days=20):
            assert s.get(Settings, 1).erfassung_ab is None


def test_az_category_has_no_active_or_note_toggle(client):
    html = client.get("/einstellungen/kategorien").text
    az = html.split('<span class="admin-code" title="fester Code">AZ</span>', 1)[1].split("Unterkategorien</h3>", 1)[0]
    assert 'name="requires_note"' not in az and 'type="checkbox" name="active"' not in az
    assert "bleibt immer aktiv" in az
