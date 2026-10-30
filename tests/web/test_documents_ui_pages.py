"""Web: screen 'Schreiben' (/schreiben*), the reminder slot (/hinweise) and the documents JSON API."""

from __future__ import annotations

import re
from datetime import date, time, timedelta
from html import unescape

from sqlalchemy import select

from app.db.models import Category, Document, Settings, TimeEntry
from app.db.session import SessionLocal
from app.services import calendar_service as cal
from app.services import documents as docs
from app.util import local_to_utc, today_local

NOTICE = "Vorlage, keine Rechtsberatung. Vor Versand prüfen, idealerweise mit Personalrat oder Verbands-Rechtsschutz."


def setup_year(*, name: str = "Erika Muster") -> None:
    with SessionLocal() as s:
        cal.create_period(s)
        st = s.get(Settings, 1)
        st.name, st.schule = name, "Grundschule Musterstadt"
        s.commit()


def add_entry(day: date, hours: float, code: str = "U", flags=()) -> None:
    with SessionLocal() as s:
        start = local_to_utc(day, time(8))
        s.add(TimeEntry(
            start_at=start, end_at=start + timedelta(hours=hours), duration_seconds=int(hours * 3600),
            has_bounds=True, day=day, category_id=s.scalar(select(Category.id).where(Category.code == code)),
            status="active", source="manuell", flags=list(flags),
        ))
        s.commit()


def last_school_day() -> date:
    d = today_local() - timedelta(days=1)
    while d.weekday() > 4:
        d -= timedelta(days=1)
    return d


def plain(html: str) -> str:
    """Visible text: tags removed, whitespace collapsed."""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", html)))


def doc_ids() -> list[str]:
    with SessionLocal() as s:
        return list(s.scalars(select(Document.id)))


# --------------------------------------------------------------------------- list page


def test_list_page_shows_types_and_notice(client):
    r = client.get("/schreiben")
    assert r.status_code == 200
    html = unescape(r.text)
    assert NOTICE in html
    for label in ("Geltendmachung", "Überlastungsanzeige", "Bitte um Priorisierung",
                  "Antrag Freizeitausgleich / Mehrarbeitsvergütung"):
        assert label in html
    for t in docs.TYPES:
        assert f'href="/schreiben/neu?typ={t}"' in html
    assert "Staatliches Schulamt" in html and "Abdruck Personalrat" in html
    assert "Noch keine Schreiben gespeichert." in html
    assert "/static/css/documents.css" in html
    assert "<script>" not in html.replace("<script src", "")  # no inline scripts (CSP)


def test_new_without_period_shows_error(client):
    r = client.get("/schreiben/neu?typ=geltendmachung")
    assert r.status_code == 200
    assert "Es ist noch kein Schuljahr angelegt" in unescape(r.text)
    assert 'action="/schreiben/neu" class="card stack-sm documents-edit' not in r.text


def test_new_unknown_type_404(client):
    assert client.get("/schreiben/neu?typ=quatsch").status_code == 404
    assert client.get("/schreiben/neu").status_code == 404


def test_new_page_renders_preview_and_form(client):
    setup_year()
    add_entry(last_school_day(), 9)
    r = client.get("/schreiben/neu?typ=geltendmachung&ort=Musterstadt")
    assert r.status_code == 200
    html = unescape(r.text)
    assert 'class="card letter"' in html
    assert "<blockquote>" in html and NOTICE in html
    assert "Schriftliche Geltendmachung" in html
    assert "Musterstadt, " in html
    assert 'name="text"' in html and 'hx-post="/schreiben/vorschau"' in html
    assert 'name="von"' in html and 'name="bis"' in html
    assert "{{saldo_periode_h}}" in html  # placeholder documentation table
    assert "/static/js/documents.js" in html


def test_antrag_page_has_month_field(client):
    setup_year()
    r = client.get("/schreiben/neu?typ=antrag_freizeitausgleich")
    assert r.status_code == 200
    assert 'type="month" name="monat"' in r.text
    assert "Antrag auf Freizeitausgleich" in unescape(r.text)


def test_preview_partial_escapes_html(client):
    r = client.post("/schreiben/vorschau", data={"text": "**fett** <img src=x onerror=alert(1)>"})
    assert r.status_code == 200
    assert "<strong>fett</strong>" in r.text
    assert "<img" not in r.text
    assert NOTICE in unescape(r.text)


def test_save_edit_download_and_mark_sent(client):
    setup_year()
    add_entry(last_school_day(), 9)
    r = client.post("/schreiben/neu", data={
        "typ": "geltendmachung", "ort": "Musterstadt", "text": "Mein **bearbeiteter** Text", "notiz": "an SL gegeben",
    }, follow_redirects=False)
    assert r.status_code == 303
    doc_id = r.headers["location"].rsplit("/", 1)[1]
    assert doc_ids() == [doc_id]

    page = client.get(f"/schreiben/{doc_id}")
    assert page.status_code == 200
    html = unescape(page.text)
    assert "Schreiben gespeichert" in html  # flash
    assert "<strong>bearbeiteter</strong>" in html
    assert NOTICE in html  # re-inserted although the edited text dropped it
    assert "data-print" in html and "Download .md" in html
    assert "Als versendet markieren" in html and "Entwurf" in html
    assert "an SL gegeben" in html

    dl = client.get(f"/schreiben/{doc_id}/download")
    assert dl.status_code == 200
    assert dl.headers["content-type"].startswith("text/markdown")
    assert "attachment" in dl.headers["content-disposition"]
    assert dl.text.startswith("> **Hinweis:** " + NOTICE)

    sent_on = today_local().replace(day=1)
    r = client.post(f"/schreiben/{doc_id}/versendet", data={"versendet_am": sent_on.isoformat()},
                    follow_redirects=True)
    html = plain(r.text)
    wirksam = docs.first_of_next_month(sent_on)
    assert f"Geltendmachung wirksam ab {wirksam:%d.%m.%Y}" in html
    assert "Als versendet markieren" not in html
    assert f"versendet {sent_on:%d.%m.%Y}" in html

    listing = plain(client.get("/schreiben").text)
    assert f"Geltendmachung wirksam ab {wirksam:%d.%m.%Y}" in listing
    assert f'href="/schreiben/{doc_id}"' in client.get("/schreiben").text


