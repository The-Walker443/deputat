"""Day type chip and sheet on the home screen "Erfassen" (partials/erfassen_week.html, erfassen.html)."""

from __future__ import annotations

import re
from datetime import date

import pytest

H = 3600
MON = date(2026, 11, 9)  # KW 46


@pytest.fixture
def year(client, no_ferien_catalog):
    assert client.post("/api/calendar/periods", json={"label": "2026/27"}).status_code == 201


def row(html: str, d: date) -> str:
    return html.split(f'data-date="{d.isoformat()}"', 1)[1].split("</li>", 1)[0]


def test_every_row_has_a_type_chip_and_the_sheet_is_rendered_once(client, year):
    html = client.get("/erfassen/2026-W46").text
    chips = re.findall(r'<button type="button" class="qe-chip[^"]*" data-qe-typ', html)
    assert len(chips) == 7
    mon = row(html, MON)
    assert 'data-typ="unterricht"' in mon and ">Unterricht</span>" in mon and 'aria-haspopup="dialog"' in mon
    assert 'data-typ-options="unterricht krank fortbildung klassenfahrt schulveranstaltung"' in mon
    sat = row(html, date(2026, 11, 14))
    assert ">Wochenende</span>" in sat and "krank" not in sat.split("data-typ-options=", 1)[1].split('"')[1]
    assert html.count("data-qe-sheet>") == 1 and '<dialog class="qe-sheet"' in html
    choices = re.findall(r'data-qe-typ-choice="([a-z]+)"', html)
    assert choices == ["unterricht", "krank", "fortbildung", "klassenfahrt", "schulveranstaltung"]
    assert "zählt höchstens 10:00 h am Tag" in html  # Klassenfahrt variant B
    assert 'data-kf-cap="36000"' in html
    # week switching only swaps the block: the partial has no second sheet
    partial = client.get("/erfassen/2026-W46", headers={"X-Erfassen-Partial": "1"}).text
    assert "data-qe-sheet" not in partial and "data-qe-typ" in partial


def test_krank_row_has_a_disabled_field(client, year):
    client.put(f"/api/days/{MON}/total", json={"value": "6"})
    client.put(f"/api/days/{MON}/typ", json={"typ": "krank"})
    mon = row(client.get("/erfassen/2026-W46").text, MON)
    assert re.search(r"<input [^>]*data-qe-input[^>]*disabled", mon, flags=re.S)
    assert 'value=""' in mon and "krank – Soll wird gutgeschrieben" in mon and "qe-badge-krank" in mon


def test_klassenfahrt_row_shows_the_cap(client, year):
    client.put(f"/api/days/{MON}/typ", json={"typ": "klassenfahrt"})
    client.put(f"/api/days/{MON}/total", json={"value": "12"})
    mon = row(client.get("/erfassen/2026-W46").text, MON)
    assert "qe-badge-sv" in mon and ">Klassenfahrt</span>" in mon and 'value="12:00"' in mon
    assert 'zählt <span class="num">10:00</span>' in mon  # what the account counts


def test_fixed_absence_links_to_the_calendar(client, year):
    client.put(f"/api/calendar/overrides/{MON}", json={"absence_type": "sonderurlaub"})
    mon = row(client.get("/erfassen/2026-W46").text, MON)
    assert f'<a class="qe-chip qe-badge-frei" href="/kalender/tag/{MON.isoformat()}"' in mon
    assert "Sonderurlaub" in mon and "data-qe-typ " not in mon


def test_calendar_day_without_calendar_input_offers_no_remove_button(client, year):
    client.put(f"/api/days/{MON}/typ", json={"typ": "schulveranstaltung"})
    html = client.get(f"/kalender/tag/{MON.isoformat()}").text
    assert "Eingaben entfernen" not in html


# --------------------------------------------------------------------------- review fixes (regressions)


def meta_text(row_html: str) -> str:
    return re.sub(r"<[^>]+>", "", row_html.split("data-qe-meta>", 1)[1].split("</p>", 1)[0])


def test_krank_row_keeps_the_extra_hours_visible(client, year):
    from app.db.models import Category
    from app.db.session import SessionLocal
    from app.services import entries as entries_svc
    from app.util import local_to_utc
    from datetime import time as dtime, timedelta

    with SessionLocal() as s:
        ko = s.query(Category).filter_by(code="K", parent_id=None).one().id
        a = local_to_utc(MON, dtime(8))
        entries_svc.create_entry(s, {"start_at": a, "end_at": a + timedelta(hours=2), "category_id": ko},
                                 source="manuell")
    client.put(f"/api/days/{MON}/total", json={"value": "7,5"})
    client.put(f"/api/days/{MON}/typ", json={"typ": "krank"})
    html = client.get("/erfassen/2026-W46").text
    mon = row(html, MON)
    text = meta_text(mon)
    # the part that must not be cut off comes before the soll (the line is clamped on phones) ...
    assert text.index("+ 2:00 erfasst, zählt zusätzlich") < text.index("Soll 8:00")
    # ... and krank rows wrap instead of being clamped
    assert re.search(r'class="qe-day[^"]*\bis-krank\b', html.split(f'data-date="{MON.isoformat()}"')[0][-400:])
    css = client.get("/static/css/erfassen.css").text
    assert re.search(r"\.qe-day\.is-krank \.qe-meta\s*\{[^}]*-webkit-line-clamp:\s*unset", css)


