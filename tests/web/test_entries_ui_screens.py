"""Web tests for the 'Einträge' screens (app/web/entries.py): list + filters, create, edit, history, lock, delete."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.db.models import Category, MonthClose, TimeEntry
from app.services import entries as svc

BERLIN = ZoneInfo("Europe/Berlin")


def L(y, m, d, h=0, mi=0) -> datetime:  # noqa: N802 - local wall-clock helper
    return datetime(y, m, d, h, mi, tzinfo=BERLIN)


def cat(db, code: str) -> int:
    return db.scalar(select(Category.id).where(Category.code == code))


def sub(db, code: str, name: str) -> int:
    parent = cat(db, code)
    return db.scalar(select(Category.id).where(Category.parent_id == parent, Category.name == name))


def make(db, start, end=None, *, code="K", source="manuell", **extra) -> TimeEntry:
    data = {"start_at": start, "end_at": end, "category_id": cat(db, code), **extra}
    (entry,) = svc.create_entry(db, data, source=source)
    return entry


def form(db, **overrides) -> dict:
    data = {
        "datum": "2026-10-12", "modus": "zeit", "von": "13:30", "bis": "15:10", "dauer": "",
        "kategorie": str(cat(db, "K")), "unterkategorie": "", "klasse": "3b", "fach": "Mathematik",
        "ort": "zuhause", "notiz": "",
    }
    data.update(overrides)
    return data


@pytest.fixture
def web(client, db):
    """client + db on the same database file."""
    return client, db


# --------------------------------------------------------------------------- list


def test_list_renders_with_default_month_and_empty_state(web):
    client, _ = web
    r = client.get("/eintraege")
    assert r.status_code == 200
    assert 'id="entries-root"' in r.text
    assert "Keine Einträge" in r.text
    assert 'name="kennzeichen"' in r.text and 'name="geloescht"' in r.text
    assert "/static/js/entries.js" in r.text
    assert "<script>" not in r.text  # CSP: no inline scripts


def test_list_groups_by_day_with_sums_and_badges(web):
    client, db = web
    make(db, L(2026, 10, 12, 13, 30), L(2026, 10, 12, 15, 10), klasse="3b")
    make(db, L(2026, 10, 12, 16), L(2026, 10, 12, 17), code="VN")
    svc.create_entry(db, {"has_bounds": False, "day": date(2026, 10, 13), "duration_seconds": "1,5",
                          "category_id": cat(db, "E")}, source="manuell")
    r = client.get("/eintraege", params={"von": "2026-10-01", "bis": "2026-10-31"})
    assert r.status_code == 200
    text = r.text
    assert "Oktober 2026" in text
    assert "12.10.2026" in text and "13.10.2026" in text
    assert "2:40 h" in text  # day sum 12.10.: 1:40 + 1:00
    assert "13:30–15:10" in text and "ohne Uhrzeit" in text
    assert "ohne Zeitgrenzen" in text  # badge
    assert "nachträglich" in text  # created now for a past day
    # newest day first
    assert text.index("13.10.2026") < text.index("12.10.2026")


def test_list_filters_category_source_flag_and_deleted(web):
    client, db = web
    k = make(db, L(2026, 10, 5, 9), L(2026, 10, 5, 10), code="K")
    make(db, L(2026, 10, 6, 9), L(2026, 10, 6, 10), code="VN")
    make(db, L(2026, 10, 7, 9), L(2026, 10, 7, 10), code="U", flags=["vertretung_angeordnet"])
    imp = svc.create_entry(db, {"has_bounds": False, "day": date(2026, 10, 8), "duration_seconds": 3600,
                                "category_id": cat(db, "KO")}, source="import")[0]
    svc.delete_entry(db, k.id, reason="doppelt")
    base = {"von": "2026-10-01", "bis": "2026-10-31"}

    r = client.get("/eintraege", params={**base, "kategorie": cat(db, "VN")})
    assert "06.10.2026" in r.text and "07.10.2026" not in r.text

    r = client.get("/eintraege", params={**base, "quelle": "import"})
    assert "08.10.2026" in r.text and "06.10.2026" not in r.text
    assert "Import" in r.text and f"/eintraege/{imp.id}" in r.text

    r = client.get("/eintraege", params={**base, "kennzeichen": "vertretung"})
    assert "07.10.2026" in r.text and "06.10.2026" not in r.text
    assert "Vertretung angeordnet" in r.text

    r = client.get("/eintraege", params=base)
    assert f"/eintraege/{k.id}" not in r.text  # deleted hidden by default
    r = client.get("/eintraege", params={**base, "geloescht": "1"})
    assert f"/eintraege/{k.id}" in r.text and "gelöscht" in r.text


def test_list_htmx_returns_partial_and_more_pages(web):
    client, db = web
    for day in range(1, 13):
        make(db, L(2026, 9, day, 9), L(2026, 9, day, 10))
    params = {"von": "2026-09-01", "bis": "2026-09-30"}
    r = client.get("/eintraege", params=params, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<html" not in r.text and 'id="entries-root"' in r.text
    assert r.text.count('class="entries-day ') == 10
    assert "/eintraege/mehr?" in r.text and "seite=2" in r.text

    more = client.get("/eintraege/mehr", params={**params, "seite": 2}, headers={"HX-Request": "true"})
    assert more.status_code == 200
    assert more.text.count('class="entries-day ') == 2
    assert "02.09.2026" in more.text and "01.09.2026" in more.text
    assert "/eintraege/mehr?" not in more.text

    # history restore / no-JS fallback gets the full page with both pages
    full = client.get("/eintraege", params={**params, "seite": 2},
                      headers={"HX-Request": "true", "HX-History-Restore-Request": "true"})
    assert "<html" in full.text and full.text.count('class="entries-day ') == 12


# --------------------------------------------------------------------------- create


def test_new_form_renders_hints(web):
    client, _ = web
    r = client.get("/eintraege/neu", params={"datum": "2026-10-12"})
    assert r.status_code == 200
    assert "Keine Namen von Kindern oder Eltern – z. B. ‚Gespräch Übertritt, 4a‘" in r.text
    assert "ohne Uhrzeiten – geringere Beweiskraft" in r.text.replace("Ohne", "ohne")
    assert "mehr als 48 h nach dem Tag" in r.text
    assert 'value="2026-10-12"' in r.text
    assert 'name="flags" value="vertretung_angeordnet"' in r.text
    assert '<option value="3b">3b</option>' in r.text  # Klasse is a select from settings


def test_create_bounded_entry_redirects_with_flash(web):
    client, db = web
    data = form(db, unterkategorie=str(sub(db, "K", "Proben")), notiz="Probe Mathe")
    r = client.post("/eintraege/neu", data=data, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/eintraege?von=2026-10-01&bis=2026-10-31")
    db.expire_all()
    entry = db.scalars(select(TimeEntry)).one()
    assert entry.has_bounds and entry.duration_seconds == 100 * 60
    assert entry.day == date(2026, 10, 12) and entry.source == "manuell"
    assert entry.klasse == "3b" and entry.location == "zuhause" and entry.subcategory.name == "Proben"
    page = client.get(r.headers["location"])
    assert "Eintrag gespeichert (1:40 h)" in page.text
    assert "13:30–15:10" in page.text and "Proben" in page.text


def test_create_duration_only_entry(web):
    client, db = web
    r = client.post("/eintraege/neu", data=form(db, modus="dauer", dauer="1,5", von="", bis=""),
                    follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    entry = db.scalars(select(TimeEntry)).one()
    assert entry.has_bounds is False and entry.duration_seconds == 5400
    assert "ohne_zeitgrenzen" in entry.flags


def test_create_respects_next_and_vertretung_only_for_unterricht(web):
    client, db = web
    data = form(db, kategorie=str(cat(db, "VN")), flags=["vertretung_angeordnet", "klassenfahrt"],
                next="/woche")
    r = client.post("/eintraege/neu", data=data, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/woche"
    db.expire_all()
    entry = db.scalars(select(TimeEntry)).one()
    assert "klassenfahrt" in entry.flags and "vertretung_angeordnet" not in entry.flags


def test_create_over_midnight_is_split(web):
    client, db = web
    r = client.post("/eintraege/neu", data=form(db, von="22:00", bis="01:00", folgetag="1"),
                    follow_redirects=True)
    assert r.status_code == 200
    assert "in 2 Einträge geteilt" in r.text
    db.expire_all()
    assert len(db.scalars(select(TimeEntry)).all()) == 2


@pytest.mark.parametrize(
    ("overrides", "message", "field"),
    [
        ({"bis": "12:00"}, "„Bis“ muss nach „Von“ liegen", "bis"),
        ({"datum": ""}, "Bitte ein gültiges Datum angeben.", "datum"),
        ({"modus": "dauer", "dauer": "abc"}, "Ungültige Dauer", "dauer"),
        ({"kategorie": ""}, "Bitte eine Kategorie wählen.", "kategorie"),
    ],
)
def test_create_validation_errors_are_shown_inline(web, overrides, message, field):
    client, db = web
    r = client.post("/eintraege/neu", data=form(db, **overrides))
    assert r.status_code == 422
    assert "Nicht gespeichert" in r.text and message in r.text
    assert f'aria-describedby="err-{field}"' in r.text
    db.expire_all()
    assert db.scalars(select(TimeEntry)).all() == []


def test_service_errors_are_shown_in_german(web):
    client, db = web
    # Sonstiges requires a note (service rule)
    r = client.post("/eintraege/neu", data=form(db, kategorie=str(cat(db, "S"))))
    assert r.status_code == 422
    assert "ist eine Notiz Pflicht" in r.text and 'aria-describedby="err-notiz"' in r.text
    # Overlap
    make(db, L(2026, 10, 12, 14), L(2026, 10, 12, 16))
    r = client.post("/eintraege/neu", data=form(db))
    assert r.status_code == 422 and "Überschneidung mit einem bestehenden Eintrag" in r.text
    # submitted values are kept
    assert 'value="13:30"' in r.text


# --------------------------------------------------------------------------- edit / history


def test_edit_form_prefilled(web):
    client, db = web
    entry = make(db, L(2026, 10, 12, 13, 30), L(2026, 10, 12, 15, 10), klasse="3b",
                 subcategory_id=sub(db, "K", "Proben"))
    r = client.get(f"/eintraege/{entry.id}")
    assert r.status_code == 200
    assert 'value="13:30"' in r.text and 'value="15:10"' in r.text
    assert "Version 1 (aktuell)" in r.text
    assert "Noch keine Änderungen" in r.text


def test_edit_creates_revision_and_history_shows_diff(web):
    client, db = web
    entry = make(db, L(2026, 10, 12, 13, 30), L(2026, 10, 12, 15, 10), klasse="3b",
                 subcategory_id=sub(db, "K", "Proben"))
    data = form(db, unterkategorie=str(sub(db, "K", "Proben")), bis="15:30", ort="",
                fach="", begruendung="Endzeit korrigiert")
    r = client.post(f"/eintraege/{entry.id}", data=data, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/eintraege/{entry.id}"
    db.expire_all()
    revs = svc.revisions(db, entry.id)
    assert len(revs) == 1 and revs[0].change_reason == "Endzeit korrigiert"
    assert db.get(TimeEntry, entry.id).duration_seconds == 120 * 60

    page = client.get(r.headers["location"])
    text = page.text
    assert "Änderung gespeichert" in text
    assert "Version 2 (aktuell)" in text
    assert re.search(r"Version 1 vom \d\d\.\d\d\. \d\d:\d\d", text)
    assert "13:30–15:10 K/Proben 3b" in text
    assert "13:30–15:30 K/Proben 3b" in text
    assert "<del>13:30–15:10</del>" in text and "<ins>13:30–15:30</ins>" in text
    assert "Grund: Endzeit korrigiert" in text


def test_edit_without_changes_writes_no_revision(web):
    client, db = web
    entry = make(db, L(2026, 10, 12, 13, 30), L(2026, 10, 12, 15, 10), klasse="3b", fach="Mathematik",
                 location="zuhause")
    r = client.post(f"/eintraege/{entry.id}", data=form(db), follow_redirects=True)
    assert "Keine Änderung." in r.text
    assert svc.revisions(db, entry.id) == []


def test_edit_keeps_seconds_of_timer_entries_when_only_note_changes(web):
    client, db = web
    start = L(2026, 10, 12, 13, 30).replace(second=17)
    end = L(2026, 10, 12, 15, 10).replace(second=42)
    entry = make(db, start, end, source="timer")
    data = form(db, klasse="", fach="", ort="", notiz="Hefte Mathe")
    r = client.post(f"/eintraege/{entry.id}", data=data, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    fresh = db.get(TimeEntry, entry.id)
    assert fresh.note == "Hefte Mathe"
    assert fresh.duration_seconds == entry.duration_seconds
    assert "korrigiert" not in fresh.flags


def test_unknown_entry_is_404(web):
    client, _ = web
    r = client.get("/eintraege/does-not-exist", headers={"Accept": "text/html"})
    assert r.status_code == 404
    assert "Eintrag nicht gefunden." in r.text


# --------------------------------------------------------------------------- locked months


def _close_month(db, ym: str) -> MonthClose:
    for entry in svc.month_hash_entries(db, ym):
        entry.locked = True
    mc = MonthClose(year_month=ym, closed_at=datetime(2026, 10, 1, 10),
                    entries_hash_sha256=svc.month_entries_hash(db, ym),
                    soll_seconds=0, ist_seconds=0, saldo_seconds=0)
    db.add(mc)
    db.commit()
    return mc


def test_locked_entry_requires_reason_on_edit(web):
    client, db = web
    entry = make(db, L(2026, 9, 14, 13, 30), L(2026, 9, 14, 15), klasse="3b", fach="Mathematik",
                 location="zuhause")
    _close_month(db, "2026-09")

    page = client.get(f"/eintraege/{entry.id}")
    assert "Gesperrt – Monat abgeschlossen." in page.text
    assert 'name="begruendung"' in page.text and "gesperrt" in page.text

    data = form(db, datum="2026-09-14", bis="15:30")
    r = client.post(f"/eintraege/{entry.id}", data=data)
    assert r.status_code == 422
    assert "Monat abgeschlossen – Änderung nur mit Begründung" in r.text
    assert 'aria-describedby="err-begruendung"' in r.text
    db.expire_all()
    assert svc.revisions(db, entry.id) == []

    r = client.post(f"/eintraege/{entry.id}", data={**data, "begruendung": "Tippfehler"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    (rev,) = svc.revisions(db, entry.id)
    assert rev.change_reason == "Tippfehler"
    assert "korrigiert" in db.get(TimeEntry, entry.id).flags
    assert db.get(MonthClose, "2026-09").status == "nachtraeglich_korrigiert"


def test_create_in_closed_month_requires_reason(web):
    client, db = web
    _close_month(db, "2026-09")
    page = client.get("/eintraege/neu", params={"datum": "2026-09-10"})
    assert "Dieser Monat ist abgeschlossen" in page.text
    assert "data-reason-field hidden" not in page.text  # visible without JS for a closed month

    r = client.post("/eintraege/neu", data=form(db, datum="2026-09-10"))
    assert r.status_code == 422 and "Monat abgeschlossen" in r.text
    r = client.post("/eintraege/neu", data=form(db, datum="2026-09-10", begruendung="vergessen"),
                    follow_redirects=False)
    assert r.status_code == 303


# --------------------------------------------------------------------------- delete / restore


def test_delete_is_soft_and_restorable(web):
    client, db = web
    entry = make(db, L(2026, 10, 12, 13, 30), L(2026, 10, 12, 15, 10))
    r = client.post(f"/eintraege/{entry.id}/loeschen", data={"grund": "doppelt"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/eintraege?von=2026-10-01")
    db.expire_all()
    fresh = db.get(TimeEntry, entry.id)
    assert fresh is not None and fresh.deleted_at is not None
    (rev,) = svc.revisions(db, entry.id)
    assert svc.revision_kind(rev) == "geloescht" and rev.change_reason == "doppelt"

    page = client.get(f"/eintraege/{entry.id}")
    assert "Gelöscht am" in page.text and "Wiederherstellen" in page.text
    assert "Version 1 vom" in page.text and "Gelöscht" in page.text

    r = client.post(f"/eintraege/{entry.id}/wiederherstellen", data={}, follow_redirects=True)
    assert "Eintrag wiederhergestellt." in r.text
    db.expire_all()
    assert db.get(TimeEntry, entry.id).deleted_at is None


def test_delete_locked_entry_requires_reason(web):
    client, db = web
    entry = make(db, L(2026, 9, 14, 13, 30), L(2026, 9, 14, 15))
    _close_month(db, "2026-09")
    r = client.post(f"/eintraege/{entry.id}/loeschen", data={"grund": ""})
    assert r.status_code == 422
    assert "Monat abgeschlossen – Änderung nur mit Begründung" in r.text
    db.expire_all()
    assert db.get(TimeEntry, entry.id).deleted_at is None

    r = client.post(f"/eintraege/{entry.id}/loeschen", data={"grund": "Fehlbuchung"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(TimeEntry, entry.id).deleted_at is not None


def test_split_entry_history_shows_creation_note(web):
    client, db = web
    (first, _second) = svc.create_entry(
        db, {"start_at": L(2026, 10, 12, 22), "end_at": L(2026, 10, 13, 1), "category_id": cat(db, "K")},
        source="manuell")
    page = client.get(f"/eintraege/{first.id}")
    assert "Automatisch an Mitternacht geteilt – Teil 1 von 2" in page.text
    assert timedelta(hours=2) == timedelta(seconds=first.duration_seconds)
