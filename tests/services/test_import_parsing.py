"""Unit tests for the file-reading half of app/services/import_service.py (no DB needed for most)."""

from __future__ import annotations

import codecs
import io
from datetime import date, datetime, time, timedelta

import pytest
from openpyxl import Workbook

from app.services import import_service as svc


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


# --------------------------------------------------------------------------- type / encoding / delimiter


def test_detect_file_type_by_content():
    assert svc.detect_file_type(b"Datum;Dauer\n", "x.csv") == "csv"
    assert svc.detect_file_type(xlsx_bytes({"A": [["x"]]}), "whatever.bin") == "xlsx"


def test_detect_file_type_rejects_old_xls_and_fake_xlsx():
    with pytest.raises(svc.ImportFileError, match="xls"):
        svc.detect_file_type(b"\xd0\xcf\x11\xe0" + b"\x00" * 20, "alt.xls")
    with pytest.raises(svc.ImportFileError, match="XLSX"):
        svc.detect_file_type(b"not a zip", "kaputt.xlsx")


@pytest.mark.parametrize(
    ("raw", "encoding"),
    [
        ("Tätigkeit;Dauer\n".encode("utf-8"), "utf-8"),
        (codecs.BOM_UTF8 + "Tätigkeit;Dauer\n".encode("utf-8"), "utf-8-sig"),
        ("Tätigkeit;Dauer\n".encode("cp1252"), "cp1252"),
        ("Tätigkeit\tDauer\n".encode("utf-16"), "utf-16"),
    ],
)
def test_decode_text_detects_encoding(raw, encoding):
    text, detected = svc.decode_text(raw)
    assert detected == encoding
    assert text.startswith("Tätigkeit")


def test_decode_text_rejects_binary():
    with pytest.raises(svc.ImportFileError):
        svc.decode_text(b"abc\x00def")


def test_sniff_semicolon_wins_against_decimal_commas():
    text = "Datum;Dauer;Kategorie\n01.10.2026;2,5;K\n02.10.2026;1,5;VN\n03.10.2026;3;U\n"
    assert svc.sniff_delimiter(text) == ";"


def test_sniff_comma_with_quoted_decimals():
    text = 'Datum,Dauer,Kategorie\n01.10.2026,"2,5",K\n02.10.2026,"1,5",VN\n'
    assert svc.sniff_delimiter(text) == ","


def test_sniff_tab():
    assert svc.sniff_delimiter("Datum\tDauer\n01.10.2026\t2,5\n") == "\t"


def test_sniff_single_column_defaults_to_semicolon():
    assert svc.sniff_delimiter("Datum\n01.10.2026\n") == ";"


# --------------------------------------------------------------------------- tables / header / mapping guess


def test_csv_table_header_detection_and_guess():
    src = svc._Source("Datum;Beginn;Ende;Dauer (h);Tätigkeit;Klasse;Fach;Bemerkung;Ort\n"
                      "01.10.2026;08:00;09:00;1;U;3b;Mathe;x;Schule\n".encode(), "csv")
    table = src.table()
    assert table.header_row == 1
    assert len(table.data) == 1
    mapping = svc.guess_mapping(table)
    assert mapping == {"datum": 0, "von": 1, "bis": 2, "dauer": 3, "kategorie": 4, "klasse": 5, "fach": 6,
                       "notiz": 7, "ort": 8}


def test_header_detection_skips_title_rows_and_blank_lines():
    content = "Arbeitszeit Schuljahr 2026/27;;\n\nDatum;Stunden;Aufgabe\n01.10.2026;2,5;K\n".encode()
    table = svc._Source(content, "csv").table()
    assert table.header_row == 3
    assert [no for no, _ in table.data] == [4]
    guessed = svc.guess_mapping(table)
    assert guessed["datum"] == 0 and guessed["dauer"] == 1 and guessed["kategorie"] == 2


def test_no_header_row_guesses_date_column_from_content():
    content = "01.10.2026;2,5;K\n02.10.2026;1:30;VN\n".encode()
    table = svc._Source(content, "csv").table()
    assert table.header_row == 0
    assert table.headers == ["Spalte A", "Spalte B", "Spalte C"]
    assert len(table.data) == 2
    assert svc.guess_mapping(table)["datum"] == 0