def test_week_summary_has_a_krank_note_the_script_can_update(client, year):
    html = client.get("/erfassen/2026-W46").text
    assert re.search(r'<span class="qe-krank-note" data-qe-krank-note data-base-krank="0" hidden>', html)
    client.put(f"/api/days/{MON}/typ", json={"typ": "krank"})
    html = client.get("/erfassen/2026-W46").text
    assert 'data-base-krank="28800"' in html and 'data-qe-krank-sum>8:00<' in html
    assert 'data-krank-gutschrift="28800"' in row(html, MON)
    js = client.get("/static/js/erfassen.js").text
    assert "renderKrankNote" in js and "week.krank_gutschrift" in js


def test_automatic_leave_day_has_a_chip_not_a_link(client, year):
    buss = date(2026, 11, 18)
    html = client.get("/erfassen/2026-W47").text
    cell = row(html, buss)
    assert re.search(r'<button type="button" class="qe-chip qe-badge-urlaub[^"]*" data-qe-typ', cell)
    assert "Urlaub (autom.)" in cell and "data-typ-hint=" in cell and 'href="/kalender/tag/' not in cell.split(
        "data-qe-meta", 1)[0]
    assert client.put(f"/api/days/{buss}/typ", json={"typ": "fortbildung"}).status_code == 200


def test_closed_month_chip_asks_for_a_reason(client, year):
    from app.db.models import MonthClose
    from app.db.session import SessionLocal
    from app.util import now_utc

    with SessionLocal() as s:
        s.add(MonthClose(year_month="2026-10", closed_at=now_utc(), entries_hash_sha256="x", soll_seconds=0,
                         ist_seconds=0, saldo_seconds=0, hash_history=[]))
        s.commit()
    html = client.get("/erfassen/2026-W42").text
    cell = row(html, date(2026, 10, 14))
    assert "data-needs-reason" in cell and re.search(r'<button type="button" class="qe-chip[^"]*" data-qe-typ', cell)
    assert "data-qe-reason-input" in html and "Grund der Korrektur" in html
    assert "Art des Tages" in html  # the sheet's title (the calendar's "Tagestyp" is another thing)


def test_chip_is_out_of_the_tab_order_and_has_a_shortcut(client, year):
    mon = row(client.get("/erfassen/2026-W46").text, MON)
    assert re.search(r'data-qe-typ\s+tabindex="-1"', mon)
    assert 'aria-keyshortcuts="Alt+ArrowDown"' in mon
    js = client.get("/static/js/erfassen.js").text
    assert 'ev.key === "ArrowDown" && ev.altKey' in js


def test_outside_a_school_year_the_sheet_explains_missing_types(client):
    html = client.get("/erfassen/2026-W46").text  # fresh install: no school year
    assert 'data-typ-disabled="krank fortbildung"' in row(html, MON)
    assert "data-qe-typ-noyear" in html and 'href="/kalender/einrichtung"' in html


def test_row_carries_its_state_for_a_provisional_info_line(client, year):
    import html as html_lib
    import json

    client.put(f"/api/days/{MON}/total", json={"value": "6"})
    mon = row(client.get("/erfassen/2026-W46").text, MON)
    raw = re.search(r"data-meta='([^']*)'", mon).group(1)
    meta = json.loads(html_lib.unescape(raw))
    assert meta["ist"] == 6 * H and meta["soll"] == 8 * H and meta["krank"] is False
    js = client.get("/static/js/erfassen.js").text
    assert "renderProvisionalMeta(row, typ)" in js


def test_default_option_text_is_plain(client, year):
    js = client.get("/static/js/erfassen.js").text
    assert "Stunden pauschal (AZ)" not in js and "Stunden zählen als normale Arbeitszeit" in js
    assert "auch für " in js and "qeFollow" in js  # one-tap offer for the following weekdays


def test_entries_mark_the_tagessumme(client, year):
    client.put(f"/api/days/{MON}/typ", json={"typ": "fortbildung"})
    client.put(f"/api/days/{MON}/total", json={"value": "7,5"})
    page = client.get(f"/eintraege?von={MON}&bis={MON}").text
    assert "Tagessumme (Erfassen)" in page
    (entry,) = client.get("/api/entries", params={"date_from": MON.isoformat(), "date_to": MON.isoformat()}
                          ).json()["entries"]
    edit = client.get(f"/eintraege/{entry['id']}").text
    assert "data-tagessumme-hint" in edit and "/erfassen/2026-W46" in edit


def test_calendar_day_shows_the_erfassen_type_and_what_krank_removes(client, year):
    client.put(f"/api/days/{MON}/typ", json={"typ": "klassenfahrt"})
    client.put(f"/api/days/{MON}/total", json={"value": "9"})
    page = client.get(f"/kalender/tag/{MON}").text
    assert "data-erfassen-typ" in page and "<strong>Klassenfahrt</strong>" in page
    assert "data-krank-removes" in page and "9:00" in page
    assert "die Fortbildungszeit zusätzlich als Eintrag (F)" not in page


def test_calendar_form_krank_removes_the_tagessumme(client, year):
    client.put(f"/api/days/{MON}/total", json={"value": "8"})
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', client.get(f"/kalender/tag/{MON}").text).group(1)
    resp = client.post(f"/kalender/tag/{MON}", data={"csrf_token": csrf, "absence_type": "krank"},
                       follow_redirects=True)
    assert resp.status_code == 200
    assert "8:00 h wurden entfernt" in resp.text
    week = client.get("/api/weeks/2026-W46").json()
    mon = week["days"][0]
    assert mon["krank"] and mon["ist"] == 0 and mon["ist_konto"] == mon["soll"]


def test_offline_queue_sends_its_send_time(client):
    js = client.get("/static/js/offline-queue.js").text
    assert '"X-Sent-At": new Date().toISOString()' in js
