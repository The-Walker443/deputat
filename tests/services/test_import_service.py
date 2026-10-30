"""Service tests for app/services/import_service.py: upload → validate → commit → undo (Konzept §12, T17)."""

from __future__ import annotations

import io
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.config import get_config
from app.db.models import Category, CategoryMapping, ImportBatch, MonthClose, TimeEntry, TimeEntryRevision
from app.services import entries as entries_svc
from app.services import import_service as svc

BERLIN = ZoneInfo("Europe/Berlin")
H = 3600
TODAY = date(2026, 10, 29)


@pytest.fixture(autouse=True)
def _school_year(request):
    """Rows outside every period are not imported by default: give the DB tests the school year 2026/27."""
    if "db" in request.fixturenames:
        from app.services import calendar_service as cal

        db = request.getfixturevalue("db")
        cal.create_period(db, "2026/27")
        db.commit()


def L(y, m, d, h=0, mi=0) -> datetime:  # noqa: N802 - local wall-clock helper
    return datetime(y, m, d, h, mi, tzinfo=BERLIN)


def utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None)


def cat(db, code: str) -> int:
    return db.scalar(select(Category.id).where(Category.code == code))


def csv_bytes(text: str, encoding: str = "utf-8") -> bytes:
    return text.encode(encoding)