def test_mark_sent_future_date_is_refused(client):
    setup_year()
    with SessionLocal() as s:
        doc = docs.save(s, "prioritaetsbitte", "Text")
        doc_id = doc.id
    future = today_local() + timedelta(days=5)
    r = client.post(f"/schreiben/{doc_id}/versendet", data={"versendet_am": future.isoformat()}, follow_redirects=True)
    assert "Zukunft" in unescape(r.text)
    with SessionLocal() as s:
        assert s.get(Document, doc_id).sent_at is None


def test_save_without_period_rerenders_with_error(client):
    r = client.post("/schreiben/neu", data={"typ": "geltendmachung", "text": "x"})
    assert r.status_code == 422
    assert "Schuljahr" in unescape(r.text)
    assert doc_ids() == []


def test_detail_unknown_404(client):
    assert client.get("/schreiben/gibtsnicht").status_code == 404
    assert client.get("/schreiben/gibtsnicht/download").status_code == 404


# --------------------------------------------------------------------------- reminders slot


def test_base_contains_reminder_slot(client):
    html = client.get("/schreiben").text
    assert 'id="reminders"' in html
    assert re.search(r'hx-get="/hinweise\?seite=/schreiben"', html)
    assert 'hx-trigger="load, deputat-refresh from:body"' in html
    assert "/static/js/reminders.js" in html


def test_hinweise_partial(client):
    r = client.get("/hinweise")
    assert r.status_code == 200
    assert 'data-reminder="keine-periode"' in r.text
    assert "data-reminder-close" in r.text
    assert r.headers["cache-control"] == "no-store"
    # the hint linking to the current page is not repeated there
    assert "keine-periode" not in client.get("/hinweise?seite=/kalender/einrichtung").text


def test_hinweise_school_day_without_entries(client):
    setup_year()
    day = last_school_day()
    if (today_local() - day).days > 4:  # pragma: no cover - long holiday weekend
        return
    with SessionLocal() as s:
        from app.db.models import CalendarDay

        row = s.get(CalendarDay, day)
        is_school = row is not None and row.day_type == "schultag"
    r = client.get("/hinweise?seite=/heute")
    if is_school:
        assert f"tag-ohne-eintrag:{day.isoformat()}" in r.text
        assert f"/eintraege/neu?datum={day.isoformat()}" in r.text


# --------------------------------------------------------------------------- JSON API


def test_api_types_and_list(client):
    r = client.get("/api/documents/types")
    assert r.status_code == 200
    data = r.json()
    assert {t["type"] for t in data["types"]} == set(docs.TYPES)
    assert "saldo_periode_h" in data["placeholders"]
    assert data["notice"] == NOTICE
    r = client.get("/api/documents")
    assert r.json() == {"documents": [], "geltendmachung_wirksam_ab": None}
    assert client.get("/api/documents?type=quatsch").status_code == 422


def test_api_create_preview_sent_download(client):
    setup_year()
    add_entry(last_school_day(), 9)
    r = client.post("/api/documents/ueberlastungsanzeige", json={"save": False, "overrides": {"ort": "Musterstadt"}})
    assert r.status_code == 200
    body = r.json()
    assert body["document"] is None and body["markdown"].startswith("> **Hinweis:** " + NOTICE)
    assert doc_ids() == []

    r = client.post("/api/documents/geltendmachung", json={"overrides": {"ort": "Musterstadt"}, "note": "n"})
    assert r.status_code == 200
    doc = r.json()["document"]
    assert doc["type"] == "geltendmachung" and doc["rendered_md"].startswith("> **Hinweis:**")
    assert doc["sent_at"] is None

    r = client.post(f"/api/documents/{doc['id']}/sent", json={"sent_on": today_local().isoformat()})
    assert r.status_code == 200
    assert r.json()["geltendmachung_wirksam_ab"] == docs.first_of_next_month(today_local()).isoformat()

    listed = client.get("/api/documents?type=geltendmachung").json()
    assert [d["id"] for d in listed["documents"]] == [doc["id"]]
    assert "rendered_md" not in listed["documents"][0]
    assert client.get(f"/api/documents/{doc['id']}").json()["rendered_md"].startswith("> **Hinweis:**")
    assert client.get(f"/api/documents/{doc['id']}/download").text.startswith("> **Hinweis:**")


def test_api_errors(client):
    assert client.post("/api/documents/quatsch", json={}).status_code == 404
    assert client.post("/api/documents/geltendmachung", json={}).status_code == 422  # no period
    assert client.get("/api/documents/nope").status_code == 404
    assert client.post("/api/documents/nope/sent", json={}).status_code == 404
    setup_year()
    r = client.post("/api/documents/antrag_freizeitausgleich", json={"monat": "2026-13"})
    assert r.status_code == 422 and "JJJJ-MM" in r.json()["detail"]
