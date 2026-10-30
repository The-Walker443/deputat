"""Web tests for the import wizard (app/web/import_.py): upload, preview, mapping, check, commit, result, undo."""

from __future__ import annotations

import io
import re
from html import unescape

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.db.models import CategoryMapping, TimeEntry
from app.db.session import SessionLocal

CSV = (
    "Datum;Von;Bis;Dauer;Tätigkeit;Klasse\n"
    "01.10.2026;;;2,5;Korrektur;3b\n"
    "02.10.2026;08:00;09:30;;Unterricht;\n"
    "05.10.2026;;;1:15;Elternabend Vorbereitung;\n"
    "kaputt;;;1;Korrektur;\n"
)


NO_PERIOD_TESTS = {"test_result_links_month_or_entry_list"}


@pytest.fixture(autouse=True)
def _school_year(request):
    """Rows outside every period are not imported by default: create the school year 2026/27."""
    if "client" in request.fixturenames and request.node.originalname not in NO_PERIOD_TESTS:
        from app.services import calendar_service as cal

        request.getfixturevalue("client")
        with SessionLocal() as s:
            cal.create_period(s, "2026/27")
            s.commit()


def text_of(html: str) -> str:
    return unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)))


def upload(client, content: bytes | str = CSV, filename: str = "zeiten.csv"):
    if isinstance(content, str):
        content = content.encode("cp1252")
    return client.post("/import", files={"datei": (filename, content, "application/octet-stream")},
                       follow_redirects=False)


def batch_id_from(response) -> str:
    assert response.status_code == 303, response.text
    return response.headers["location"].rsplit("/", 1)[1]


def mapping_form(**extra) -> dict:
    data = {"map_datum": "0", "map_von": "1", "map_bis": "2", "map_dauer": "3", "map_kategorie": "4",
            "map_klasse": "5", "map_fach": "", "map_notiz": "", "map_ort": "", "kopfzeile": "",
            "aus_zuordnung": "1"}
    data.update(extra)
    return data


def batch_entries(batch_id: str) -> list[TimeEntry]:
    with SessionLocal() as s:
        return list(s.scalars(select(TimeEntry).where(TimeEntry.import_batch_id == batch_id,
                                                      TimeEntry.deleted_at.is_(None))))


# --------------------------------------------------------------------------- step 1


def test_start_page(client):
    r = client.get("/import")
    assert r.status_code == 200
    t = text_of(r.text)
    assert 'enctype="multipart/form-data"' in r.text and 'name="datei"' in r.text
    assert "Noch keine Importe" in t and 'aria-current="step"' in r.text
    assert "/static/js/import.js" in r.text


def test_upload_requires_file_and_rejects_garbage(client):
    r = client.post("/import", data={})
    assert r.status_code == 422 and "CSV- oder XLSX-Datei" in r.text
    r = upload(client, b"", "leer.csv")
    assert r.status_code == 422 and "leer" in text_of(r.text)


def test_upload_redirects_to_preview(client):
    batch_id = batch_id_from(upload(client))
    r = client.get(f"/import/{batch_id}")
    assert r.status_code == 200
    t = text_of(r.text)
    assert "zeiten.csv" in t and "cp1252" in t and "Semikolon" in t
    assert "Korrektur" in t and "2,5" in t and "Elternabend Vorbereitung" in t
    assert f'action="/import/{batch_id}/zuordnung"' in r.text
    assert client.get("/import/unbekannt").status_code == 404


def test_xlsx_sheet_selection(client):
    wb = Workbook()
    wb.remove(wb.active)
    for name, day in (("Oktober", "01.10.2026"), ("November", "02.11.2026")):
        ws = wb.create_sheet(name)
        ws.append(["Datum", "Dauer", "Kategorie"])
        ws.append([day, "2", "Korrektur"])
    buf = io.BytesIO()
    wb.save(buf)
    batch_id = batch_id_from(upload(client, buf.getvalue(), "zeiten.xlsx"))
    r = client.get(f"/import/{batch_id}")
    assert r.status_code == 200
    assert 'name="blatt" value="Oktober" checked' in r.text and 'name="blatt" value="November"' in r.text
    r = client.get(f"/import/{batch_id}", params={"blatt": ["Oktober", "November"], "ansicht": "November"})
    assert "02.11.2026" in r.text

    # both sheets through to the check
    r = client.post(f"/import/{batch_id}/pruefen",
                    data={"blatt": ["Oktober", "November"], "map_datum": "0", "map_dauer": "1", "map_kategorie": "2",
                          "kopfzeile": "", "aus_zuordnung": "1"})
    assert r.status_code == 200, r.text
    t = text_of(r.text)
    assert "Oktober 2026" in t and "November 2026" in t