def xlsx_bytes(sheets: dict[str, list[list]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def imported(db, batch_id: str, *, include_deleted: bool = False) -> list[TimeEntry]:
    q = select(TimeEntry).where(TimeEntry.import_batch_id == batch_id).order_by(TimeEntry.start_at)
    if not include_deleted:
        q = q.where(TimeEntry.deleted_at.is_(None))
    return list(db.scalars(q))


def rows_by_ref(report) -> dict[str, dict]:
    return {r["ref"]: r for r in report["rows"]}


def upload_validate(db, content: bytes, filename: str = "zeiten.csv", mapping=None, **kwargs):
    preview = svc.upload(db, content, filename)
    report = svc.validate(db, preview["batch_id"], mapping or preview["mapping"], today=TODAY, **kwargs)
    return preview, report


# --------------------------------------------------------------------------- T17 / CSV with ; and decimal comma


def test_t17_duration_without_times_becomes_unbounded_entry(db):
    content = csv_bytes("Datum;Stunden;Tätigkeit\n01.10.2026;2,5;Korrektur\n", "cp1252")
    preview, report = upload_validate(db, content)
    assert preview["encoding"] == "cp1252" and preview["delimiter"] == ";"
    assert report["summary"]["ok"] == 1 and report["can_commit"]

    result = svc.commit(db, preview["batch_id"])
    assert result["imported_rows"] == 1 and result["entries_created"] == 1

    (entry,) = imported(db, preview["batch_id"])
    assert entry.duration_seconds == 2 * H + 30 * 60  # 2:30
    assert entry.has_bounds is False
    assert "ohne_zeitgrenzen" in entry.flags
    assert entry.source == "import"
    assert entry.day == date(2026, 10, 1)
    assert entry.start_at == utc(L(2026, 10, 1))  # local 00:00
    assert entry.category.code == "K"


def test_csv_semicolon_decimal_comma_full_columns(db):
    content = csv_bytes(
        "Datum;Von;Bis;Dauer;Kategorie;Klasse;Fach;Bemerkung;Ort\n"
        "01.10.2026;;;2,5;K;3b;Deutsch;Proben;zu Hause\n"
        "02.10.2026;08:00;09:30;;U;3B;Mathematik;;Schule\n"
        "05.10.2026;;;0,75;VN;;;Material;\n"
    )
    preview, report = upload_validate(db, content)
    assert preview["mapping"] == {"datum": 0, "von": 1, "bis": 2, "dauer": 3, "kategorie": 4, "klasse": 5,
                                  "fach": 6, "notiz": 7, "ort": 8}
    rows = rows_by_ref(report)
    assert rows["2"]["duration_seconds"] == 9000 and rows["2"]["location"] == "zuhause"
    assert rows["3"]["von"] == "08:00" and rows["3"]["bis"] == "09:30" and rows["3"]["has_bounds"]
    assert rows["3"]["klasse"] == "3b"  # case-insensitive match against settings.klassen
    assert rows["4"]["duration_seconds"] == 45 * 60
    assert report["summary"]["importieren"] == 3
    assert report["summary"]["entries_bounded"] == 1 and report["summary"]["entries_unbounded"] == 2

    svc.commit(db, preview["batch_id"])
    entries = imported(db, preview["batch_id"])
    assert len(entries) == 3
    bounded = next(e for e in entries if e.has_bounds)
    assert bounded.start_at == utc(L(2026, 10, 2, 8)) and bounded.end_at == utc(L(2026, 10, 2, 9, 30))
    assert "ohne_zeitgrenzen" not in bounded.flags
    first = next(e for e in entries if e.day == date(2026, 10, 1))
    assert (first.klasse, first.fach, first.note, first.location) == ("3b", "Deutsch", "Proben", "zuhause")


def test_original_file_is_stored_under_data_dir(db):
    content = csv_bytes("Datum;Dauer;Kategorie\n01.10.2026;1;K\n")
    preview = svc.upload(db, content, "C:\\Users\\x\\Meine Zeiten.csv")
    batch = db.get(ImportBatch, preview["batch_id"])
    path = get_config().imports_dir / batch.id / "original.csv"
    assert path.read_bytes() == content
    assert batch.stored_file == str(path)
    assert batch.filename == "Meine Zeiten.csv"
    assert batch.mapping_json["stage"] == "hochgeladen"
    assert preview["rows_total"] == 1 and len(preview["rows"]) == 1
    assert preview["columns"][1]["samples"] == ["1"]


def test_preview_limited_to_50_rows(db):
    lines = "".join(f"{(date(2026, 9, 1) + timedelta(days=i)).strftime('%d.%m.%Y')};1;K\n" for i in range(60))
    preview = svc.upload(db, csv_bytes("Datum;Dauer;Kategorie\n" + lines), "viele.csv")
    assert preview["rows_total"] == 60
    assert len(preview["rows"]) == 50
    assert preview["rows"][0] == {"row_no": 2, "cells": ["01.09.2026", "1", "K"]}


def test_utf8_bom_comma_and_quoted_decimals(db):
    content = b"\xef\xbb\xbf" + 'Datum,Dauer,Kategorie\n"01.10.2026","2,5",Elternarbeit\n'.encode()
    preview, report = upload_validate(db, content)
    assert (preview["encoding"], preview["delimiter"]) == ("utf-8-sig", ",")
    assert report["rows"][0]["duration_seconds"] == 9000
    assert report["rows"][0]["category_code"] == "E"


def test_hmm_durations_and_other_formats(db):
    content = csv_bytes(
        "Datum;Dauer;Kategorie\n"
        "01.10.2026;1:45;K\n"
        "2026-10-02;0:30;K\n"
        "Fr, 02.10.2026;02:15:00;VN\n"
        "05.10.26;90 min;VN\n"
        "06.10.2026;2.25;U\n"
    )
    _, report = upload_validate(db, content)
    assert [r["duration_seconds"] for r in report["rows"]] == [105 * 60, 30 * 60, 135 * 60, 90 * 60, 135 * 60]
    assert [r["day"] for r in report["rows"]] == ["2026-10-01", "2026-10-02", "2026-10-02", "2026-10-05",
                                                    "2026-10-06"]
    assert all(r["status"] == "ok" for r in report["rows"])


# --------------------------------------------------------------------------- XLSX


def test_xlsx_real_time_and_date_cells(db):
    content = xlsx_bytes({
        "Notizen": [["irgendwas"]],
        "Oktober": [
            ["Arbeitszeiterfassung Oktober"],
            [],
            ["Datum", "Von", "Bis", "Dauer", "Tätigkeit", "Klasse"],
            [datetime(2026, 10, 1), time(7, 45), time(9, 15), None, "Unterricht", "4a"],
            [date(2026, 10, 2), None, None, time(2, 30), "Korrektur", None],
            [datetime(2026, 10, 5), None, None, timedelta(hours=1, minutes=10), "VN", None],
            [datetime(2026, 10, 6), datetime(2026, 10, 6, 14, 0), None, 1.5, "Konferenzen", None],
        ],
    })
    preview = svc.upload(db, content, "zeiten.xlsx", sheet="Oktober")
    assert preview["file_type"] == "xlsx"
    assert preview["sheets"] == ["Notizen", "Oktober"]
    assert preview["sheet"] == "Oktober" and preview["header_row"] == 3
    mapping = preview["mapping"]
    assert (mapping["datum"], mapping["von"], mapping["bis"], mapping["dauer"], mapping["kategorie"]) == (0, 1, 2, 3, 4)

    report = svc.validate(db, preview["batch_id"], mapping, sheets="Oktober", today=TODAY)
    rows = rows_by_ref(report)
    assert set(rows) == {"Oktober!4", "Oktober!5", "Oktober!6", "Oktober!7"}
    assert rows["Oktober!4"]["von"] == "07:45" and rows["Oktober!4"]["duration_seconds"] == 90 * 60
    assert rows["Oktober!5"]["duration_seconds"] == 9000 and not rows["Oktober!5"]["has_bounds"]
    assert rows["Oktober!6"]["duration_seconds"] == 70 * 60
    assert rows["Oktober!7"]["von"] == "14:00" and rows["Oktober!7"]["bis"] == "15:30"
    assert rows["Oktober!7"]["category_code"] == "KO"  # alias from "Konferenzen & Zusammenarbeit"
    assert report["summary"]["fehler"] == 0

    result = svc.commit(db, preview["batch_id"])
    assert result["imported_rows"] == 4
    entries = imported(db, preview["batch_id"])
    assert entries[0].start_at == utc(L(2026, 10, 1, 7, 45))
    assert entries[0].klasse == "4a"


def test_xlsx_duration_cell_with_datetime_format(db):
    content = xlsx_bytes({"T": [["Datum", "Dauer", "Kategorie"],
                                [datetime(2026, 10, 1), datetime(1899, 12, 30, 2, 30), "K"],
                                [datetime(2026, 10, 2), datetime(2026, 10, 2, 2, 30), "K"]]})
    _, report = upload_validate(db, content, "d.xlsx")
    assert report["rows"][0]["duration_seconds"] == 9000 and report["rows"][0]["status"] == "ok"
    assert report["rows"][1]["status"] == "fehler"  # a real date is not a duration


def test_xlsx_multiple_sheets_with_header_names(db):
    rows_okt = [["Datum", "Dauer", "Kategorie"], [datetime(2026, 10, 1), 2, "K"]]
    rows_nov = [["Kategorie", "Datum", "Dauer"], ["VN", datetime(2026, 9, 30), 1]]  # other column order
    preview = svc.upload(db, xlsx_bytes({"Okt": rows_okt, "Sep": rows_nov}), "jahr.xlsx")
    assert preview["sheet"] == "Okt"
    mapping = {"datum": "Datum", "dauer": "Dauer", "kategorie": "Kategorie"}
    report = svc.validate(db, preview["batch_id"], mapping, sheets=["Okt", "Sep"], today=TODAY)
    assert [(r["ref"], r["category_code"], r["day"]) for r in report["rows"]] == [
        ("Okt!2", "K", "2026-10-01"), ("Sep!2", "VN", "2026-09-30")]
    assert [m["month"] for m in report["months"]] == ["2026-09", "2026-10"]
    result = svc.commit(db, preview["batch_id"])
    assert result["imported_rows"] == 2
    assert db.get(ImportBatch, preview["batch_id"]).mapping_json["selected_sheets"] == ["Okt", "Sep"]


def test_preview_other_sheet_and_unknown_sheet(db):
    preview = svc.upload(db, xlsx_bytes({"A": [["Datum", "Dauer"]], "B": [["x", "y", "z"]]}), "m.xlsx")
    other = svc.preview(db, preview["batch_id"], sheet="B")
    assert other["sheet"] == "B" and len(other["headers"]) == 3
    with pytest.raises(svc.ImportValidationError):
        svc.preview(db, preview["batch_id"], sheet="C")
    with pytest.raises(svc.ImportValidationError):
        svc.upload(db, xlsx_bytes({"A": [["x"]]}), "m.xlsx", sheet="C")


# --------------------------------------------------------------------------- row validation


def test_bounds_variants_and_errors(db):
    content = csv_bytes(
        "Datum;Von;Bis;Dauer;Kategorie\n"
        "01.10.2026;22:30;01:15+1;;K\n"        # over midnight, explicit next-day notation
        "02.10.2026;08:00;;1:30;U\n"           # von + dauer
        "02.10.2026;;12:00;0:45;VN\n"          # bis + dauer
        "05.10.2026;08:00;12:00;3:30;U\n"      # dauer mismatch
        "05.10.2026;08:00;08:00;;U\n"          # same von/bis
        "06.10.2026;08:00;;;U\n"               # missing bis/dauer
        "07.10.2026;25:00;26:00;;U\n"          # invalid time
        "xx.10.2026;;;2;U\n"                   # invalid date
        ";;;2;U\n"                             # missing date
        "08.10.2026;;;abc;U\n"                 # invalid duration
        "09.10.2026;;;25;U\n"                  # > 24 h
        "12.10.2026;14:00;24:00;;K\n"          # end of day
        "13.10.2026;10:00;09:00;;U\n"          # Bis before Von without +1: error, not 23 h
        "13.10.2026;22:00;01:00;3:00;K\n"      # overnight confirmed by the Dauer column
        "14.10.2026;08:00;09:00+1;;K\n"        # explicit +1: 25 h -> too long
    )
    _, report = upload_validate(db, content)
    rows = rows_by_ref(report)
    assert rows["2"]["status"] == "ok" and rows["2"]["duration_seconds"] == 2 * H + 45 * 60
    assert any("Mitternacht" in w for w in rows["2"]["warnings"])
    assert (rows["3"]["von"], rows["3"]["bis"]) == ("08:00", "09:30")
    assert (rows["4"]["von"], rows["4"]["bis"]) == ("11:15", "12:00")
    assert "passt nicht" in rows["5"]["errors"][0]
    assert "gleich" in rows["6"]["errors"][0]
    assert "Bis oder Dauer fehlt" in rows["7"]["errors"][0]
    assert rows["8"]["status"] == "fehler" and "Von" in rows["8"]["errors"][0]
    assert "Datum" in rows["9"]["errors"][0]
    assert rows["10"]["errors"] == ["Datum fehlt."]
    assert rows["11"]["errors"][0].startswith("Dauer:")
    assert "24 Stunden" in rows["12"]["errors"][0]
    assert rows["13"]["status"] == "ok" and rows["13"]["duration_seconds"] == 10 * H
    assert rows["14"]["status"] == "fehler" and "liegt vor Von" in rows["14"]["errors"][0]
    assert "09:00+1" in rows["14"]["errors"][0]
    assert rows["15"]["status"] == "ok" and rows["15"]["duration_seconds"] == 3 * H
    assert "24 Stunden" in rows["16"]["errors"][0]
    assert report["summary"]["fehler"] == 10


def test_over_midnight_row_creates_two_entries(db):
    content = csv_bytes("Datum;Von;Bis;Kategorie\n01.10.2026;22:30;01:15 (+1);K\n")
    preview, _ = upload_validate(db, content)
    result = svc.commit(db, preview["batch_id"])
    assert result["imported_rows"] == 1 and result["entries_created"] == 2
    entries = imported(db, preview["batch_id"])
    assert [e.day for e in entries] == [date(2026, 10, 1), date(2026, 10, 2)]
    assert sum(e.duration_seconds for e in entries) == 2 * H + 45 * 60
    assert all(e.import_batch_id == preview["batch_id"] for e in entries)


def test_empty_sum_and_comment_rows(db):
    content = csv_bytes(
        "Datum;Dauer;Kategorie;Bemerkung\n"
        "01.10.2026;2;K;\n"
        "03.10.2026;;;Feiertag\n"
        "04.10.2026;0;;\n"
        "Summe KW 40;2;;\n"
        ";;;nur ein Kommentar\n"
        "05.10.2026;1;KO;Gesamtkonferenz\n"
    )
    _, report = upload_validate(db, content)
    statuses = [r["status"] for r in report["rows"]]
    assert statuses == ["ok", "leer", "leer", "ignoriert", "leer", "ok"]
    assert report["summary"]["leer"] == 3 and report["summary"]["ignoriert"] == 1
    assert report["summary"]["seconds_file"] == 3 * H  # sum row not counted


def test_attributes_unknown_klasse_ort_and_required_note(db):
    content = csv_bytes(
        "Datum;Dauer;Kategorie;Klasse;Ort;Notiz\n"
        "01.10.2026;1;K;4c;Bibliothek;Hefte\n"
        "02.10.2026;1;S;;;\n"
        "02.10.2026;1;S;;;Inventur\n"
    )
    preview, report = upload_validate(db, content)
    rows = rows_by_ref(report)
    assert rows["2"]["klasse"] is None and rows["2"]["location"] is None
    assert rows["2"]["note"] == "Hefte · Klasse 4c · Ort: Bibliothek"
    assert len(rows["2"]["warnings"]) == 2 and rows["2"]["status"] == "ok"
    assert "Notiz Pflicht" in rows["3"]["errors"][0]
    assert rows["4"]["status"] == "ok"
    svc.commit(db, preview["batch_id"])
    notes = sorted(e.note for e in imported(db, preview["batch_id"]))
    assert notes == ["Hefte · Klasse 4c · Ort: Bibliothek", "Inventur"]


def test_future_date_warning(db):
    _, report = upload_validate(db, csv_bytes("Datum;Dauer;Kategorie\n01.12.2026;1;K\n"))
    assert report["rows"][0]["status"] == "ok"
    assert "Zukunft" in report["rows"][0]["warnings"][0]


def test_inactive_category_is_error(db):
    db.get(Category, cat(db, "DW")).active = False
    db.commit()
    _, report = upload_validate(db, csv_bytes("Datum;Dauer;Kategorie\n01.10.2026;1;DW\n"))
    assert "deaktiviert" in report["rows"][0]["errors"][0]


# --------------------------------------------------------------------------- categories / mapping memory


def test_catalogue_matching_by_code_name_alias_and_subcategory(db):
    content = csv_bytes(
        "Datum;Dauer;Kategorie\n"
        "01.10.2026;1;vn\n"
        "01.10.2026;2;Elternarbeit\n"
        "01.10.2026;3;Elternabend\n"
        "01.10.2026;4;aufsichten\n"
        "01.10.2026;5;U.2\n"
    )
    _, report = upload_validate(db, content)
    assert [r["category_code"] for r in report["rows"]] == ["VN", "E", "E.1", "AV", "U.2"]
    assert {r["category_source"] for r in report["rows"]} == {"katalog"}


def test_unknown_category_needs_mapping_and_is_remembered(db):
    content = csv_bytes(
        "Datum;Dauer;Tätigkeit\n"
        "01.10.2026;1;Hefte korrigieren\n"
        "02.10.2026;2;hefte  KORRIGIEREN\n"
        "02.10.2026;1;Elterngespräch\n"
    )
    preview, report = upload_validate(db, content)
    assert report["summary"]["fehler"] == 3
    unmapped = {u["text"]: u for u in report["unmapped_categories"]}
    assert unmapped["Hefte korrigieren"]["count"] == 2  # normalised text groups both spellings
    assert unmapped["Hefte korrigieren"]["rows"] == ["2", "3"]
    assert not report["can_commit"]

    options = {"category_map": {"Hefte korrigieren": "K.2", "Elterngespräch": "E"}}
    report = svc.validate(db, preview["batch_id"], preview["mapping"], options=options, today=TODAY)
    assert report["summary"]["ok"] == 3 and report["unmapped_categories"] == []
    assert {r["category_source"] for r in report["rows"]} == {"manuell"}

    result = svc.commit(db, preview["batch_id"])  # uses stored mapping + options
    assert result["mappings_remembered"] == 2
    stored = {m.source_text: m.category_code for m in db.scalars(select(CategoryMapping))}
    assert stored == {"Hefte korrigieren": "K.2", "Elterngespräch": "E"}
    entry = imported(db, preview["batch_id"])[0]
    assert entry.category.code == "K" and entry.subcategory.code == "K.2"

    # Second import: remembered mapping (Merkfunktion) applies automatically
    _, report2 = upload_validate(db, csv_bytes("Datum;Dauer;Tätigkeit\n07.10.2026;1;HEFTE korrigieren\n"))
    assert report2["rows"][0]["category_code"] == "K.2"
    assert report2["rows"][0]["category_source"] == "gemerkt"
    assert report2["category_resolution"][0]["source"] == "gemerkt"


def test_remember_mappings_false_and_user_map_overrides_memory(db):
    db.add(CategoryMapping(source_text="Diverses", category_code="VN"))
    db.commit()
    preview, report = upload_validate(db, csv_bytes("Datum;Dauer;Tätigkeit\n01.10.2026;1;Diverses\n"),
                                      options={"category_map": {"diverses": "KV"}, "remember_mappings": False})
    assert report["rows"][0]["category_code"] == "KV"
    svc.commit(db, preview["batch_id"])
    assert db.get(CategoryMapping, "Diverses").category_code == "VN"


def test_unknown_code_in_user_map_and_default_category(db):
    content = csv_bytes("Datum;Dauer\n01.10.2026;1\n")
    preview = svc.upload(db, content, "ohne_kat.csv")
    with pytest.raises(svc.ImportValidationError, match="Standard-Kategorie"):
        svc.validate(db, preview["batch_id"], preview["mapping"])
    with pytest.raises(svc.ImportValidationError, match="Unbekannte Standard-Kategorie"):
        svc.validate(db, preview["batch_id"], preview["mapping"], options={"default_category": "ZZ"})
    with pytest.raises(svc.ImportValidationError, match="Unbekannter Kategorie-Code"):
        svc.validate(db, preview["batch_id"], preview["mapping"], options={"default_category": "K",
                                                                          "category_map": {"x": "ZZ"}})
    report = svc.validate(db, preview["batch_id"], preview["mapping"], options={"default_category": "vn"})
    assert report["rows"][0]["category_code"] == "VN" and report["rows"][0]["category_source"] == "standard"


def test_mapping_crud(db):
    saved = svc.save_mapping(db, "  Korrigieren ", "k")
    assert saved["source_text"] == "Korrigieren" and saved["category_code"] == "K"
    saved = svc.save_mapping(db, "korrigieren", "K.1")  # same normalised text → update
    assert saved["category_code"] == "K.1"
    assert [m["source_text"] for m in svc.list_mappings(db)] == ["Korrigieren"]
    with pytest.raises(svc.ImportValidationError):
        svc.save_mapping(db, "x", "nope")
    svc.delete_mapping(db, "KORRIGIEREN")
    assert svc.list_mappings(db) == []
    with pytest.raises(svc.ImportNotFound):
        svc.delete_mapping(db, "korrigieren")


# --------------------------------------------------------------------------- duplicates / overlaps


def test_duplicates_within_file(db):
    content = csv_bytes(
        "Datum;Von;Bis;Dauer;Kategorie\n"
        "01.10.2026;;;2,5;K\n"
        "01.10.2026;;;2:30;Korrektur\n"     # duplicate of row 2
        "01.10.2026;;;2,5;VN\n"             # other category
        "02.10.2026;08:00;08:45;;U\n"
        "02.10.2026;09:00;09:45;;U\n"       # same duration, different start → no duplicate
        "02.10.2026;;;0:45;U\n"             # unbounded vs bounded → duplicate of row 5
    )
    _, report = upload_validate(db, content)
    rows = rows_by_ref(report)
    assert [rows[str(i)]["status"] for i in range(2, 8)] == ["ok", "duplikat", "ok", "ok", "ok", "duplikat"]
    assert rows["3"]["duplicate_of"] == "2" and rows["7"]["duplicate_of"] == "5"
    assert rows["3"]["import"] is False
    assert report["summary"]["duplikat"] == 2 and report["summary"]["importieren"] == 4


def test_duplicates_against_existing_entries(db):
    entries_svc.create_entry(db, {"has_bounds": False, "day": date(2026, 10, 1), "duration_seconds": 9000,
                                  "category_id": cat(db, "K")}, source="manuell")
    (manual,) = entries_svc.create_entry(db, {"start_at": L(2026, 10, 2, 8), "end_at": L(2026, 10, 2, 9),
                                              "category_id": cat(db, "U")}, source="manuell")
    content = csv_bytes(
        "Datum;Von;Bis;Dauer;Kategorie\n"
        "01.10.2026;;;2,5;K\n"              # same as existing unbounded entry
        "02.10.2026;;;1;U\n"                # unbounded vs existing bounded 1 h U → duplicate
        "02.10.2026;08:00;09:00;;U\n"       # same start → duplicate (not an overlap error)
    )
    preview, report = upload_validate(db, content)
    rows = rows_by_ref(report)
    assert [rows[r]["status"] for r in ("2", "3", "4")] == ["duplikat"] * 3
    assert rows["4"]["duplicate_of"] == f"eintrag:{manual.id}"
    assert "bestehender Eintrag" in rows["2"]["warnings"][0]
    with pytest.raises(svc.ImportValidationError, match="Keine Zeilen"):
        svc.commit(db, preview["batch_id"])

    # skip_duplicates=False imports unbounded duplicates; the bounded one now collides → error
    report = svc.validate(db, preview["batch_id"], preview["mapping"], options={"skip_duplicates": False},
                          today=TODAY)
    rows = rows_by_ref(report)
    assert rows["2"]["import"] and rows["3"]["import"]
    assert rows["4"]["status"] == "fehler" and "Überschneidung" in rows["4"]["errors"][0]


def test_include_and_exclude_rows(db):
    content = csv_bytes("Datum;Dauer;Kategorie\n01.10.2026;1;K\n01.10.2026;1;K\n02.10.2026;1;K\n")
    preview, report = upload_validate(db, content, options={"include_rows": [3], "exclude_rows": ["4"]})
    rows = rows_by_ref(report)
    assert rows["3"]["status"] == "duplikat" and rows["3"]["import"] is True
    assert rows["4"]["excluded"] and not rows["4"]["import"]
    result = svc.commit(db, preview["batch_id"])
    assert result["imported_rows"] == 2
    assert result["skipped"]["ausgeschlossen"] == 1
    assert len(imported(db, preview["batch_id"])) == 2


def test_bounded_rows_overlapping_existing_or_each_other(db):
    entries_svc.create_entry(db, {"start_at": L(2026, 10, 1, 8), "end_at": L(2026, 10, 1, 10),
                                  "category_id": cat(db, "U")}, source="manuell")
    content = csv_bytes(
        "Datum;Von;Bis;Kategorie\n"
        "01.10.2026;09:00;11:00;K\n"     # overlaps the manual entry
        "01.10.2026;10:00;11:00;K\n"     # touches it (half-open) → fine
        "01.10.2026;10:30;12:00;VN\n"    # overlaps row 3
        "01.10.2026;12:00;13:00;VN\n"    # fine
    )
    preview, report = upload_validate(db, content)
    rows = rows_by_ref(report)
    assert rows["2"]["status"] == "fehler" and "bestehendem Eintrag" in rows["2"]["errors"][0]
    assert rows["3"]["status"] == "ok"
    assert rows["4"]["status"] == "fehler" and "Zeile 3" in rows["4"]["errors"][0]
    assert rows["5"]["status"] == "ok"
    result = svc.commit(db, preview["batch_id"])
    assert result["imported_rows"] == 2 and result["skipped"]["fehler"] == 2


def test_bounded_import_overlaps_running_timer(db):
    from app.services import timer

    timer.start(db, cat(db, "VN"), started_at=L(2026, 10, 28, 15), now=L(2026, 10, 28, 15, 1))
    _, report = upload_validate(db, csv_bytes("Datum;Von;Bis;Kategorie\n28.10.2026;16:00;17:00;K\n"))
    assert "laufenden Timer" in report["rows"][0]["errors"][0]


def test_unbounded_rows_are_exempt_from_overlap(db):
    entries_svc.create_entry(db, {"start_at": L(2026, 10, 1, 0), "end_at": L(2026, 10, 1, 6),
                                  "category_id": cat(db, "U")}, source="manuell")
    preview, report = upload_validate(db, csv_bytes("Datum;Dauer;Kategorie\n01.10.2026;3;K\n"))
    assert report["rows"][0]["status"] == "ok"
    assert svc.commit(db, preview["batch_id"])["imported_rows"] == 1


# --------------------------------------------------------------------------- monthly sums


def test_monthly_sums_file_vs_import(db):
    entries_svc.create_entry(db, {"has_bounds": False, "day": date(2026, 9, 10), "duration_seconds": 3600,
                                  "category_id": cat(db, "U")}, source="manuell")
    content = csv_bytes(
        "Datum;Dauer;Kategorie\n"
        "28.09.2026;2;K\n"
        "29.09.2026;1,5;Unbekannt\n"
        "01.10.2026;3;VN\n"
        "01.10.2026;3;VN\n"
    )
    _, report = upload_validate(db, content)
    sep, okt = report["months"]
    assert sep["month"] == "2026-09" and sep["label"] == "September 2026"
    assert sep["datei_seconds"] == int(3.5 * H) and sep["import_seconds"] == 2 * H
    assert sep["differenz_seconds"] == -int(1.5 * H) and sep["differenz_hm"] == "-1:30"
    assert sep["fehler"] == 1 and sep["bestehend_seconds"] == H
    assert okt["datei_seconds"] == 6 * H and okt["import_seconds"] == 3 * H and okt["duplikate"] == 1
    assert report["summary"]["seconds_file"] == int(9.5 * H)
    assert report["summary"]["seconds_import"] == 5 * H


# --------------------------------------------------------------------------- commit rules


def test_commit_twice_and_skip_errors_false(db):
    content = csv_bytes("Datum;Dauer;Kategorie\n01.10.2026;1;K\n02.10.2026;x;K\n")
    preview, report = upload_validate(db, content)
    assert report["can_commit"]
    with pytest.raises(svc.ImportValidationError, match="Fehler"):
        svc.commit(db, preview["batch_id"], preview["mapping"], options={"skip_errors": False})
    assert imported(db, preview["batch_id"]) == []
    result = svc.commit(db, preview["batch_id"])
    assert result["imported_rows"] == 1 and result["skipped"]["fehler"] == 1
    batch = db.get(ImportBatch, preview["batch_id"])
    assert (batch.rows_total, batch.rows_imported, batch.rows_skipped) == (2, 1, 1)
    assert batch.mapping_json["stage"] == "importiert"
    assert batch.notes.startswith("1 Zeile(n) importiert (1 Einträge)")
    with pytest.raises(svc.ImportConflict):
        svc.commit(db, preview["batch_id"])


def test_commit_without_mapping_needs_validation_first(db):
    preview = svc.upload(db, csv_bytes("Datum;Dauer;Kategorie\n01.10.2026;1;K\n"), "a.csv")
    with pytest.raises(svc.ImportValidationError, match="zuordnen"):
        svc.commit(db, preview["batch_id"])
    result = svc.commit(db, preview["batch_id"], preview["mapping"])  # direct commit with mapping
    assert result["imported_rows"] == 1


def test_commit_is_atomic_when_entries_service_rejects_a_row(db, monkeypatch):
    content = csv_bytes("Datum;Dauer;Kategorie\n01.10.2026;1;K\n02.10.2026;1;K\n03.10.2026;1;K\n")
    preview, _ = upload_validate(db, content)
    original = entries_svc.create_entry
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise entries_svc.EntryValidationError("Testfehler")
        return original(*args, **kwargs)

    monkeypatch.setattr(entries_svc, "create_entry", flaky)
    with pytest.raises(svc.ImportValidationError, match="Zeile 3: Testfehler"):
        svc.commit(db, preview["batch_id"])
    assert imported(db, preview["batch_id"], include_deleted=True) == []
    assert db.get(ImportBatch, preview["batch_id"]).mapping_json["stage"] == "geprueft"


def test_imported_entry_flags_and_revisions(db):
    preview, _ = upload_validate(db, csv_bytes("Datum;Dauer;Kategorie\n03.10.2026;1;K\n"))
    svc.commit(db, preview["batch_id"])
    (entry,) = imported(db, preview["batch_id"])
    assert entry.source == "import" and entry.import_batch_id == preview["batch_id"]
    assert "wochenende" in entry.flags  # 03.10.2026 is a Saturday
    assert "nachtraeglich" in entry.flags  # recorded long after the day
    assert db.scalars(select(TimeEntryRevision).where(TimeEntryRevision.entry_id == entry.id)).all() == []


# --------------------------------------------------------------------------- closed months


def close_month(db, ym: str) -> MonthClose:
    mc = MonthClose(year_month=ym, closed_at=datetime(2026, 10, 2, 10), entries_hash_sha256=entries_svc.month_entries_hash(db, ym),
                    soll_seconds=0, ist_seconds=0, saldo_seconds=0)
    db.add(mc)
    db.commit()
    return mc


def test_closed_month_rows_need_reason(db):
    close_month(db, "2026-09")
    content = csv_bytes("Datum;Dauer;Kategorie\n30.09.2026;1;K\n01.10.2026;1;K\n")
    preview, report = upload_validate(db, content)
    rows = rows_by_ref(report)
    assert rows["2"]["status"] == "fehler" and "abgeschlossen" in rows["2"]["errors"][0]
    assert rows["2"]["closed_month"] and report["closed_months"] == ["2026-09"]
    assert rows["3"]["status"] == "ok"

    report = svc.validate(db, preview["batch_id"], preview["mapping"], options={"reason": "Nachtrag Papierliste"},
                          today=TODAY)
    assert rows_by_ref(report)["2"]["status"] == "ok"
    svc.commit(db, preview["batch_id"])
    entries = imported(db, preview["batch_id"])
    sep = next(e for e in entries if e.day.month == 9)
    assert sep.locked is True
    (rev,) = db.scalars(select(TimeEntryRevision).where(TimeEntryRevision.entry_id == sep.id)).all()
    assert rev.revision_no == 0 and rev.change_reason == "Import: Nachtrag Papierliste"
    mc = db.get(MonthClose, "2026-09")
    assert mc.status == "nachtraeglich_korrigiert" and len(mc.hash_history) == 1
    okt = next(e for e in entries if e.day.month == 10)
    assert db.scalars(select(TimeEntryRevision).where(TimeEntryRevision.entry_id == okt.id)).all() == []


# --------------------------------------------------------------------------- undo


def test_undo_soft_deletes_all_entries_of_batch(db):
    content = csv_bytes("Datum;Von;Bis;Dauer;Kategorie\n01.10.2026;22:00;01:00+1;;K\n02.10.2026;;;2;VN\n")
    preview, _ = upload_validate(db, content)
    svc.commit(db, preview["batch_id"])
    other = entries_svc.create_entry(db, {"has_bounds": False, "day": date(2026, 10, 2), "duration_seconds": 600,
                                          "category_id": cat(db, "U")}, source="manuell")[0]
    assert len(imported(db, preview["batch_id"])) == 3

    result = svc.undo(db, preview["batch_id"])
    assert result["entries_deleted"] == 3 and result["discarded"] is False
    assert imported(db, preview["batch_id"]) == []
    for e in imported(db, preview["batch_id"], include_deleted=True):
        assert e.deleted_at is not None
        rev = db.scalars(select(TimeEntryRevision).where(TimeEntryRevision.entry_id == e.id)
                         .order_by(TimeEntryRevision.revision_no.desc())).first()
        assert rev.change_reason == "Import rückgängig" and rev.snapshot_json["_action"] == "delete"
    assert db.get(TimeEntry, other.id).deleted_at is None
    batch = db.get(ImportBatch, preview["batch_id"])
    assert batch.undone_at is not None and batch.mapping_json["stage"] == "rueckgaengig"
    assert "Rückgängig gemacht" in batch.notes
    assert (get_config().imports_dir / batch.id / "original.csv").exists()  # evidence stays

    with pytest.raises(svc.ImportConflict):
        svc.undo(db, preview["batch_id"])
    with pytest.raises(svc.ImportConflict):
        svc.commit(db, preview["batch_id"])

    # After undo the same file can be imported again (deleted entries are no duplicates)
    _, report = upload_validate(db, content)
    assert report["summary"]["duplikat"] == 0


def test_undo_with_closed_month_requires_reason(db):
    preview, _ = upload_validate(db, csv_bytes("Datum;Dauer;Kategorie\n28.09.2026;1;K\n29.09.2026;1;K\n"
                                               "01.10.2026;1;K\n"))
    svc.commit(db, preview["batch_id"])
    close_month(db, "2026-09")
    with pytest.raises(svc.ImportLocked) as info:
        svc.undo(db, preview["batch_id"])
    assert info.value.locked_entries == 2 and info.value.status_code == 423
    assert "September 2026" in info.value.message
    assert len(imported(db, preview["batch_id"])) == 3  # nothing changed

    listed = svc.list_batches(db)[0]
    assert (listed["entries_active"], listed["entries_locked"]) == (3, 2)

    result = svc.undo(db, preview["batch_id"], reason="Falsche Datei")
    assert result["entries_deleted"] == 3 and result["locked_entries"] == 2
    assert imported(db, preview["batch_id"]) == []
    mc = db.get(MonthClose, "2026-09")
    assert mc.status == "nachtraeglich_korrigiert"
    revs = db.scalars(select(TimeEntryRevision.change_reason)).all()
    assert set(revs) == {"Import rückgängig: Falsche Datei"}


def test_undo_uncommitted_batch_discards_it(db):
    preview = svc.upload(db, csv_bytes("Datum;Dauer;Kategorie\n01.10.2026;1;K\n"), "a.csv")
    directory = get_config().imports_dir / preview["batch_id"]
    assert directory.exists()
    result = svc.undo(db, preview["batch_id"])
    assert result == {"discarded": True, "batch_id": preview["batch_id"], "entries_deleted": 0, "locked_entries": 0}
    assert db.get(ImportBatch, preview["batch_id"]) is None
    assert not directory.exists()
    with pytest.raises(svc.ImportNotFound):
        svc.undo(db, preview["batch_id"])


# --------------------------------------------------------------------------- batches / upload errors


def test_list_batches_and_batch_dict(db):
    p1, _ = upload_validate(db, csv_bytes("Datum;Dauer;Kategorie\n01.10.2026;1;K\n"), "eins.csv")
    svc.commit(db, p1["batch_id"])
    p2 = svc.upload(db, csv_bytes("Datum;Dauer;Kategorie\n02.10.2026;1;K\n"), "zwei.csv")
    batches = svc.list_batches(db)
    assert [b["id"] for b in batches] == [p2["batch_id"], p1["batch_id"]]
    first = batches[1]
    assert first["stage"] == "importiert" and first["entries_active"] == 1 and first["entries_created"] == 1
    assert first["label"].startswith("Import vom ") and first["label"].endswith("aus Datei eins.csv")
    assert batches[0]["stage"] == "hochgeladen" and batches[0]["entries_active"] == 0
    with pytest.raises(svc.ImportNotFound):
        svc.get_batch(db, "nope")


def test_upload_errors(db, monkeypatch):
    with pytest.raises(svc.ImportFileError, match="leer"):
        svc.upload(db, b"", "a.csv")
    with pytest.raises(svc.ImportFileError, match="keine Daten"):
        svc.upload(db, b"\n\n;;\n", "a.csv")
    with pytest.raises(svc.ImportFileError, match="xls"):
        svc.upload(db, b"\xd0\xcf\x11\xe0" + b"\x00" * 100, "alt.xls")
    with pytest.raises(svc.ImportFileError, match="XLSX"):
        svc.upload(db, b"PK\x03\x04garbage", "kaputt.xlsx")
    monkeypatch.setattr(svc, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(svc.ImportFileTooLarge) as info:
        svc.upload(db, b"Datum;Dauer\n01.10.2026;1\n", "a.csv")
    assert info.value.status_code == 413
    assert db.scalars(select(ImportBatch)).all() == []
    assert not any(get_config().imports_dir.iterdir())


def test_missing_original_file_is_reported(db):
    preview = svc.upload(db, csv_bytes("Datum;Dauer;Kategorie\n01.10.2026;1;K\n"), "a.csv")
    (get_config().imports_dir / preview["batch_id"] / "original.csv").unlink()
    with pytest.raises(svc.ImportNotFound, match="Originaldatei"):
        svc.validate(db, preview["batch_id"], preview["mapping"])