def test_explicit_header_row_and_no_header():
    content = "Datum;Dauer\n01.10.2026;2\n".encode()
    src = svc._Source(content, "csv")
    assert len(src.table(header_row=0).data) == 2
    assert src.table(header_row=1).headers == ["Datum", "Dauer"]
    with pytest.raises(svc.ImportValidationError):
        src.table(header_row=7)


def test_xlsx_sheets_and_typed_cells():
    content = xlsx_bytes({
        "Deckblatt": [["Nur Text"]],
        "Oktober": [
            ["Datum", "Von", "Bis", "Dauer", "Kategorie"],
            [datetime(2026, 10, 1), time(8, 0), time(9, 30), None, "U"],
            [date(2026, 10, 2), None, None, timedelta(hours=2, minutes=15), "K"],
        ],
    })
    src = svc._Source(content, "xlsx")
    assert src.sheets == ["Deckblatt", "Oktober"]
    table = src.table("Oktober")
    (no1, row1), (no2, row2) = table.data
    assert (no1, no2) == (2, 3)
    assert isinstance(row1[0], datetime) and row1[1] == time(8, 0)
    assert svc.display_cell(row1[0]) == "01.10.2026"
    assert svc.display_cell(row1[1]) == "08:00"
    assert svc.display_cell(row2[3]) == "2:15"
    with pytest.raises(svc.ImportValidationError, match="nicht gefunden"):
        src.table("Gibtsnicht")


def test_display_cell_formats():
    assert svc.display_cell(None) == ""
    assert svc.display_cell(2.5) == "2,5"
    assert svc.display_cell(3.0) == "3"
    assert svc.display_cell(datetime(2026, 10, 1, 8, 15)) == "01.10.2026 08:15"
    assert svc.display_cell(True) == "WAHR"


def test_column_letters():
    assert [svc.column_letter(i) for i in (0, 1, 25, 26, 27, 51, 52)] == ["A", "B", "Z", "AA", "AB", "AZ", "BA"]
    assert all(svc._letter_index(svc.column_letter(i)) == i for i in range(80))


def test_resolve_mapping_accepts_index_header_and_letter():
    table = svc._Source("Datum;Dauer;Kategorie\n01.10.2026;2;K\n".encode(), "csv").table()
    cols = svc._resolve_mapping({"datum": "datum", "dauer": "B", "kategorie": 2}, table, has_default_category=False)
    assert (cols["datum"], cols["dauer"], cols["kategorie"]) == (0, 1, 2)


@pytest.mark.parametrize(
    ("mapping", "message"),
    [
        ({"dauer": 1, "kategorie": 2}, "Datum"),
        ({"datum": 0, "kategorie": 2}, "Von und Bis oder"),
        ({"datum": 0, "von": 1, "kategorie": 2}, "Von und Bis oder"),
        ({"datum": 0, "dauer": 1}, "Standard-Kategorie"),
        ({"datum": 0, "dauer": 0, "kategorie": 2}, "unterschiedliche"),
        ({"datum": 0, "dauer": 1, "kategorie": 2, "foo": 1}, "Unbekannte Felder"),
        ({"datum": 0, "dauer": 9, "kategorie": 2}, "existiert nicht"),
        ({"datum": 0, "dauer": "Stundenzahl", "kategorie": 2}, "nicht gefunden"),
    ],
)
def test_resolve_mapping_errors(mapping, message):
    table = svc._Source("Datum;Dauer;Kategorie\n01.10.2026;2;K\n".encode(), "csv").table()
    with pytest.raises(svc.ImportValidationError, match=message):
        svc._resolve_mapping(mapping, table, has_default_category=False)


def test_options_parse_and_roundtrip():
    opts = svc.ImportOptions.parse({"category_map": {" Korr ": "K", "leer": "", "x": None}, "include_rows": [3, "B!4"],
                                    "skip_duplicates": False, "reason": "  "})
    assert opts.category_map == {"Korr": "K"}
    assert opts.include_rows == frozenset({"3", "B!4"})
    assert opts.skip_duplicates is False and opts.skip_errors is True
    assert opts.reason is None
    assert svc.ImportOptions.parse(opts.to_json()) == opts
    with pytest.raises(svc.ImportValidationError, match="Unbekannte Optionen"):
        svc.ImportOptions.parse({"nope": 1})