# --------------------------------------------------------------------------- step 2


def test_mapping_page_preselects_guess(client):
    batch_id = batch_id_from(upload(client))
    r = client.get(f"/import/{batch_id}/zuordnung")
    assert r.status_code == 200
    assert re.search(r'name="map_datum"[^>]*>\s*<option value="">[^<]*</option>\s*<option value="0" selected>', r.text)
    assert '<option value="4" selected>' in r.text  # Tätigkeit -> Kategorie
    assert 'name="standard_kategorie"' in r.text and "Pflicht" in r.text


def test_invalid_mapping_stays_on_step_2(client):
    batch_id = batch_id_from(upload(client))
    r = client.post(f"/import/{batch_id}/pruefen", data=mapping_form(map_dauer="", map_bis=""))
    assert r.status_code == 422
    assert "Von und Bis oder die Spalte Dauer" in r.text and 'name="map_datum"' in r.text


# --------------------------------------------------------------------------- step 3 + 4


def test_check_commit_result_and_undo(client):
    batch_id = batch_id_from(upload(client))
    r = client.post(f"/import/{batch_id}/pruefen", data=mapping_form())
    assert r.status_code == 200, r.text
    t = text_of(r.text)
    assert "Ergebnis der Prüfung" in t and "Summen je Monat" in t and "Oktober 2026" in t
    assert "„Elternabend Vorbereitung“" in t  # unmapped category text in the mapping table
    assert 'class="import-row is-fehler"' in r.text  # broken date row highlighted
    assert f'formaction="/import/{batch_id}/uebernehmen"' in r.text

    # map the unknown text, re-check: no unmapped categories left
    texts = re.findall(r'name="kat_text" value="([^"]*)"', r.text)
    origs = re.findall(r'name="kat_orig" value="([^"]*)"', r.text)
    codes = ["KV" if unescape(tx) == "Elternabend Vorbereitung" else o for tx, o in zip(texts, origs)]
    form = mapping_form(optionen="1", merken="1", duplikate_ueberspringen="1",
                        kat_text=[unescape(x) for x in texts], kat_orig=origs, kat_code=codes)
    r = client.post(f"/import/{batch_id}/pruefen", data=form)
    assert r.status_code == 200
    assert "ohne Zuordnung" not in text_of(r.text)
    assert "3 Zeilen übernehmen" in text_of(r.text)

    # commit
    r = client.post(f"/import/{batch_id}/uebernehmen", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/import/{batch_id}/ergebnis"
    assert len(batch_entries(batch_id)) == 3
    with SessionLocal() as s:
        assert s.get(CategoryMapping, "Elternabend Vorbereitung") is not None

    r = client.get(f"/import/{batch_id}/ergebnis")
    assert r.status_code == 200
    t = text_of(r.text)
    assert "Import übernommen" in t and "3 Zeile(n) übernommen" in t and "Oktober 2026" in t
    assert "Import vom" in t and "zeiten.csv" in t

    # steps 1-3 are closed now; commit again only redirects
    assert client.get(f"/import/{batch_id}/zuordnung", follow_redirects=False).status_code == 303
    r = client.post(f"/import/{batch_id}/uebernehmen", data=form, follow_redirects=False)
    assert r.status_code == 303 and len(batch_entries(batch_id)) == 3

    # overview lists the batch + remembered mapping
    r = client.get("/import")
    assert "zeiten.csv" in r.text and f'href="/import/{batch_id}/rueckgaengig"' in r.text
    assert "Elternabend Vorbereitung" in r.text

    # undo needs confirmation
    r = client.get(f"/import/{batch_id}/rueckgaengig")
    assert r.status_code == 200 and "Import rückgängig machen?" in r.text
    r = client.post(f"/import/{batch_id}/rueckgaengig", data={})
    assert r.status_code == 422
    r = client.post(f"/import/{batch_id}/rueckgaengig", data={"bestaetigt": "1"}, follow_redirects=False)
    assert r.status_code == 303 and batch_entries(batch_id) == []
    r = client.get("/import")
    assert "Import rückgängig gemacht: 3 Einträge gelöscht" in text_of(r.text)
    assert "rückgängig" in r.text


def test_check_again_from_stored_state(client):
    batch_id = batch_id_from(upload(client))
    client.post(f"/import/{batch_id}/pruefen", data=mapping_form())
    r = client.get(f"/import/{batch_id}/pruefen")
    assert r.status_code == 200 and "Ergebnis der Prüfung" in r.text
    assert 'name="map_dauer" value="3"' in r.text


def test_exclude_and_duplicates(client):
    batch_id = batch_id_from(upload(client))
    form = mapping_form(optionen="1", duplikate_ueberspringen="1", ausschliessen=["2"])
    r = client.post(f"/import/{batch_id}/pruefen", data=form)
    assert r.status_code == 200
    assert 'name="ausschliessen" value="2" checked' in r.text
    r = client.post(f"/import/{batch_id}/uebernehmen", data=form, follow_redirects=False)
    assert r.status_code == 303
    assert len(batch_entries(batch_id)) == 1  # row 2 excluded, row 4 unmapped category, row 5 broken

    # the same file again: the imported row is now a duplicate
    second = batch_id_from(upload(client))
    r = client.post(f"/import/{second}/pruefen", data=mapping_form())
    assert 'class="import-row is-duplikat"' in r.text and 'name="einschliessen"' in r.text


def test_discard_unfinished_upload(client):
    batch_id = batch_id_from(upload(client))
    r = client.get(f"/import/{batch_id}/rueckgaengig")
    assert "Upload verwerfen?" in r.text
    r = client.post(f"/import/{batch_id}/rueckgaengig", data={"bestaetigt": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get(f"/import/{batch_id}").status_code == 404


def test_forget_mapping(client):
    with SessionLocal() as s:
        s.add(CategoryMapping(source_text="Klausur", category_code="K"))
        s.commit()
    r = client.get("/import")
    assert "Klausur" in r.text
    r = client.post("/import/zuordnungen/loeschen", data={"source_text": "Klausur"}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as s:
        assert s.get(CategoryMapping, "Klausur") is None


def test_check_enter_key_rechecks_instead_of_committing(client):
    batch_id = batch_id_from(upload(client))
    r = client.post(f"/import/{batch_id}/pruefen", data=mapping_form())
    form = r.text[r.text.index(f'action="/import/{batch_id}/pruefen"'):]
    # implicit submission (Enter in a text field) uses the first submit button: it must be the re-check
    first_button = re.search(r"<button[^>]*>", form).group(0)
    assert 'type="submit"' in first_button and "formaction" not in first_button
    assert form.index(first_button) < form.index(f'formaction="/import/{batch_id}/uebernehmen"')


def test_result_links_month_or_entry_list(client):
    from app.services import calendar_service as cal

    # no period: the rows lie outside every school year and need the explicit option
    form = mapping_form(optionen="1", duplikate_ueberspringen="1", ausserhalb_importieren="1")
    batch_id = batch_id_from(upload(client, CSV.replace("Elternabend Vorbereitung", "Korrektur")))
    assert client.post(f"/import/{batch_id}/uebernehmen", data=form, follow_redirects=False).status_code == 303
    # no period yet: /monate/2026-10 would be a 404, so the result links the entry list of the month
    r = client.get(f"/import/{batch_id}/ergebnis")
    assert 'href="/eintraege?von=2026-10-01&amp;bis=2026-10-31"' in r.text
    assert 'href="/monate/2026-10"' not in r.text

    with SessionLocal() as s:
        cal.create_period(s, "2026/27")
        s.commit()
    r = client.get(f"/import/{batch_id}/ergebnis")
    assert 'href="/monate/2026-10"' in r.text
    assert client.get("/monate/2026-10").status_code == 200
