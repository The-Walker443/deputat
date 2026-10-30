"""services/documents.py: letter generator (Konzept §11, L7, Anhang A/B)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Category, Document, Settings, TimeEntry
from app.services import calendar_service as cal
from app.services import documents as docs
from app.util import local_to_utc

TODAY = date(2026, 10, 30)


def cat(db, code: str) -> int:
    return db.scalar(select(Category.id).where(Category.code == code))


def add(db, code: str, day: date, hours: float, *, start: time = time(8), flags=()) -> None:
    start_at = local_to_utc(day, start)
    secs = int(hours * 3600)
    db.add(TimeEntry(
        start_at=start_at, end_at=start_at + timedelta(seconds=secs), duration_seconds=secs, has_bounds=True,
        day=day, category_id=cat(db, code), status="active", source="manuell", flags=list(flags),
    ))


@pytest.fixture
def year(db):
    period = cal.create_period(db, "2026/27")
    db.commit()
    return period


@pytest.fixture
def data(db, year):
    """Two full school weeks (5 × 10 h) in October plus FS and KV work; Vertretung in October."""
    for monday in (date(2026, 10, 12), date(2026, 10, 19)):
        for i in range(5):
            d = monday + timedelta(days=i)
            add(db, "U", d, 6)
            add(db, "VN", d, 3, start=time(15))
            add(db, "FS", d, 1, start=time(19))
    for d, flag in ((date(2026, 10, 13), "vertretung_angeordnet"), (date(2026, 10, 14), "vertretung_genehmigt"),
                    (date(2026, 10, 15), "vertretung_angeordnet")):
        add(db, "U", d, 1.5, start=time(14, 0), flags=[flag])
    s = db.get(Settings, 1)
    s.name, s.schule = "Erika Muster", "Grundschule Musterstadt"
    db.commit()


def test_notice_is_first_block_for_every_type(db, data):
    for doc_type in docs.TYPES:
        md = docs.render(db, doc_type, date(2026, 10, 12), date(2026, 10, 25), {"monat": "2026-10"}, today=TODAY)
        assert md.startswith(docs.NOTICE_BLOCK), doc_type
        assert md.count(docs.NOTICE) == 1


def test_notice_cannot_be_removed(db):
    assert docs.ensure_notice("Hallo").startswith(docs.NOTICE_BLOCK)
    again = docs.ensure_notice(docs.ensure_notice("Text"))
    assert again.count(docs.NOTICE) == 1
    doc = docs.save(db, "geltendmachung", "Nur mein Text ohne Hinweis")
    assert doc.rendered_md.startswith(docs.NOTICE_BLOCK)
    assert "Nur mein Text" in doc.rendered_md
    assert docs.NOTICE in docs.to_html("ohne Hinweis")


def test_geltendmachung_values(db, data):
    r = docs.prepare(db, "geltendmachung", date(2026, 10, 12), date(2026, 10, 25), {"ort": "Musterstadt"}, today=TODAY)
    v = r.values
    assert v["name"] == "Erika Muster" and v["schule"] == "Grundschule Musterstadt"
    assert v["zeitraum_von"] == "12.10.2026" and v["zeitraum_bis"] == "25.10.2026"
    assert v["schulwochen_anzahl"] == "2"
    assert v["schulwochen_avg_h"] == "52,25"  # (2×50 h + 4,5 h Vertretung) / 2
    assert v["schulwochen_ueber_48"] == "2"
    assert v["soll_h"] == "80,00"
    assert v["saldo_periode_h"] == "+24,50"
    assert "| Unterricht |" in v["kategorien_tabelle"]
    assert v["datum"] == "30.10.2026"
    assert v["dienstweg"] == "auf dem Dienstweg über die Schulleitung der Grundschule Musterstadt"
    md = r.markdown
    assert "Musterstadt, 30.10.2026" in md
    assert "Abdruck: Personalrat" in md
    assert "[" not in md.split("Sehr geehrte")[0].replace("[Landkreis/Stadt]", "")  # no gaps in the header
    # sober wording, legal references only as "vgl." with a verification hint (Anhang A/B)
    assert "vgl. § 2 Abs. 1 und Abs. 4 BayAzV" in md
    assert "ohne Gewähr, Rechtsstand vor Versand prüfen" in md
    assert "Anspruch besteht" not in md


def test_ueberlastungsanzeige_contents(db, data):
    md = docs.render(db, "ueberlastungsanzeige", date(2026, 10, 12), date(2026, 10, 25), today=TODAY)
    assert "Bitte um Priorisierung" in md
    assert "weiterhin alle mir übertragenen Aufgaben vollständig erfüllen" in md
    assert "- Unterricht:" in md and "Ø" in md  # burdens: categories with hours


def test_prioritaetsbitte_lists_fs_tasks(db, data):
    r = docs.prepare(db, "prioritaetsbitte", date(2026, 10, 12), date(2026, 10, 25), today=TODAY)
    assert r.values["adressat"] == "Schulleitung der Grundschule Musterstadt"
    tasks = r.values["aufgaben_liste"]
    assert "10,00 h" in tasks  # FS 10 × 1 h
    assert tasks.strip().endswith("[weitere Aufgaben ergänzen]")
    assert "Abdruck" not in r.markdown


def test_antrag_freizeitausgleich_month(db, data):
    r = docs.prepare(db, "antrag_freizeitausgleich", overrides={"monat": "2026-10"}, today=TODAY)
    v = r.values
    assert v["monat"] == "Oktober 2026"
    assert v["ustd_angeordnet"] == "4,0" and v["ustd_genehmigt"] == "2,0" and v["ustd_gesamt"] == "6,0"
    assert v["ustd_ausgleichsfaehig"] == "6,0"
    assert v["freizeitausgleich_frist"] == "31.01.2027"
    assert v["mehrarbeit_tabelle"].count("\n") == 2
    assert "angeordnet" in v["mehrarbeit_tabelle"] and "genehmigt" in v["mehrarbeit_tabelle"]
    assert (r.period_from, r.period_to) == (date(2026, 10, 1), date(2026, 10, 31))
    assert "vgl. Art. 87 BayBG" in r.markdown


def test_antrag_bagatell_warning(db, year):
    add(db, "U", date(2026, 10, 13), 1.5, flags=["vertretung_angeordnet"])
    db.commit()
    r = docs.prepare(db, "antrag_freizeitausgleich", overrides={"monat": "2026-10"}, today=TODAY)
    assert r.values["ustd_ausgleichsfaehig"] == "0,0"
    assert any("Bagatellgrenze" in w for w in r.warnings)


def test_range_is_clamped_and_validated(db, year):
    r = docs.prepare(db, "geltendmachung", date(2025, 1, 1), date(2026, 9, 30), today=TODAY)
    assert r.period_from == date(2026, 8, 1)
    assert any("gekürzt" in w for w in r.warnings)
    with pytest.raises(docs.DocumentError):
        docs.prepare(db, "geltendmachung", date(2026, 10, 10), date(2026, 10, 1), today=TODAY)
    with pytest.raises(docs.DocumentError):
        docs.prepare(db, "unbekannt", today=TODAY)


def test_range_entirely_outside_period_is_rejected(db, year):
    # Browser review: 2025 dates used to clamp only the start -> "Zeitraum 01.08.2026 bis 01.02.2025".
    with pytest.raises(docs.DocumentError, match="außerhalb der Kontoperiode"):
        docs.prepare(db, "geltendmachung", date(2025, 1, 1), date(2025, 2, 1), today=TODAY)


def test_negative_saldo_warns(db, year):
    r = docs.prepare(db, "geltendmachung", date(2026, 10, 12), date(2026, 10, 16), today=TODAY)
    assert r.values["saldo_periode_h"] == "-40,00"
    assert any("negativ" in w for w in r.warnings)


def test_no_period_raises(db):
    with pytest.raises(docs.DocumentError, match="Schuljahr"):
        docs.render(db, "geltendmachung", today=TODAY)


def test_unknown_placeholder_is_visible():
    assert docs.fill("{{name}} {{gibtsnicht}}", {"name": "A"}) == "A [gibtsnicht?]"


def test_overrides_and_empty_abdruck(db, data):
    r = docs.prepare(db, "geltendmachung", date(2026, 10, 12), date(2026, 10, 25),
                     {"adressat": "Staatliches Schulamt im Landkreis Musterkreis", "abdruck": "", "ort": "Musterstadt"},
                     today=TODAY)
    assert "Staatliches Schulamt im Landkreis Musterkreis" in r.markdown
    assert "Abdruck" not in r.markdown  # empty abdruck removes the line
    r2 = docs.prepare(db, "geltendmachung", date(2026, 10, 12), date(2026, 10, 25),
                      {"abdruck": "-", "dienstweg": "-"}, today=TODAY)
    assert "Abdruck" not in r2.markdown
    assert "Dienstweg" not in r2.markdown and "auf dem Dienstweg" not in r2.markdown
    assert "[Ort]" in r2.markdown and any("Ort fehlt" in w for w in r2.warnings)


def test_create_mark_sent_and_wirksam_ab(db, data):
    assert docs.geltendmachung_wirksam_ab(db) is None
    doc, _ = docs.create(db, "geltendmachung", date(2026, 10, 12), date(2026, 10, 25), text="Bearbeitet", today=TODAY)
    assert db.get(Document, doc.id).rendered_md.startswith(docs.NOTICE_BLOCK)
    assert "Bearbeitet" in doc.rendered_md
    assert doc.params_json["zeitraum_von"] == "12.10.2026"
    ueb, _ = docs.create(db, "ueberlastungsanzeige", today=TODAY)
    docs.mark_sent(db, ueb.id, date(2026, 9, 1))
    assert docs.geltendmachung_wirksam_ab(db) is None  # only Geltendmachung counts
    docs.mark_sent(db, doc.id, date(2026, 10, 20))
    assert docs.geltendmachung_wirksam_ab(db) == date(2026, 11, 1)
    later, _ = docs.create(db, "geltendmachung", today=TODAY)
    docs.mark_sent(db, later.id, datetime(2026, 10, 29, 9, 0))
    assert docs.geltendmachung_wirksam_ab(db) == date(2026, 11, 1)  # earliest sent one
    with pytest.raises(docs.DocumentError):
        docs.mark_sent(db, later.id, date.today() + timedelta(days=3))
    assert [d.id for d in docs.list_documents(db, "geltendmachung")][-1] == doc.id


def test_save_rejects_empty_and_too_long(db):
    with pytest.raises(docs.DocumentError):
        docs.save(db, "geltendmachung", "   ")
    with pytest.raises(docs.DocumentError):
        docs.save(db, "geltendmachung", "x" * (docs.MAX_MD_LENGTH + 1))


def test_to_html_neutralises_raw_html():
    html = str(docs.to_html("Text <script>alert(1)</script> & **fett**\n\n| a | b |\n|---|---|\n| 1 | 2 |"))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<strong>fett</strong>" in html
    assert "<table>" in html
    assert html.startswith("<blockquote>")


def test_ueberlastungsanzeige_category_means_add_up_to_the_weekly_mean(db, data):
    """Audit: the per-category Ø used fractional school weeks of the whole range, the headline only full weeks."""
    import re

    # extend the range into the Herbstferien-free week before (partial week -> not a full school week)
    md = docs.render(db, "ueberlastungsanzeige", date(2026, 10, 1), date(2026, 10, 25), today=TODAY)
    headline = re.search(r"vollen Schulwochen: ([\d,]+) Stunden \((\d+) Wochen\)", md)
    assert headline is not None
    weekly = float(headline.group(1).replace(",", "."))
    means = [float(x.replace(",", ".")) for x in re.findall(r"Ø ([\d,]+) h je voller Schulwoche", md)]
    assert means and abs(sum(means) - weekly) < 0.05
    assert f"dieselben {headline.group(2)} vollen Schulwochen" in md
