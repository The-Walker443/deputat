# Deputat – Rechenregeln im Detail

Diese Datei beschreibt genau, wie Deputat rechnet – so, wie es im Code umgesetzt ist (Rechenkern `app/core/`,
Datenzugriff ausschließlich über `app/services/ledger.py`). Die allgemeinverständliche Fassung steht im
[README](../README.md#so-rechnet-das-konto). Fachliche Quelle ist das Konzept
(Fachkonzept, Kapitel 6 und 13; nicht im öffentlichen Repo); wo der Code eine Lücke des Konzepts
entschieden hat, ist das hier als **Entscheidung** markiert.

Rechenregeln-Version: **1** (steht in jedem Bericht und unter *Mehr → Über Deputat*).

> Keine Rechtsberatung. Die App rechnet nach den im Konzept festgelegten Regeln; ob diese Regeln im
> Einzelfall rechtlich zutreffen, ist vor jeder Verwendung gegenüber dem Dienstherrn zu prüfen.

---

## 1. Grundbegriffe und Einheiten

- Gespeichert wird in **Sekunden**, Zeitstempel in UTC; angezeigt wird in Europe/Berlin als `h:mm`,
  in Berichten zusätzlich dezimal mit 2 Stellen. Gerundet wird nur bei der Anzeige
  (Summen werden aus exakten Werten gebildet, `app/core/soll.py`).
- **Teilzeitfaktor** `f = UPZ-Vertrag / UPZ-voll`, wobei
  `UPZ-Vertrag = UPZ-Ist + Altersermäßigung + Anrechnungsstunden` (höchstens UPZ-voll).
  **Entscheidung:** In den Einstellungen ist *UPZ-Ist* die tatsächlich gehaltene Unterrichtsmenge (Ermäßigungen
  schon abgezogen). Altersermäßigung und Anrechnungsstunden dürfen das Soll aber nicht senken (Konzept §6.1),
  deshalb werden sie für den Faktor wieder addiert (`app/services/settings_service.py`, `contract_upz`).
  Beispiel: 28 UPZ voll, UPZ-Ist 26, Altersermäßigung 2 → f = 28/28 = 1,0.
- **Tages-Soll** eines normalen Werktags = `Wochen-Soll voll / 5 × f` (40 h → 8:00 bei Vollzeit, 6:00 bei 21/28).
- **Altersermäßigung** wird aus dem Geburtsdatum berechnet (−1 UPZ ab 58, −2 ab 60, −3 ab 62; Stichtag:
  Geburtstag 01.08.–31.01. → laufendes Schuljahr, sonst folgendes) und ist überschreibbar.

## 2. Tagestyp und Referenz-Soll (`app/core/calendar.py`, `app/core/soll.py`)

Vorrang beim Tagestyp: Wochenende > Feiertag > manuell gesetzter Tagestyp > Ferientag > unterrichtsfrei > Schultag.

| Tag | Soll |
|---|---|
| Samstag, Sonntag | 0 |
| gesetzlicher Feiertag Bayern (inkl. eingeschalteter regionaler: Mariä Himmelfahrt, Augsburger Friedensfest) | 0 |
| Urlaubstag (aus der Urlaubszuordnung) | 0 |
| Dienstbefreiung, Sonderurlaub, Elternzeit | 0 |
| Schultag, Ferientag ohne Urlaub, unterrichtsfreier Tag (z. B. Buß- und Bettag, pädagogischer Tag), Fortbildung | Tages-Soll (8:00 · f) |
| Krankheitstag | Tages-Soll – aber siehe §4 (neutralisiert) |

Feiertage werden berechnet (Ostern nach Gauß), nicht hart kodiert. Buß- und Bettag (Mittwoch vor dem 23.11.)
ist automatisch *unterrichtsfrei* (kein Feiertag, Soll bleibt).

**J_soll** = Summe aller Tages-Soll der Kontoperiode (Konzept-Test T7: 261 Werktage − 11 Feiertage − 30 Urlaubstage
= 220 Tage × 8 h = 1.760 h).

## 3. Urlaubszuordnung (`app/core/leave.py`)

Die 30 Urlaubstage (Einstellung *Urlaubstage*, bei Teilzeit ebenfalls 30) werden auf **Kandidaten** gelegt:
Mo–Fr, Tagestyp Ferientag oder unterrichtsfrei, kein Feiertag, keine andere Abwesenheit.

- **Sommerferien zuerst** (Standard): Sommerferien chronologisch, danach Weihnachten, Pfingsten, Ostern,
  Fasching, Herbst, sonstige Ferien, zuletzt unterrichtsfreie Tage außerhalb von Ferien.
  **Entscheidung:** „Sommerferien“ sind die Sommerferien am Anfang der Periode. Beginnen die nächsten
  Sommerferien schon vor dem 31.07. (z. B. 29.07.), gehören diese Tage zum nächsten Urlaubsjahr und werden erst
  nach allen anderen Ferien verwendet.
- **Proportional:** 30 Tage anteilig nach der Zahl der Kandidaten je Ferienblock, Verfahren des größten Rests.
  **Entscheidung (Gleichstand):** bei gleichem Rest gewinnt der größere Block, dann der frühere. Konzept-Test T9:
  Blöcke 30/10/5/10/10 → 14/5/2/5/4. Innerhalb eines Blocks chronologisch.
- **Manuell:** die im Kalender angeklickten Tage; ungültige Tage werden abgewiesen, „noch zuzuordnen: n“ wird
  angezeigt, mehr als 30 Tage können nicht gespeichert werden.

**Entscheidung:** Ein als *krank* markierter Ferientag ist kein Urlaubskandidat; der Urlaubstag wandert auf den
nächsten Kandidaten. Gibt es weniger Kandidaten als Urlaubstage, werden alle belegt und der Fehlbetrag angezeigt.

Das Konto zeigt beide automatischen Modi im Vergleich (*Konto → Urlaubsmodus im Vergleich*); maßgeblich für
Berichte ist der in den Einstellungen gewählte Modus.

## 4. Ist-Zeit (`app/services/ledger.py`, `app/core/account.py`, `app/core/klassenfahrt.py`)

`Ist(Tag)` = Summe der Dauer aller **aktiven, nicht gelöschten** Einträge, deren lokales Datum (Start) der Tag ist.
Stundenplan-Vorschläge zählen erst nach Bestätigung. Ein laufender Timer zählt mit der bisher verstrichenen Zeit.

- **Mitternacht:** Ein Eintrag über Mitternacht wird beim Speichern in Teile je Kalendertag geteilt
  (halboffene Intervalle: Teil 1 endet exakt um 00:00, angezeigt als 23:59:59/24:00; die Summe bleibt
  sekundengenau gleich; Sommer-/Winterzeit wird berücksichtigt). Konzept-Test T13: 22:30–01:15 → 22:30–24:00 und
  00:00–01:15.
- **Vergessener Timer** (Entscheidung, `app/services/timer.py`): Wird ein Timer bis 4 Stunden nach Mitternacht
  gestoppt, wird normal geteilt. Später (oder an einem späteren Tag) wird er automatisch um 24:00 des Starttags
  beendet, mit dem Hinweis, spätere Arbeitszeit nachzutragen. Laufzeit > 6 h erzeugt einen Hinweis.
- **Krankheit** (Einstellung *Krankheit neutralisiert das Soll*, Standard an):
  - Ganzer Krankheitstag (im Kalender als *krank* markiert): `Ist := erfasste Zeit + Tages-Soll` – das Konto
    bleibt unverändert (T10). **Entscheidung:** Wird an einem ganz krank gemeldeten Tag trotzdem etwas erfasst,
    zählt es zusätzlich.
  - Teilweise krank: Eintrag in der Sonderkategorie **„Krank (Teil)“** (Code KR). Diese Zeit füllt nur die
    Lücke bis zum Soll auf: Gutschrift = min(KR-Zeit, max(0, Soll − erfasste Arbeit)). Sie erzeugt nie ein Plus
    und erscheint nicht in Kategorie-Statistiken.
  - Einstellung aus: keine Gutschrift, Krankheitstage bauen das Konto ab.
- **Klassenfahrt** (Einstellung *Klassenfahrt-Bewertung*, Standard B): Betrifft jeden Tag mit mindestens einem
  Eintrag mit Kennzeichen *Klassenfahrt*.
  - Variante A: alle Einträge inkl. *Nachtbereitschaft*.
  - Variante B: Einträge mit *Nachtbereitschaft* fallen weg, die Tagessumme **aller** übrigen Einträge des Tages
    wird auf die eingestellte Kappung (Standard 10 h) begrenzt. Kategorie-Statistiken werden auf derselben Basis
    anteilig gekürzt.
- **Große Pausen** zählen nur, wenn eingestellt (dann schlägt der Stundenplan sie als Aufsichts-/Präsenzzeit
  vor); Aufsichten zählen immer.

## 5. Konto (`app/core/account.py`)

```
Saldo(t)       = Σ über alle Tage d ≤ t der Periode: Ist(d) − Soll(d)        („heute“ zählt mit)
Saldo in Tagen = Saldo / (8 h · f)
```

Jede Kontoperiode beginnt bei 0.

- **Übertrag** (Konzept §1.3, §18): Der Saldo am Ende der vorigen Periode (nur wenn dort etwas erfasst ist) wird auf
  „Konto“ als „Übertrag aus …“ ausgewiesen. Mit der Einstellung „Übertrag ins neue Jahr verrechnen“ wird er zu Saldo,
  Saldo-Verlauf, Prognose, Ferien-Guthaben-Rechner und kumuliertem Monats-Saldo addiert (Kette über höchstens drei
  Vorperioden); Standard ist *nur ausweisen*.
- **Erfassung ab** (Einstellung, Migration 0004): Tage vor diesem Datum zählen neutral (Ist = Soll), gehen nicht in die
  Prognose-Stichprobe und nicht in Ferienarbeits-Mittelwerte ein, erscheinen nicht als offene Tage, und das
  EU-Mittel beginnt frühestens dort. Wer mitten im Schuljahr anfängt, startet so nicht mit „0 h gearbeitet“ für jeden
  vergangenen Schultag.
- **Abgeschlossene Monate**: Ändert eine Einstellung (UPZ, Ermäßigungen, Wochen-Soll, Urlaub, regionale Feiertage,
  Krank-Regel, Klassenfahrt-Variante, Erfassung ab) Soll oder Ist eines abgeschlossenen Monats, wird nur mit
  Begründung gespeichert; der Monat wird dann „nachträglich korrigiert“.

**Ferienarbeit** = erfasste Zeit an Ferientagen und an Wochenenden/Feiertagen innerhalb eines Ferienblocks.

### Prognose Periodenende

- Mittel des Ist über die letzten **20 Schultage vor heute** (Einstellung *Prognose-Basis*; nur Schultage ohne
  Abwesenheit; heute zählt nicht, weil meist unvollständig). Gibt es noch keinen solchen Tag, wird Ist = Soll
  angenommen (neutral).
- Künftige Schultage: `Mittel − Soll`; künftige Ferien-/unterrichtsfreie Tage: `−Soll` (keine Arbeit erwartet);
  dazu die erwartete Ferienarbeit (siehe Ferien-Guthaben-Rechner).

### Ferien-Guthaben-Rechner

Für jeden noch nicht beendeten Ferienblock F, chronologisch verkettet:

```
Saldo vor F   = Saldo heute + Prognose bis Ferienbeginn
Abbau(F)      = Σ Soll der (restlichen) Tage von F     – i. d. R. 8 h · f je Werktag ohne Urlaub
Saldo nach F  = Saldo vor F − Abbau(F) + erwartete Ferienarbeit(F)
```

Erwartete Ferienarbeit = deine Eingabe im Rechner; leer = Mittel der früheren gleichartigen Ferien (aus bis zu
drei Vorperioden, nur vollständig erfasste Blöcke), ohne Vorgeschichte 0. In laufenden Ferien zählen nur die
restlichen Tage, bereits erfasste Ferienarbeit wird abgezogen. Konzept-Test T19: +83 h, Herbstferien mit 5 Werktagen
(Abbau 40 h), 12 h geplant → +55 h.

### W_max (zulässige Stunden je Schulwoche)

```
W_max = (J_soll − Ferienarbeit bisher − Ferienarbeit geplant) / Schulwochen
Schulwochen = Anzahl Schultage / 5   (Wochen mit Feiertag zählen anteilig)
```

### EU-12-Monats-Mittel (Höchstgrenze 48 h)

```
Fenster   = die 365 Tage bis einschließlich heute
Mittel    = erfasste Zeit im Fenster / (52 − neutrale Tage / 5)   [h/Woche]
```

- Neutrale Tage = Urlaubs- und Krankheitstage an Mo–Fr, die kein Feiertag sind. Gerechnet wird mit der
  **erfassten** Zeit (ohne Krank-Gutschrift, Klassenfahrt-Variante angewendet).
- **Entscheidung:** Tage vor dem Beginn der Datenerfassung (erste Periode bzw. erster Eintrag) gelten nicht als
  „0 h gearbeitet“, sondern werden wie Abwesenheit neutralisiert; das Mittel bezieht sich dann nur auf die
  erfassten Wochen (Anzeige „seit …“).
- **Ampel (wie umgesetzt):** grün unter 44 h, gelb 44 bis einschließlich 48 h, rot über 48 h.
  **Offene Frage:** Konzept-Test T18 (2.020 h, 6 Urlaubswochen → 2.020 / 46 ≈ 43,9 h) erwartet „gelb“; nach den
  Schwellen aus Konzept §6.5 ist 43,9 h aber „grün“. Umgesetzt ist §6.5; der Test ist als bekannte Abweichung
  markiert, bis entschieden ist.
- Zusätzlich: Zähler **Schulwochen > 48 h** (Wochen mit mindestens einem Schultag und mehr als 48 h erfasst).

## 6. Mehrarbeit im engen Sinn (`app/core/mehrarbeit.py`)

- Zählt nur Unterricht mit Kennzeichen *Vertretung angeordnet* oder *Vertretung genehmigt*
  (trägt ein Eintrag beide, zählt er als angeordnet).
- UStd = Minuten / 45, auf 0,5 gerundet (jede Summe einzeln aus Sekunden gerundet).
- **Bagatellgrenze:** ausgleichsfähig nur, wenn im Monat **mehr als 3** UStd anfallen – dann ab der ersten
  Stunde (T11: 3 UStd → 0; T12: 4 UStd → 4). Status je Monat: offen / beantragt / ausgeglichen; Hinweis auf
  Freizeitausgleich binnen drei Monaten.
- Diese Stunden sind ohnehin Teil des Ist; der Zähler weist sie nur getrennt aus.

## 7. Kennzeichen „nachträglich“ und „korrigiert“ (`app/core/timesplit.py`, `app/services/entries.py`)

- **nachträglich:** Ein Eintrag wird **mehr als 48 Stunden nach Ende seines Tages** angelegt. Beispiel: Eintrag
  für Montag, angelegt am Donnerstag 00:00:01 → nachträglich; Mittwoch 23:59 → nicht. Wird ein Eintrag später auf
  einen anderen Tag verschoben, wird die Regel zum Zeitpunkt des Verschiebens erneut geprüft.
- **korrigiert:** Start/Ende eines Timer-Eintrags wurden nachträglich geändert, oder ein Eintrag in einem
  abgeschlossenen Monat wurde geändert.
- Beide sind **nicht entfernbar**. *Ferienarbeit*, *Wochenende*, *Feiertag* und *ohne Zeitgrenzen* setzt die App
  selbst und rechnet sie bei jeder Änderung neu.

## 8. Prüf-Hash (`app/core/hashing.py`)

SHA-256 über eine kanonische Darstellung der Einträge (Hash-Format v1):

1. Je Eintrag genau diese Felder in dieser Reihenfolge, fehlende als `null`:
   `id, day, start_at, end_at, duration_seconds, has_bounds, category_id, subcategory_id, klasse, fach,
   location, note, flags, source, status, import_batch_id, created_at, deleted_at`.
   `locked` und `updated_at` sind bewusst nicht enthalten (das Abschließen ändert den Hash nicht).
2. Zeitstempel in UTC als `JJJJ-MM-TTThh:mm:ssZ` (Mikrosekunden nur, wenn ≠ 0, als `.ffffffZ`),
   Datum als `JJJJ-MM-TT`, `flags` alphabetisch sortiert.
3. Einträge sortiert nach (`start_at`, `id`).
4. Kompaktes JSON ohne Leerzeichen, UTF-8, Umlaute nicht maskiert:
   `{"v":1,"entries":[…]}`; davon SHA-256 als Hex.

Welche Einträge eingehen:

| Wo | Auswahl |
|---|---|
| Monatsabschluss, Monatsbericht | Tag im Monat, Status *aktiv*, **inklusive gelöschter** Einträge (die Löschung bleibt sichtbar) |
| Jahresbericht | wie oben, über die ganze Periode |
| CSV-Export (letzte Zeile „Prüf-Hash“) | genau die exportierten Zeilen: Zeitraum Von–Bis, Status *aktiv*, gelöschte nur mit Häkchen „Gelöschte Einträge mit ausgeben“ |
| ICS-Export (`X-DEPUTAT-PRUEF-HASH`) | aktive, nicht gelöschte, abgeschlossene Einträge im Zeitraum |

Ein offener Monat zeigt im Bericht einen **vorläufigen** Hash des aktuellen Stands. Wie man einen Hash
nachprüft, steht im [README](../README.md#einen-prüf-hash-nachprüfen); das dort verwendete Skript steht hier:

### Skript `pruef_hash.py` (nur Python-Standardbibliothek)

```python
"""Prüf-Hash von Deputat aus einem JSON-Vollbackup nachrechnen (nur Python-Standardbibliothek).

Aufruf:  python pruef_hash.py <deputat_backup_….json> <von JJJJ-MM-TT> <bis JJJJ-MM-TT> [--ohne-geloeschte]
"""
import hashlib
import json
import sys
from datetime import date, datetime

FELDER = ("id", "day", "start_at", "end_at", "duration_seconds", "has_bounds", "category_id",
          "subcategory_id", "klasse", "fach", "location", "note", "flags", "source", "status",
          "import_batch_id", "created_at", "deleted_at")
ZEITFELDER = ("start_at", "end_at", "created_at", "deleted_at")


def utc(text):
    """'2026-09-01T06:00:00[.123456]' (UTC) -> '2026-09-01T06:00:00[.123456]Z'."""
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ" if dt.microsecond else "%Y-%m-%dT%H:%M:%SZ")


backup, von, bis = sys.argv[1], date.fromisoformat(sys.argv[2]), date.fromisoformat(sys.argv[3])
mit_geloeschten = "--ohne-geloeschte" not in sys.argv
with open(backup, encoding="utf-8") as f:
    zeilen = json.load(f)["tables"]["time_entry"]

eintraege = []
for z in zeilen:
    if z["status"] != "active" or not (von <= date.fromisoformat(z["day"]) <= bis):
        continue
    if z["deleted_at"] and not mit_geloeschten:
        continue
    e = {feld: z.get(feld) for feld in FELDER}
    for feld in ZEITFELDER:
        e[feld] = utc(e[feld])
    e["flags"] = sorted(e["flags"] or [])
    eintraege.append(e)
eintraege.sort(key=lambda e: (e["start_at"] or "", str(e["id"] or "")))

daten = json.dumps({"v": 1, "entries": eintraege}, ensure_ascii=False, separators=(",", ":"))
print(f"{len(eintraege)} Einträge, SHA-256: {hashlib.sha256(daten.encode('utf-8')).hexdigest()}")
```

Das Skript ist bewusst unabhängig vom App-Code geschrieben (eine zweite, unabhängige Umsetzung derselben Regel)
und wurde gegen die App geprüft: Monatsabschluss-Hash, CSV-Hash mit und ohne gelöschte Einträge stimmen überein.

## 9. Konzept-Tests T1–T20 (Abnahme, `tests/core/`)

| # | Szenario | Ergebnis im Code |
|---|---|---|
| T1 | Schulwoche Vollzeit | Wochen-Soll 40:00 |
| T2 | Feiertag am Donnerstag | 32:00 |
| T3 | Ferienwoche ohne Urlaub, Ist 0 | −40:00 |
| T4 | Urlaubstag | Soll 0, Saldo unverändert |
| T5 | Schulwoche mit 50:00 | +10:00 |
| T6 | Teilzeit 21/28 | Woche 30:00, Urlaubstag 0, weiterhin 30 Urlaubstage |
| T7 | 261 Werktage, 11 Feiertage, 30 Urlaub | J_soll 1.760:00 |
| T8 | Sommerferien zuerst, 30 Ferien-Werktage | alle 30 Urlaubstage im Sommer |
| T9 | Proportional 30/10/5/10/10 | 14/5/2/5/4 |
| T10 | Krankheitstag, neutralisiert | Ist = Soll, Saldo unverändert |
| T11/T12 | 3 bzw. 4 UStd Vertretung | ausgleichsfähig 0 bzw. 4 |
| T13 | Timer 22:30–01:15 | zwei Teile, Summe unverändert |
| T14 | Eintrag bearbeiten | Revision mit vollem Vorzustand, Nummer +1 |
| T15 | Änderung im abgeschlossenen Monat | ohne Grund abgelehnt; mit Grund Revision, „nachträglich korrigiert“, alter Hash bleibt |
| T16 | Eintrag 3 Tage später angelegt | „nachträglich“, nicht entfernbar |
| T17 | Import „2,5“ ohne Uhrzeiten | 2:30, ohne Zeitgrenzen |
| T18 | EU-Mittel 2.020 h, 6 Urlaubswochen | 43,9 h – Ampel **grün** (Konzept erwartet gelb, siehe §5, offen) |
| T19 | Ferien-Guthaben +83 / −40 / +12 | +55 h |
| T20 | Hash | gleicher Stand → gleicher Hash, jede Feldänderung → anderer Hash |

Dazu Eigenschaftstests (hypothesis): Summe der Tages-Soll = J_soll, Saldo additiv, Urlaub nie auf Feiertag oder
Wochenende, Soll nie negativ.
