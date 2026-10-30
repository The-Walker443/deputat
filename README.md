# Deputat – Arbeitszeitkonto für Lehrkräfte

Deputat erfasst die gesamte Arbeitszeit einer verbeamteten Lehrkraft in Bayern minutengenau und vergleicht sie
laufend mit einem „normalen“ Beamten, der 40 Stunden pro Woche arbeitet. Das Ergebnis ist ein Arbeitszeitkonto,
das sich in Schulwochen aufbaut und in den Ferien abbaut – und am Ende des Schuljahres zeigt, wie viel mehr (oder
weniger) gearbeitet wurde. Die Daten werden so geführt, dass sie als Beleg taugen: jede Änderung wird protokolliert,
Monate werden mit einem Prüf-Hash abgeschlossen. Die App läuft selbst gehostet als ein Docker-Container (SQLite,
keine Cloud, keine Telemetrie) und ist fürs Handy gebaut (installierbar, Timer funktioniert auch offline).

> **Keine Rechtsberatung.** Deputat liefert Zahlen und Vorlagen, keine rechtliche Bewertung. Die zugrunde gelegten
> Regeln (siehe [Rechtsquellen](#rechtsquellen-zu-verifizieren)) sind vor jeder Verwendung gegenüber dem Dienstherrn
> zu prüfen, idealerweise mit Personalrat oder Verbands-Rechtsschutz.

**Inhalt:**
[Funktionen](#funktionen) ·
[So rechnet das Konto](#so-rechnet-das-konto) ·
[Beweiskraft](#beweiskraft) ·
[Datenschutz](#datenschutz) ·
[FAQ](#faq) ·
[Entwicklung](#entwicklung) ·
[Betrieb](#betrieb) ·
[Rechtsquellen](#rechtsquellen-zu-verifizieren)

Die genauen Rechenregeln (mit allen Sonderfällen) stehen in [docs/RECHENREGELN.md](docs/RECHENREGELN.md).

---

## Funktionen

Unten gibt es fünf Reiter: **Heute · Woche · Konto · Jahr · Mehr**. Unter *Mehr* liegen Einträge, Kalender,
Stundenplan, Monate & Berichte, Import, Export & Backup, Schreiben und Einstellungen.

| Bereich | Was er kann |
|---|---|
| **Heute** | Großer Start/Stopp-Knopf, Kategorie-Chips (Wechsel mit einem Tipp), *Pause* / *Weiter*, optional Klasse, Fach, Ort. Kacheln: Ist heute, Soll heute, Saldo gesamt, Woche Ist/Soll, nächste Ferien mit Abbau-Kapazität. Stundenplan-Vorschläge mit „Tag wie geplant“. Funktioniert **offline**: Timer-Aktionen werden auf dem Gerät gespeichert und später in der richtigen Reihenfolge übertragen – maßgeblich ist immer die Uhrzeit des Tipps, nicht die der Übertragung. |
| **Einträge** | Alle Einträge mit Filter; *Neu* bzw. *Nachtragen* mit Von–Bis oder „Nur Dauer“. Jede Änderung erzeugt eine **Revision** (Verlauf je Eintrag einsehbar), Löschen ist nur ein Ausblenden und lässt sich wiederherstellen. |
| **Stundenplan** | Wochenraster mit Zeitraster der Schule, Versionen mit Gültigkeit (Halbjahreswechsel). Für jeden Schultag entstehen Vorschläge, die erst nach Bestätigung zählen; einzelne Stunden lassen sich unter *Offene Tage* (`/tage`) bzw. pro Tag (`/tag/<datum>`, auch aus der Woche und den Hinweisen erreichbar) auf *entfallen*, *Vertretung gehalten*, *Exkursion* oder *Zeit angepasst* setzen. |
| **Kalender** | Einrichtungs-Assistent (Schuljahr → Ferien → Feiertage → Urlaub). Die **amtlichen bayerischen Ferientermine** (Schuljahre 2024/25 bis 2029/30) bringt die App mit und übernimmt sie beim Anlegen des Schuljahres automatisch (Kennzeichen „amtlich“, bearbeitbar); andere Termine per ICS-Datei oder von Hand. Bayerische Feiertage werden berechnet (regional: Mariä Himmelfahrt, Augsburger Friedensfest). Je Tag oder Zeitraum: krank, Fortbildung, Dienstbefreiung, Sonderurlaub, Elternzeit, unterrichtsfreier Tag. **Urlaubszuordnung** der 30 Urlaubstage: Sommerferien zuerst, proportional oder manuell. |
| **Konto** | Saldo-Verlauf über das Schuljahr (Ferien und Urlaub schattiert) mit Prognose-Linie. Kacheln: Saldo heute (in Stunden und Arbeitstagen), Prognose Periodenende, J_soll, Ist bisher, Ferienarbeit, Woche Ist / W_max, EU-12-Monats-Mittel mit Ampel, Schulwochen > 48 h. **Ferien-Guthaben-Rechner**, Vergleich der Urlaubsmodi, Umschalter Klassenfahrt-Variante A/B. |
| **Woche / Jahr** | Woche: Einträge als Blöcke, Ist/Soll je Tag, Wochen-Ist gegen Soll und W_max. Jahr: Heatmap aller Tage, Kategorienverteilung mit Unterrichtsanteil, Verteilung der Schulwochen (Marken bei 40 und 48 h), Monatsliste. |
| **Monate & Berichte** | **Monatsabschluss** mit Prüfung offener Punkte, Prüf-Hash und Sperre; Mehrarbeit (Art. 87 BayBG) je Monat mit Status. Monats- und Jahresbericht als HTML zum Drucken bzw. „Als PDF speichern“, mit vollständiger Eintragsliste und Änderungsprotokoll. |
| **Import** | Bestehende Aufzeichnungen aus CSV/XLSX in vier Schritten (Hochladen, Spalten zuordnen, Prüfen, Übernehmen). Dezimalstunden („2,5“) und „h:mm“ werden erkannt, Duplikate markiert, ein Import kann als Ganzes rückgängig gemacht werden. |
| **Export & Backup** | CSV (für Excel), JSON-Vollbackup, ICS (Kalenderansicht); CSV und ICS tragen einen Prüf-Hash. Nächtliches Datenbank-Backup, „Backup jetzt“, Wiederherstellen aus JSON. |
| **Schreiben** | Vorlagen, befüllt mit den eigenen Zahlen: Geltendmachung, Überlastungsanzeige, Bitte um Priorisierung, Antrag Freizeitausgleich/Mehrarbeitsvergütung. Jede Vorlage trägt den nicht entfernbaren Hinweis „Vorlage, keine Rechtsberatung“. „Als versendet markieren“ – das Konto zeigt danach „Geltendmachung wirksam ab …“. |
| **Benchmarks** | Wochenwerte aus Arbeitszeitstudien (z. B. Grundschule Niedersachsen 2015/16: 47:58 h) zum Einblenden in *Woche* – ausdrücklich „Studienwert, nicht Norm“, Quelle in `config/benchmarks.yaml`. |
| **Hinweise** | Timer läuft > 6 h, Schultag ohne Eintrag, unbestätigter Stundenplan, Monatsabschluss fällig, Backup fehlgeschlagen. |
| **Home Assistant** | `GET /api/sensors` (nur mit Bearer-Token, unter *Einstellungen → Sensor-API*): Saldo, Ist heute/Woche, EU-Mittel mit Ampel, Timer läuft, Prognose. Beispiel-Konfiguration in [DEPLOY.md](DEPLOY.md#home-assistant). |

---

## So rechnet das Konto

### Die Idee in einem Satz

Neben der Lehrkraft läuft ein gedachter **Referenz-Beamter** mit: Montag bis Freitag je 8 Stunden Soll,
0 an Wochenenden, Feiertagen und an seinen 30 Urlaubstagen. Das **Konto** ist die laufende Differenz:

> **Saldo = alles, was erfasst wurde (Ist) − was der Referenz-Beamte bis heute gearbeitet hätte (Soll)**

Plus heißt: mehr gearbeitet als ein 40-Stunden-Beamter. Minus heißt: weniger. Der Saldo wird auch in
Arbeitstagen angezeigt (Saldo ÷ 8 h).

### Welcher Tag wie viel Soll hat (Vollzeit)

| Tag | Soll |
|---|---|
| Schultag | 8:00 |
| Ferientag (Mo–Fr), der **kein** Urlaubstag ist | **8:00** – hier wird das Konto abgebaut |
| Urlaubstag (30 Stück, liegen in den Ferien) | 0 |
| Unterrichtsfreier Tag (z. B. Buß- und Bettag), Fortbildungstag | 8:00 |
| Wochenende, gesetzlicher Feiertag | 0 |
| Dienstbefreiung, Sonderurlaub, Elternzeit | 0 |
| Krankheitstag | neutral – zählt so, als hätte sie genau das Soll gearbeitet |

Warum haben Ferientage Soll? Weil Lehrkräfte rechtlich nur 30 Tage Urlaub haben wie alle Beamten. Die übrigen
Ferien-Werktage sind keine Freizeit, sondern der Ausgleich für die langen Schulwochen. Genau diese Rechnung des
Dienstherrn („das gleicht sich übers Jahr aus“) macht Deputat nachprüfbar.

### Ein durchgerechnetes Beispiel

Vollzeit, Tages-Soll 8:00. Schuljahr 01.08.2026–31.07.2027 mit den amtlichen Ferien (die Beispieldatei
`examples/ferien_bayern_BEISPIEL.ics` hat für dieses Schuljahr dieselben Ferientermine), Urlaub „Sommerferien
zuerst“.

**Einzelne Wochen:**

| Woche | Soll | Ist (erfasst) | Saldo-Änderung |
|---|---|---|---|
| Normale Schulwoche | 40:00 | 46:30 | **+6:30** |
| Schulwoche mit Feiertag am Donnerstag (z. B. Christi Himmelfahrt) | 32:00 | 38:00 | **+6:00** |
| Schulwoche, Mittwoch krank (im Kalender eingetragen) | 40:00 | 4 Tage à 9:00 = 36:00, + 8:00 für den Krankheitstag | **+4:00** |
| Herbstferien, 5 Werktage, kein Urlaub | 40:00 | 6:00 (Korrekturen) | **−34:00** |
| Sommerferien-Woche mit 5 Urlaubstagen | 0:00 | 4:00 (Vorbereitung) | **+4:00** |

**Das ganze Schuljahr** (so rechnet Deputat mit diesen Ferien):

- 260 Werktage − 8 Feiertage an Werktagen − 30 Urlaubstage = 222 Tage mit Soll → **J_soll = 222 × 8 h = 1.776 h**.
- Davon sind 185 Schultage (= 37 Schulwochen) und 37 Ferien-/unterrichtsfreie Tage ohne Urlaub
  (36 Ferien-Werktage + Buß- und Bettag). Diese 37 Tage bauen bis zu **296 h** ab.
- Alle 30 Urlaubstage landen in den Sommerferien (die haben hier 31 Werktage; der 31. hat 8 h Soll).
- **W_max** = 1.776 h ÷ 37 Schulwochen = **48:00 h**. Heißt: Wer in jeder Schulwoche 48 Stunden arbeitet und in
  den Ferien (außer Urlaub) gar nicht, steht am 31.07. genau bei 0 – also gleichauf mit dem 40-Stunden-Beamten.
  Jede Stunde Ferienarbeit senkt W_max; plant sie z. B. 60 h Ferienarbeit ein, sind es (1.776 − 60) ÷ 37 ≈ 46,4 h.
- Zum Vergleich: Arbeitszeitstudien messen bei Grundschullehrkräften Durchschnittswochen um 48–50 h
  *über das ganze Jahr* (inkl. Ferien) – das wäre deutlich über dem Referenz-Soll.

**Ferien-Guthaben-Rechner** (Konzept-Beispiel): Saldo vor den Herbstferien +83 h. Die 5 Ferien-Werktage bauen
höchstens 40 h ab. Wenn sie wie zuletzt 12 h in den Ferien arbeitet, kommt sie mit 83 − 40 + 12 = **+55 h** zurück.

### Teilzeit, Altersermäßigung, Anrechnungsstunden

- **Teilzeitfaktor** f = Vertrags-UPZ ÷ Vollzeit-UPZ. Bei 21 von 28 UPZ ist f = 0,75: Tages-Soll 6:00,
  Wochen-Soll 30:00. Im Beispieljahr: J_soll 1.332 h, W_max 36:00. Urlaubstage bleiben 30 (5-Tage-Woche).
- **Altersermäßigung und Anrechnungsstunden senken das Soll nicht** – sie verringern nur den Unterrichtsanteil.
  In den Einstellungen ist „UPZ-Ist“ die tatsächlich gehaltene Unterrichtsmenge; die App zählt Ermäßigung und
  Anrechnung für den Faktor wieder dazu. Beispiel: UPZ-Ist 26 + 2 Altersermäßigung = 28 → f = 1,0.
- Die Altersermäßigung wird aus dem Geburtsdatum berechnet (−1 ab 58, −2 ab 60, −3 ab 62 Jahren, mit
  Schuljahres-Stichtag) und kann überschrieben werden.

### Krankheit

- **Ganzer Tag:** im *Kalender* den Tag (oder einen Zeitraum) auf *krank* setzen. Standard: Das Konto bleibt
  unverändert (Einstellung *Krankheit neutralisiert das Soll*). Wird an einem solchen Tag trotzdem etwas erfasst,
  zählt es zusätzlich.
- **Halber Tag:** einen Eintrag mit der Kategorie **„Krank (Teil)“** anlegen (z. B. 3:00). Diese Zeit füllt nur
  die Lücke bis zum Tages-Soll auf, erzeugt nie ein Plus und taucht in keiner Kategorie-Statistik auf.
- Ein Krankheitstag in den Ferien ist kein Urlaubstag – der Urlaubstag rutscht automatisch auf den nächsten
  freien Ferientag.

### Klassenfahrt

Die echte Dienstzeit erfassen (z. B. 07:00–22:30) mit Kennzeichen *Klassenfahrt*, die Nachtbereitschaft als
eigenen Eintrag mit Kennzeichen *Nachtbereitschaft*. Die App rechnet zwei Varianten:

- **Variante A:** alle Stunden inklusive Nachtbereitschaft.
- **Variante B** (Standard, vorsichtig): ohne Nachtbereitschaft, und jeder Fahrttag wird auf **10 h** gekappt
  (einstellbar). Gekappt wird die Summe *aller* Einträge des Tages.

Das Konto rechnet mit der Variante aus den Einstellungen; unter *Konto → Ansicht umschalten* lässt sich die andere
ansehen, der Jahresbericht zeigt beide.

### Mehrarbeit ist nicht Zuvielarbeit

| | **Mehrarbeit** (eng) | **Zuvielarbeit** (weit) |
|---|---|---|
| Was | angeordnete oder genehmigte **zusätzliche Unterrichtsstunden** (Vertretungen), Art. 87 BayBG | alles, was im Jahresdurchschnitt über 40 h/Woche hinausgeht |
| In Deputat | Unterrichts-Eintrag mit Kennzeichen *Vertretung angeordnet* oder *genehmigt*; Zähler unter *Monate* | der **Saldo** des Kontos |
| Regel | Ausgleich erst, wenn im Monat **mehr als 3** Unterrichtsstunden anfallen – dann ab der ersten (3 UStd → 0, 4 UStd → 4). Freizeitausgleich binnen drei Monaten, sonst Vergütung beantragen. | Ansprüche entstehen frühestens ab dem Monat **nach einer schriftlichen Geltendmachung**; nach BVerwG 2 C 19.25 außerdem nur, wenn eine **Überlastungsanzeige mit Bitte um Priorisierung** vorliegt. |

Deshalb sind **Geltendmachung** und **Überlastungsanzeige** wichtig: Ohne sie zählt der Saldo rechtlich womöglich
nicht, egal wie gut er belegt ist. Beide Schreiben erzeugt *Mehr → Schreiben* aus den eigenen Zahlen – als Vorlage,
die vor dem Versand zu prüfen ist. Nach „Als versendet markieren“ zeigt das Konto „Geltendmachung wirksam ab
[Erster des Folgemonats]“.

### EU-48-Stunden-Grenze

Die EU-Arbeitszeitrichtlinie erlaubt höchstens 48 h pro Woche im Durchschnitt. Deputat rechnet das Mittel über
die **letzten 365 Tage**: erfasste Zeit ÷ (52 Wochen − Urlaubs- und Krankheitstage ÷ 5). Tage, bevor die
Aufzeichnung begann, werden nicht als „0 Stunden“ gezählt, sondern herausgerechnet. Ampel: **grün unter 44 h,
gelb 44–48 h, rot über 48 h.** Zusätzlich zählt die App die Schulwochen mit mehr als 48 h.

*Offener Punkt:* Ein Konzept-Beispiel (43,9 h) erwartet „gelb“, nach den festgelegten Schwellen ist es „grün“ –
umgesetzt sind die Schwellen. Details: [docs/RECHENREGELN.md](docs/RECHENREGELN.md#eu-12-monats-mittel-höchstgrenze-48-h).

### Prognose und W_max

- **Prognose Periodenende:** Saldo heute + (Durchschnitt der letzten 20 Schultage − Soll) für jeden restlichen
  Schultag − Soll der restlichen Ferientage + geplante Ferienarbeit.
- **W_max:** (J_soll − Ferienarbeit bisher − geplante Ferienarbeit) ÷ Zahl der Schulwochen. Das ist die
  Wochenarbeitszeit in Schulwochen, bei der das Jahr genau aufgeht.

---

## Beweiskraft

Deputat ist darauf ausgelegt, dass die Aufzeichnungen einer Prüfung standhalten:

- **Revisionen:** Jede Änderung oder Löschung speichert den vollständigen vorherigen Stand (*Einträge →
  Eintrag → Verlauf*). Nichts wird still überschrieben, nichts wird endgültig gelöscht.
- **„nachträglich“:** Ein Eintrag, der **mehr als 48 Stunden nach Ende seines Tages** angelegt wird, bekommt
  dieses Kennzeichen automatisch – sichtbar in Listen, Berichten und Exporten, nicht entfernbar.
- **„korrigiert“:** wenn Start/Ende eines Timer-Eintrags nachträglich geändert werden oder ein Eintrag in einem
  abgeschlossenen Monat geändert wird. Ebenfalls nicht entfernbar.
- **„ohne Zeitgrenzen“:** Einträge nur mit Dauer (z. B. aus einem Import) – geringere Beweiskraft, deshalb
  wenn möglich Von–Bis erfassen.
- **Monatsabschluss** (*Monate → Monat → Monat abschließen*): Die App prüft offene Punkte (unbestätigte
  Stundenplan-Vorschläge, Schultage ohne Eintrag), berechnet einen **Prüf-Hash (SHA-256)** über alle Einträge
  des Monats, sperrt die Einträge und legt den Bericht unveränderlich als `/data/reports/JJJJ-MM.html` ab.
  Ein abgeschlossener Monat lässt sich nicht wieder öffnen; Korrekturen brauchen eine Begründung (siehe FAQ).
- **Empfehlung:** Nach jedem Monatsabschluss den Monatsbericht öffnen, als PDF speichern und **an die eigene
  Mailadresse schicken**. Der Zeitstempel des Mailservers ist ein Nachweis von außen, dass der Stand mit diesem
  Hash zu diesem Zeitpunkt existierte.

### Einen Prüf-Hash nachprüfen

Der Hash ist ein „Fingerabdruck“ der Einträge: Ändert sich auch nur eine Sekunde oder ein Zeichen einer Notiz,
ergibt sich ein völlig anderer Hash. Er wird über die Einträge gebildet (nicht über Kalender oder Einstellungen).
Monats- und Jahreshash enthalten auch gelöschte Einträge – eine Löschung bleibt so sichtbar.

**1. Ganz einfach – in der App vergleichen**
Den Hash aus dem gemailten Bericht (Kopf und Fußzeile) mit *Monate → [Monat] → Prüf-Hash* vergleichen.
Gleich → seit dem Abschluss unverändert. Wurde der Monat nachträglich korrigiert, steht dort „Aktueller Prüf-Hash“,
und der alte Hash erscheint unter *Frühere Hashes* mit Datum und Begründung der Korrektur.

**2. Über den CSV-Export**
*Mehr → Export & Backup → CSV*: Von = erster, Bis = letzter Tag des Monats, Häkchen **„Gelöschte Einträge mit
ausgeben“** setzen, herunterladen. Die letzte Zeile der Datei lautet
`Prüf-Hash;SHA-256 v1;<hash>;<n> Einträge` – dieser Hash ist gleich dem Monats-Hash, solange sich nichts
geändert hat. (Ohne das Häkchen fehlen gelöschte Einträge, dann ist der CSV-Hash ein anderer – er passt aber
weiterhin genau zu den Zeilen der Datei.)

**3. Unabhängig nachrechnen (technisch)**
Für Dritte, die der App nicht vertrauen wollen: Aus einem JSON-Vollbackup lässt sich jeder Hash mit einem kurzen
Python-Skript ohne Zusatzpakete nachrechnen. Das Skript `pruef_hash.py` und die genaue Rechenvorschrift stehen in
[docs/RECHENREGELN.md](docs/RECHENREGELN.md#8-prüf-hash-appcorehashingpy).

```bash
# JSON-Backup: Mehr → Export & Backup → „JSON-Backup herunterladen“
python pruef_hash.py deputat_backup_20261005-080000.json 2026-09-01 2026-09-30
# → 4 Einträge, SHA-256: cbd4b46d…   (= Monats-Hash September)
python pruef_hash.py deputat_backup_….json 2026-08-01 2027-07-31           # = Hash des Jahresberichts
python pruef_hash.py deputat_backup_….json 2026-09-01 2026-09-30 --ohne-geloeschte   # = CSV ohne Häkchen
```

Zusätzlich prüft die App beim **Wiederherstellen** eines JSON-Backups jeden gespeicherten Monats-Hash gegen die
wiederhergestellten Einträge und lehnt die ganze Wiederherstellung ab, wenn einer nicht passt.

---

## Datenschutz

- **Keine Namen von Kindern oder Eltern** in Notizen – stattdessen z. B. „Gespräch Übertritt, 4a“. Klassen
  werden aus einer Liste gewählt. Das Notizfeld erinnert daran.
- Deputat speichert keine Schülerdaten, keine Noten, kein Klassenbuch.
- Alle Daten bleiben auf dem eigenen Server (eine SQLite-Datei); keine externen Dienste, keine Telemetrie,
  alle Bibliotheken liegen in der App.
- Die App hat **keinen Login**: Von außen schützt Cloudflare Access, im Heimnetz ist sie bewusst offen – jedes
  Gerät im Heimnetz kann sie öffnen und Exporte/Backups laden (Begründung in [DEPLOY.md](DEPLOY.md#sicherheitsmodell-bewusste-entscheidung-des-betreibers)).
- Exporte und Backups enthalten alle Arbeitszeitdaten – entsprechend sorgsam weitergeben.

---

## FAQ

**Warum ist mein Saldo in den Sommerferien negativ?**
Das Schuljahr beginnt am 01.08., mitten in den Sommerferien, und jedes Schuljahr startet bei 0. Mit „Sommerferien
zuerst“ sind die ersten 30 Werktage der Sommerferien Urlaub (Soll 0) – das Konto bleibt dort neutral. Negativ wird
es, wenn die Sommerferien mehr als 30 Werktage haben (jeder weitere Ferientag hat 8 h Soll), wenn der Urlaubsmodus
„proportional“ gewählt ist (dann liegen nur etwa die Hälfte der Urlaubstage im Sommer, der Rest der Sommerferien
baut ab) oder wenn die Ferien im Kalender fehlen (dann gelten die Tage als Schultage). Die Woche vor Schulbeginn ist
oft Vorbereitungszeit – die gehört erfasst, sie gleicht genau diesen Abbau aus. In den Schulwochen baut sich das
Konto wieder auf; entscheidend ist der Saldo am Periodenende (Kachel *Prognose Periodenende*).

**Ich habe erst mitten im Schuljahr angefangen – warum ist das Konto so tief im Minus?**
Ohne weitere Angabe zählt jeder Schultag seit Periodenbeginn, an dem nichts erfasst ist, als „0 Stunden
gearbeitet“. Trag unter *Einstellungen → Arbeitszeitkonto → Erfassung ab* (oder im letzten Schritt des
Einrichtungsassistenten) den ersten Tag deiner Erfassung ein: Die Tage davor zählen dann neutral.

**Ich habe erst im Oktober angefangen – warum steht da ein großes Minus?**
Für August bis Oktober fehlen Einträge, das Soll läuft aber seit dem 01.08. Entweder die bisherigen
Aufzeichnungen über *Mehr → Import* übernehmen (empfohlen), oder beim Anlegen des Schuljahrs einen
*abweichenden Zeitraum* wählen, der erst mit der Aufzeichnung beginnt. Im zweiten Fall verteilt die App die
Urlaubstage auf die verbleibenden Ferien – die Urlaubszuordnung im Kalender dann prüfen.

**Was passiert, wenn ich vergesse, den Timer zu stoppen?**
Nach 6 Stunden erscheint ein Hinweis. Läuft er über Mitternacht und wird bis 4 Uhr gestoppt, wird der Eintrag an
Mitternacht geteilt (z. B. 22:30–24:00 und 00:00–01:15). Später wird er automatisch um 24:00 des Starttags beendet;
die tatsächliche Endzeit dann im Eintrag korrigieren (er bekommt das Kennzeichen *korrigiert*) und spätere Arbeit
nachtragen.

**Wie trage ich Krankheit ein?**
Ganzer Tag: *Kalender* → Tag antippen → Abwesenheit *krank* (mehrere Tage: *Zeitraum eintragen*). Halber Tag:
*Einträge → Neu*, Kategorie „Krank (Teil)“, Dauer oder Von–Bis. Beides lässt das Konto unverändert
(siehe [Krankheit](#krankheit)).

**Wie erfasse ich eine Klassenfahrt?**
Die Dienstzeit jedes Tages als Eintrag (Kategorie *Schulveranstaltungen & Fahrten*) mit Kennzeichen *Klassenfahrt*,
die Nachtbereitschaft als eigenen Eintrag mit Kennzeichen *Nachtbereitschaft*. Gezählt wird nach Variante B
(10 h je Tag, ohne Nacht), sofern in den Einstellungen nichts anderes steht – siehe [Klassenfahrt](#klassenfahrt).

**Ich arbeite Teilzeit – was muss ich einstellen?**
*Einstellungen → Unterrichtspflichtzeit*: UPZ-voll (Grundschule 28) und UPZ-Ist (z. B. 21). Daraus ergibt sich
der Teilzeitfaktor; Soll und W_max skalieren automatisch, Urlaubstage bleiben 30. Altersermäßigung und
Anrechnungsstunden separat eintragen – sie senken das Soll nicht.

**Was bedeutet W_max?**
Die Wochenarbeitszeit, die in Schulwochen „erlaubt“ ist, damit das Jahr genau bei 0 endet – vorausgesetzt, in den
Ferien wird so viel gearbeitet wie bisher plus geplant. Liegt die Schulwoche darüber, wächst das Plus.

**Warum steht bei einem Eintrag „nachträglich“?**
Er wurde mehr als 48 Stunden nach dem Ende seines Tages angelegt (für einen Montag also ab Donnerstag 0 Uhr). Das
ist kein Fehler, sondern Ehrlichkeit gegenüber einem Prüfer; das Kennzeichen lässt sich nicht entfernen. Am besten
noch am selben oder nächsten Tag nachtragen.

**Wie korrigiere ich einen abgeschlossenen Monat?**
Den Eintrag ganz normal öffnen und ändern bzw. löschen – bei gesperrten Monaten ist das Feld *Grund* Pflicht.
Die Änderung wird als Revision gespeichert, der Eintrag bekommt *korrigiert*, der Monat den Status
„nachträglich korrigiert“; der alte Hash bleibt mit Datum und Begründung erhalten, der neue kommt dazu. Dasselbe
gilt für Kalenderänderungen (z. B. Krankheitstag nachtragen), die einen abgeschlossenen Monat betreffen. Wieder
„öffnen“ lässt sich ein Monat nicht. Den gespeicherten Abschlussbericht überschreibt die App nie.

**Funktioniert die App offline?**
Ja, für den Timer: Start, Stopp, Wechsel und Pause werden auf dem Handy zwischengespeichert und automatisch
übertragen, sobald wieder Netz da ist – mit der Uhrzeit des Tipps. Zuletzt geladene Seiten sind offline lesbar;
Einträge bearbeiten, Berichte und Exporte brauchen eine Verbindung. Ist die Cloudflare-Anmeldung abgelaufen, zeigt
die App „Sitzung abgelaufen – bitte Seite neu laden“; die gespeicherten Timer-Aktionen bleiben erhalten.

**Wie spiele ich ein Backup zurück?**
JSON-Backup: *Mehr → Export & Backup → Wiederherstellen*, Datei wählen, „ERSETZEN“ eintippen. Achtung: Alle
aktuellen Daten werden durch den Stand der Sicherung ersetzt (vorher legt die App automatisch eine
Sicherheitskopie an). Ein JSON-Backup passt nur zur selben Datenbank-Version der App. Die nächtlichen
Datenbank-Backups (`/data/backups/JJJJ-MM-TT.sqlite.gz`) werden auf dem Server eingespielt – Anleitung in
[DEPLOY.md](DEPLOY.md#daten-backup-wiederherstellung); dabei laufen nötige Migrationen automatisch.

**Wo kommen die Ferien her?**
Aus dem mitgelieferten Ferienkatalog `config/ferien_bayern.yaml`: die amtlichen Termine der Ferienordnung des
Bayerischen Kultusministeriums (BayMBl. 2022 Nr. 747, Schuljahre 2024/25 bis 2029/30, Stand 30.10.2026; abgeglichen
mit dem Ferienkalender-Feed des Ministeriums). Legst du ein Schuljahr an, das der Katalog abdeckt, übernimmt die App
die Ferien automatisch – ein Import ist nicht nötig. In der Ferienliste tragen sie das Kennzeichen „amtlich“ und den
Quellenlink; du kannst sie bearbeiten (danach entfällt das Kennzeichen). Sommerferien, die vor dem 01.08. beginnen,
werden an der Schuljahresgrenze geteilt: Die Tage bis 31.07. gehören zum alten Schuljahr. Buß- und Bettag steht nicht
im Katalog – die App berechnet ihn selbst als unterrichtsfreien Tag mit vollem Soll (Lehrkräfte haben Dienst).
Für Schuljahre ohne amtliche Termine (z. B. 2030/31: bisher sind nur die Sommerferien festgelegt) zeigt die App einen
Hinweis; dann die Ferien per ICS importieren oder von Hand erfassen. Berufsschulen dürfen um bis zu zwei Tage
abweichen – eigene Termine einfach bearbeiten oder per ICS („Andere Termine importieren“) übernehmen. Ein
aktualisierter Katalog kann im Datenvolume liegen (`FERIEN_CATALOG_FILE`, siehe [DEPLOY.md](DEPLOY.md)).

**Ich habe die Beispiel-Ferien importiert – was nun?**
`examples/ferien_bayern_BEISPIEL.ics` enthält ausdrücklich Beispieldaten. Erkennt die App Ferien mit „BEISPIEL“ im
Namen, bietet sie im Kalender „Amtliche Ferien übernehmen“ an und ersetzt sie nach Rückfrage durch die amtlichen
Termine (betrifft das einen abgeschlossenen Monat, nur mit Begründung). Den regionalen Feiertag Mariä Himmelfahrt
für den Schulort trotzdem prüfen.

---

## Entwicklung

Python 3.12 über [uv](https://docs.astral.sh/uv/), FastAPI, SQLAlchemy 2 + Alembic, SQLite (WAL),
Jinja2 + HTMX + Alpine.js (CSP-Build) + Chart.js – **ohne Build-Schritt**, alle JS-Bibliotheken liegen unter
`app/static/vendor/`. Python immer über `uv run` aufrufen.

### Einrichten und starten

```bash
uv sync                    # Abhängigkeiten inkl. Dev-Gruppe
cp .env.example .env       # DATA_DIR=./data, BASE_URL=http://localhost:8000
make dev                   # = uv run --env-file .env uvicorn app.main:app --reload --port 8000
```

Ohne `make` (z. B. Windows): `uv run uvicorn app.main:app --reload`. Beim Start migriert die App die Datenbank
automatisch (`alembic upgrade head`) und legt Einstellungen und Kategorien an. Danach
`http://localhost:8000` öffnen; der Assistent unter *Kalender* führt durch die Einrichtung.

| Make-Ziel | Zweck |
|---|---|
| `make dev` | Dev-Server mit Reload (liest `.env`, falls vorhanden; Port über `PORT=…`) |
| `make test` | komplette Testsuite |
| `make test-core` | nur Rechenkern (`tests/core`) |
| `make migrate` | `alembic upgrade head` |
| `make revision m="…"` | neue Migration per Autogenerate |
| `make build` / `make up` / `make down` / `make logs` | Docker-Image bauen, Compose starten/stoppen, Logs |
| `make backup` | Backup im laufenden Container anstoßen |

### Tests

```bash
uv run pytest -q                    # alles außer Browser-Tests
uv run pytest tests/core -q         # Rechenkern: Konzept-Tests T1–T20 + hypothesis-Eigenschaften
uv run playwright install chromium  # einmalig für die Browser-Smoke-Tests
uv run pytest -m e2e                # Playwright-Smoke (tests/e2e), sonst automatisch abgewählt
```

`tests/core` (reiner Rechenkern), `tests/services` (Fachlogik mit DB), `tests/api` (JSON-API), `tests/web`
(HTML-Seiten), dazu Sicherheits- und Migrationstests im Wurzelverzeichnis von `tests/`. Die Fixtures in
`tests/conftest.py` liefern eine frisch migrierte SQLite-Datei je Test (`db`) und einen `TestClient` (`client`).
Bekannte Abweichung: `test_t18_ampel_expectation_gelb` ist als *xfail* markiert (offene Spezifikationsfrage, siehe
[EU-48-Stunden-Grenze](#eu-48-stunden-grenze)).

### Projektstruktur

```
app/
  core/        Rechenkern, nur Standardbibliothek (Soll, Urlaub, Konto, Mehrarbeit, Mitternacht, Hash, Klassenfahrt)
  services/    Fachlogik mit DB; ledger.py ist der einzige Weg von der DB in den Rechenkern,
               entries.py der einzige Schreibweg für Zeiteinträge (Revisionen, Sperren, Kennzeichen)
  api/         JSON-API unter /api
  web/         HTML-Seiten (Jinja2 + HTMX), templates/
  db/          ORM-Modelle, Session, Seed
  static/      CSS, JS (Timer, Offline-Queue, Charts), vendor/, Service Worker, Manifest
  templates_docs/  Schreiben-Vorlagen (Markdown)
alembic/       Migrationen
config/        benchmarks.yaml (Studienwerte), ferien_bayern.yaml (amtliche Ferientermine Bayern)
examples/      Beispiel-Ferien-ICS (nicht amtlich)
tests/         core/, services/, api/, web/, e2e/
docs/          ARCHITECTURE.md, RECHENREGELN.md
```

Architektur, Konventionen und Schnittstellen: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (bei Widerspruch gilt
fachlich das nicht veröffentlichte Fachkonzept). Code und Kommentare sind Englisch, die
Oberfläche Deutsch.

### Eine Migration hinzufügen

1. Modell in `app/db/models.py` ändern.
2. Migration erzeugen, mit fortlaufender Nummer als Revision:
   `uv run alembic revision --autogenerate --rev-id 0005 -m "kurze beschreibung"`
   (oder `make revision m="…"`, dann die zufällige Revision-ID von Hand auf die nächste Nummer setzen).
3. Datei in `alembic/versions/` prüfen und nachbessern: SQLite braucht `op.batch_alter_table` (Autogenerate
   erzeugt das dank `render_as_batch`), neue Pflichtspalten brauchen ein `server_default`, `downgrade()` füllen.
4. `tests/test_migrations_match_models.py` auf die neue Head-Revision anpassen und `uv run pytest -q` laufen
   lassen – der Test prüft, dass die Migrationen exakt das Schema der Modelle ergeben.
5. Bestehende Datenbanken werden beim nächsten App-Start automatisch migriert. JSON-Backups lassen sich nur in
   eine Datenbank mit derselben Schema-Revision zurückspielen.

---

## Betrieb

Deputat läuft als ein Container (`ghcr.io/the-walker443/deputat`, gebaut von GitHub Actions) hinter Nginx Proxy
Manager, extern über Cloudflare Tunnel + Cloudflare Access unter `https://time.bauer.center`. Alles Schreibende
liegt im Volume `/data` (Datenbank, Backups, Berichte, Exporte). Variablen, Erst-Inbetriebnahme, Backup,
Wiederherstellung, Updates und Home-Assistant-Anbindung: **[DEPLOY.md](DEPLOY.md)**.

---

## Rechtsquellen (zu verifizieren)

Die App bildet die folgenden Regeln ab (Stand Oktober 2026, Konzept Anhang A). **Alle Angaben sind vor einer
Verwendung zu verifizieren** – Rechtsstand und Fundstellen können sich geändert haben.

- § 2 BayAzV – regelmäßige Arbeitszeit 40 h im Durchschnitt, Höchstgrenze 48 h über 12 Monate, Feiertagsminderung,
  Pausen keine Arbeitszeit *(zu verifizieren)*
- Art. 87 BayBG; KMBek „Mehrarbeit im Schulbereich“ – Bagatellgrenze drei Unterrichtsstunden im Monat,
  Freizeitausgleich binnen drei Monaten *(zu verifizieren)*
- KMBek Unterrichtspflichtzeit Grund- und Mittelschulen – 28 UPZ, Altersermäßigung, Anrechnungsstunden *(zu verifizieren)*
- § 3 UrlMV – 30 Urlaubstage, Abgeltung durch die unterrichtsfreie Zeit bei Lehrkräften *(zu verifizieren)*
- § 9a LDO – zumutbare Verfügbarkeit in den Ferien *(zu verifizieren)*
- Richtlinie 2003/88/EG; EuGH C-55/18 (CCOO); EuGH C-429/09 (Fuß) *(zu verifizieren)*
- BVerwG 2 C 70.11, 2 C 36.16, 2 C 8.19 – schriftliche Geltendmachung, Textform, stundenbezogener Ausgleich
  *(zu verifizieren)*
- BVerwG 2 C 19.25 vom 17.09.2026 – Heranziehung, Überlastungsanzeige, Zurückstellen von Aufgaben *(zu verifizieren)*
- BayVGH 12.11.2024 – Arbeitszeitkonto-Verordnung §§ 12–15 AZKoV unwirksam; relevant für „Ansparstunden 2020–2025
  prüfen“ *(zu verifizieren)*

Benchmark-Werte (`config/benchmarks.yaml`) sind Studienwerte bzw. Verbandsschätzungen, keine Norm; Quelle und Jahr
der BLLV-Hochrechnung sind ebenfalls zu prüfen.
