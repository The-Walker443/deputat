# Schreiben-Vorlagen (Konzept §11, L7)

Je Typ eine Markdown-Datei `<typ>.md` mit Platzhaltern `{{name}}` (keine Logik). Gerendert wird in
`app/services/documents.py`; dort wird auch der **nicht entfernbare Hinweis** vorangestellt
(„Vorlage, keine Rechtsberatung. Vor Versand prüfen, idealerweise mit Personalrat oder
Verbands-Rechtsschutz.“) – beim Rendern, Speichern und bei der HTML-Ansicht, unabhängig vom Dateiinhalt.

| Typ | Datei | Adressat (Vorschlag) |
|---|---|---|
| Geltendmachung | `geltendmachung.md` | Staatliches Schulamt auf dem Dienstweg über die Schulleitung, Abdruck Personalrat |
| Überlastungsanzeige | `ueberlastungsanzeige.md` | wie oben |
| Bitte um Priorisierung | `prioritaetsbitte.md` | Schulleitung |
| Antrag Freizeitausgleich / Mehrarbeitsvergütung | `antrag_freizeitausgleich.md` | Schulleitung zur Weiterleitung an das Staatliche Schulamt |

Formulierungsregeln: sachlich, Beamten-Schriftverkehr; nie einen Anspruch als Tatsache behaupten
(Anhang B: keine Rechtsbewertung); Rechtsquellen nur aus Konzept Anhang A, als „vgl. …“ und mit dem
Zusatz „ohne Gewähr, Rechtsstand vor Versand prüfen“.

## Platzhalter

Alle Werte sind Text; Zahlen deutsch formatiert, Stunden dezimal mit 2 Stellen. Unbekannte Platzhalter
erscheinen als `[name?]`, damit Lücken auffallen.

| Platzhalter | Inhalt |
|---|---|
| `name`, `schule` | aus den Einstellungen (sonst `[Name]` / `[Schule]`) |
| `ort` | Eingabe (sonst `[Ort]`) |
| `datum` | Datum des Schreibens (heute, änderbar) |
| `adressat`, `dienstweg`, `abdruck` | Vorschlag je Typ, änderbar; leeres `abdruck` entfernt die Zeile „Abdruck:“ |
| `periode_label` | Kontoperiode, z. B. `2026/27` |
| `zeitraum_von`, `zeitraum_bis` | ausgewerteter Zeitraum (auf die Kontoperiode begrenzt) |
| `soll_h` | Referenz-Soll im Zeitraum |
| `ist_h` | angerechnete Ist-Zeit (inkl. Krank-Neutralisierung) |
| `erfasst_h` | erfasste Zeit (Klassenfahrt-Variante angewendet) |
| `saldo_periode_h` / `saldo_periode_hm` | Ist − Soll im Zeitraum (mit Vorzeichen; dezimal / h:mm) |
| `saldo_kumuliert_h` | Saldo der Kontoperiode am Ende des Zeitraums |
| `schulwochen_avg_h`, `schulwochen_anzahl` | Ø erfasste Stunden in vollen Schulwochen und deren Anzahl |
| `schulwochen_ueber_48` | Schulwochen mit mehr als 48 h |
| `wochen_soll_h`, `upz`, `ustd_minuten` | regelmäßige Wochenarbeitszeit, UPZ, Minuten je UStd |
| `eu_mittel_h`, `eu_fenster_von`, `eu_fenster_bis` | 12-Monats-Mittel und Fenster |
| `kategorien_tabelle` | Tabelle Tätigkeitsbereich · Stunden · Anteil |
| `belastungen_liste` | Liste der Bereiche mit Stunden, Anteil, Ø je Schulwoche |
| `aufgaben_liste` | Aufgaben aus FS/KV/B (mit Unterkategorien und Stunden) |
| `monatsabschluesse` | abgeschlossene Monate im Zeitraum mit Kurz-Hash |
| `monat` | (Antrag) Monat, z. B. `Oktober 2026` – Auswahl über `monat=JJJJ-MM` |
| `ustd_angeordnet`, `ustd_genehmigt`, `ustd_gesamt`, `ustd_ausgleichsfaehig` | Mehrarbeit im engen Sinn |
| `mehrarbeit_tabelle` | Tabellenzeilen Datum · Dauer · UStd · Grundlage |
| `freizeitausgleich_frist` | Ende des dritten Folgemonats |
