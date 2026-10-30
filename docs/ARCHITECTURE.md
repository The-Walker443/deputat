# Deputat – Architektur-Vertrag (verbindlich für alle Umsetzungsschritte)

Fachliche Quelle: das Fachkonzept (nicht im öffentlichen Repo; Kapitelverweise „Konzept §x“ im Code beziehen sich darauf). Dieses Dokument legt
die technischen Schnittstellen fest, damit mehrere Agenten parallel an disjunkten Dateien arbeiten können.
Wenn dieses Dokument und das Konzept kollidieren, gilt das Konzept fachlich, dieses Dokument technisch.

## Laufzeit / Tooling

- Python 3.12 via **uv** (Windows-Entwicklungsrechner, kein `python` im PATH). Immer `uv run ...`:
  - Tests: `uv run pytest` (oder `uv run pytest tests/core -q`)
  - Dev-Server: `uv run uvicorn app.main:app --reload`
  - Abhängigkeiten stehen in `pyproject.toml` und sind installiert. **Keine neuen Abhängigkeiten hinzufügen**
    ohne zwingenden Grund; falls doch: `uv add <pkg>` und im Ergebnis melden.
- Kein Docker lokal verfügbar → Dockerfile wird geschrieben, aber nicht lokal gebaut.
- Code, Bezeichner, Kommentare: Englisch. UI-Texte: Deutsch. Domänenbegriffe (upz, soll, ist, saldo, ferien,
  urlaub, krank, mehrarbeit, klasse, fach) dürfen im Code bleiben.
- Keine externen Requests zur Laufzeit. Alle JS-Libs liegen unter `app/static/vendor/`.

## Verzeichnisstruktur und Besitz

```
app/
  main.py            FastAPI-App-Factory `create_app()`, `app = create_app()`; Middleware; mountet ALLE Router
  config.py          `Settings`-Objekt aus Umgebungsvariablen (DATA_DIR, APP_SECRET, BASE_URL, COOKIE_SECURE, ...)
  util.py            uuid7(), now_utc(), to_local(), local_date(), fmt_hm(seconds), fmt_dec(seconds)
  core/              Rechenkern – NUR Standardbibliothek, keine Imports aus app.db/fastapi/sqlalchemy
    types.py         dataclasses/Enums: DayType, AbsenceType, FerienKind, Day, FerienRange, ...
    calendar.py      Feiertage (Gauß), Buß- und Bettag, Ferien-Klassifikation, build_days()
    soll.py          SollParams, teilzeitfaktor, tagessoll, day_soll, week_soll, period_soll (J_soll)
    leave.py         assign_leave(...) – 3 Modi
    account.py       daily rows, saldo, saldo_tage, Prognose, Ferien-Guthaben, W_max, EU-Mittel, Schulwochen>48
    mehrarbeit.py    Monatszähler Art. 87 BayBG (Bagatellgrenze)
    timesplit.py     Split über Mitternacht (lokal Europe/Berlin), nachtraeglich-Regel
    parsing.py       Dauer-/Datums-/Zeitparser für Import ("2,5", "2.5", "2:30", "02:30:00")
    hashing.py       kanonische Serialisierung + SHA-256
    klassenfahrt.py  Variante A/B
  db/
    base.py          DeclarativeBase
    models.py        ALLE ORM-Modelle (siehe unten). Nur der Scaffold-Schritt ändert diese Datei.
    session.py       engine, SessionLocal, get_db() (FastAPI-Dependency), init_db()
    seed.py          Settings-Singleton, Kategorien-Katalog (Kapitel 5), Krank-Pseudokategorie
  services/          Fachlogik mit DB-Zugriff (je Datei ein Besitzer)
  api/               JSON-Router, Präfix /api/...  (je Datei `router = APIRouter()`)
  web/               HTML-Router (Jinja2 + HTMX)    (je Datei `router = APIRouter()`)
    templates/       base.html, partials/, je Screen ein Template
  static/            css/app.css, js/*.js, vendor/ (htmx, alpine, chart.js), manifest.webmanifest, sw.js, icons/
  templates_docs/    Schreiben-Vorlagen (Markdown)
alembic/             Migrationen (env.py nutzt app.db.base.Base.metadata; DB-URL aus app.config)
tests/
  conftest.py        Fixtures: `db` (frische SQLite-Datei in tmp_path + Seed), `client` (TestClient, CSRF_ENFORCE=false)
  core/              T1–T20 + hypothesis
  services/, api/, e2e/
```

Router-Module (vom Scaffold als Stub angelegt, von main.py gemountet; Feature-Schritte füllen sie):
`app/api/{auth,timer,entries,days,calendar,account,timetable,months,export,import_,documents,settings,sensors,backup}.py`
`app/web/{auth,today,week,account,year,entries,calendar,timetable,months,import_,documents,settings}.py`

## Konventionen

- Zeitstempel in DB: UTC, naive `datetime` (SQLite), Spaltentyp `DateTime`. Konvertierung nur über `app.util`.
- Dauer: `int` Sekunden. Anzeige `h:mm` (`fmt_hm`), Berichte zusätzlich dezimal 2 Stellen (`fmt_dec`).
- IDs: `str` UUIDv7 (`app.util.uuid7()`), Spalte `String(36)`.
- Tageszuordnung eines Eintrags: lokales Datum (Europe/Berlin) von `start_at`; zusätzlich materialisiert in
  Spalte `time_entry.day` (Date) für schnelle Abfragen.
- Enums als `String`-Spalten mit Python-`StrEnum` in `app/core/types.py` (Werte = Konzept-Begriffe, klein).
- Flags: JSON-Liste von Strings in `time_entry.flags` (Werte wie im Konzept §4).
- Soft-Delete: `time_entry.deleted_at`; Löschen erzeugt Revision. Nichts wird hart gelöscht (L4),
  Ausnahme: Import-Batch-Rückgängig (setzt ebenfalls deleted_at + Revision).
- `time_entry.status`: `active` | `vorschlag` (Stundenplan-Vorschlag, zählt nicht) | `verworfen`.
- Revisionen: `time_entry_revision` append-only, `snapshot_json` = vollständiger Vorzustand.
- Zeitzone: `zoneinfo.ZoneInfo("Europe/Berlin")` (tzdata ist installiert).
- Templates: Jinja2 über `app.web.templating.templates` (Scaffold legt `app/web/templating.py` an mit Filtern
  `hm`, `dec`, `date_de`, `datetime_de`).
- CSRF: Formular-POSTs tragen `csrf_token` (Hidden-Feld) bzw. HTMX-Header `X-CSRF-Token`
  (base.html setzt `hx-headers`). JSON-API mit Bearer-Token ist CSRF-frei; Session-API-Aufrufe aus JS senden Header.

## ORM-Modelle (app/db/models.py) – Tabellen und Kernspalten

Entspricht Konzept §4 mit diesen technischen Ergänzungen:

- `settings` (id=1): alle Felder aus §4 + `name`, `schule`, `klassen` (JSON-Liste), `faecher` (JSON-Liste),
  `klassenfahrt_variante` ('A'|'B', Default 'B'), `klassenfahrt_cap_h` (10.0), `prognose_schultage` (20),
  `zeitraster` (JSON: Liste {slot_no,start,end,label}), `api_token_hash` (null; `password_hash` entfernt in Migration 0003),
  `ui_default_location_rule` (JSON), `ferienarbeit_schaetzung` (JSON {ferien_id: hours}).
- `period`: id, label, start, end, kind ('schuljahr'|'kalenderjahr').
- `ferien`: id, period_id, name, kind (FerienKind), start, end, source ('manuell'|'ics'|'katalog'), quelle_url.
- `holiday`: date PK, name, scope ('gesetzlich'|'regional'|'unterrichtsfrei'), source.
- `day_override`: date PK, absence_type|null, day_type|null (z. B. 'unterrichtsfrei' für päd. Tag),
  urlaub_manuell bool, note.  ← Nutzer-Eingaben; `calendar_day` wird daraus materialisiert.
- `calendar_day`: date PK, weekday, day_type, absence_type, ferien_id, soll_seconds, note, period_id.
- `category`: id (int), code, name, parent_id, color, sort, is_unterricht, counts_as_mehrarbeit_candidate,
  active, is_pseudo (Krank-Pseudokategorie 'KR': neutralisiert nur Soll, nicht in Statistiken),
  requires_note (S).
- `time_entry`: id, start_at, end_at, duration_seconds, has_bounds, day, category_id, subcategory_id, klasse,
  fach, location, note, flags(JSON), source, status, import_batch_id, timetable_slot_id, created_at,
  updated_at, deleted_at, locked.
- `time_entry_revision`: id, entry_id, revision_no, changed_at, change_reason, snapshot_json.
- `timer_state` (id=1): paused_category_id, paused_payload (JSON) – für Pause/Weiter.
- `timetable`, `timetable_slot`, `day_confirmation`, `mehrarbeit_month`, `month_close`
  (+ `corrected_at`, `hash_history` JSON-Liste alter Hashes, `status` 'abgeschlossen'|'nachtraeglich_korrigiert'),
  `import_batch`, `document`, `backup_log` – wie Konzept §4.
- `category_mapping`: source_text PK, category_code (Merkfunktion Import).

## Rechenkern-Schnittstelle (app/core) – von Services genutzt

```python
# types.py
class DayType(StrEnum): SCHULTAG='schultag'; FERIENTAG='ferientag'; UNTERRICHTSFREI='unterrichtsfrei'; FEIERTAG='feiertag'; WOCHENENDE='wochenende'
class AbsenceType(StrEnum): URLAUB, KRANK, FORTBILDUNG, DIENSTBEFREIUNG, SONDERURLAUB, ELTERNZEIT  (Werte klein)
class FerienKind(StrEnum): HERBST, WEIHNACHTEN, WINTER (=Fasching), OSTERN, PFINGSTEN, SOMMER, SONSTIGE
@dataclass(frozen=True) FerienRange(id, name, kind, start, end)
@dataclass(frozen=True) Day(date, day_type, absence: AbsenceType|None, ferien_id, holiday_name, is_holiday)
@dataclass(frozen=True) SollParams(wochen_soll_voll_h=40.0, upz_voll=28, upz_ist=28)

# calendar.py
easter_sunday(year) -> date
buss_und_bettag(year) -> date
bavarian_holidays(year, regional: frozenset[str]) -> dict[date, tuple[name, scope]]
classify_ferien(name) -> FerienKind
build_days(start, end, ferien, holidays: dict[date, (name, scope)], overrides: dict[date, Override], leave_days: set[date]) -> list[Day]

# soll.py
teilzeitfaktor(p) ; tagessoll_voll_seconds(p) ; day_soll(day, p) -> int ; period_soll(days, p) -> int

# leave.py
leave_candidates(days) -> list[date]
assign_leave(days, ferien, mode, n=30, manual: set[date]=frozenset()) -> set[date]

# account.py
DayLedger(date, soll, ist, day_type, absence, ferien_id)    # ist inkl. Krank-Neutralisierung
build_ledger(days, p, ist_by_day: dict[date,int], krank_neutralisiert, krank_partial: dict[date,int]) -> list[DayLedger]
saldo_series(ledger) -> list[(date, saldo)] ; saldo_at(ledger, t) ; saldo_tage(saldo, p)
holiday_forecast(ledger, today, ferien, planned_work: dict[ferien_id, seconds]) -> list[...]
prognose_ende(ledger, today, n_schultage=20, geplante_ferienarbeit=0) -> int
w_max(ledger, today, ferienarbeit_geplant=0) -> seconds per Schulwoche
eu_mittel(ist_by_day, absences_by_day, today) -> (hours, ampel 'gruen'|'gelb'|'rot')
schulwochen_ueber(ledger, threshold_h=48) -> int

# mehrarbeit.py
month_mehrarbeit(entries: iterable[(day, duration_s, flags)]) -> MehrarbeitMonth(ustd_angeordnet, ustd_genehmigt, total, ausgleichsfaehig)

# timesplit.py
split_at_midnight(start_utc, end_utc) -> list[(start_utc, end_utc)]   # T13
is_nachtraeglich(entry_day, created_at_utc) -> bool                     # > 48 h nach Tagesende

# hashing.py
canonical_entries(entries: iterable[dict]) -> bytes ; entries_hash(entries) -> str  # T20
```

Exakte Signaturen darf der Core-Schritt präzisieren; er dokumentiert sie dann als Docstrings, und Services
richten sich danach.

## Service-Schicht – zentrale Bausteine

- `services/ledger.py` – lädt Settings/Periode/calendar_day/time_entry und liefert `LedgerContext`
  (days, ledger rows, saldo, Kennzahlen). EINZIGE Stelle, an der DB-Daten in den Rechenkern fließen.
- `services/calendar_service.py` – Perioden, Ferien (CRUD, ICS-Import, amtliche Ferien aus dem Katalog:
  `apply_ferien_catalog`, automatisch in `create_period`), Feiertage, Overrides, Urlaubszuordnung,
  `rematerialize(db, period)`.
- `services/ferien_catalog.py` – lädt/prüft `config/ferien_bayern.yaml` (oder `FERIEN_CATALOG_FILE`),
  `blocks_for_period` (an der Periodengrenze zugeschnitten), `coverage()`. Nur Bundesland BY.
- `services/entries.py` – create/update/delete mit Revisionen, Überlappungsprüfung, Mitternachts-Split,
  Lock-Regeln, nachtraeglich-Flag. ALLE Schreibzugriffe auf time_entry laufen hier durch.
- `services/timer.py` – start/stop/switch/pause/resume (nutzt entries.py).

## UI

Mobile first, untere Tab-Leiste: Heute · Woche · Konto · Jahr · Mehr (Einträge, Kalender, Stundenplan,
Monate/Berichte, Import, Schreiben, Einstellungen). Dark-Mode via `prefers-color-scheme`. Kein Build-Step.

## Änderungen nach Abstimmung mit Betrieb (verbindlich, ersetzt Auth-Teile oben)

Siehe `DEPLOY.md`. Kurz:
- **Kein Login.** Passwort-Modus, Setup, Argon2, Login-Rate-Limit, `require_user`-Redirects entfallen. Schutz extern über
  Cloudflare Access, LAN bewusst offen. `AUTH_MODE`/`ADMIN_PASSWORD*` entfallen.
- Pflicht: Host-Header-Prüfung (`ALLOWED_HOSTS`, Default Host aus `BASE_URL` + `localhost`/`127.0.0.1`/`testserver`),
  CSRF für alle schreibenden Requests, kein CORS, CSP inkl. `frame-ancestors 'none'`, HSTS `max-age=31536000`.
- Bearer-Token nur für `/api/sensors` (Pflicht; ohne konfiguriertes Token → 404). Token-Hash in `settings.api_token_hash`.
- Absolute URLs nur aus `BASE_URL`. Keine https-Redirects in der App. Client-IP (nur Logging) = `X-Real-IP`, wenn Peer in
  `PROXY_TRUSTED_NETS`, sonst Peer.
- Container: read-only Root-FS → Schreiben nur nach `/data` und `/tmp`.
- LAN per `http`: Das CSRF-Session-Cookie ist standardmäßig **nicht** `Secure` (`COOKIE_SECURE=false|auto|true`), sonst
  scheitert jeder POST eines http-Clients mit 403. Auf `http://` (außer localhost) zeigt `app.js` einen wegklickbaren
  Hinweis mit Link auf `BASE_URL` (`<meta name="deputat-base-url">`), weil Service Worker/Installation https brauchen.
- Cache-Busting: `static_url(path)` (Jinja-Global, `app/web/assets.py`) hängt den Inhalts-Hash der Datei an
  (`?v=<12 hex>`). `/sw.js` wird über eine Route ausgeliefert, die Asset-Version und Datei-Hashes einsetzt; der
  Cache-Name `deputat-shell-<version>` ändert sich automatisch mit jedem Asset.
- Migration 0003 entfernt `settings.password_hash` (kein Login).
- Migration 0004: `settings.erfassung_ab` (Date, null). Tage davor sind im Ledger neutral
  (`DayLedger.vor_erfassung`, Ist = Soll; `core.account.build_ledger(..., erfassung_ab)`), nicht in Prognose,
  Ferienarbeits-Historie, offenen Tagen und Monats-Offenpunkten; EU-Fenster ab `max(Datenbeginn, erfassung_ab)`.
- Übertrag: `LedgerContext.uebertrag` (Schluss-Saldo der letzten Vorperiode mit Daten), `uebertrag_verrechnet`
  (nur mit `settings.uebertrag_verrechnen`): Saldo, Serie, Prognose, Ferienrechner, Monats-`saldo_end` verschoben;
  Verbraucher nutzen `ctx.saldo_at(d)` statt `core.account.saldo_at(ctx.ledger, d)`.
- Einstellungen und abgeschlossene Monate: `settings_service.update_settings(..., reason=)` vergleicht
  `closed_month_figures()` (Kalender-Signatur + Soll/Ist je Monat) vor/nach der Änderung von `GUARDED_FIELDS`;
  ohne Begründung `CalendarLocked` (423), mit Begründung `nachtraeglich_korrigiert` (`hash_history.kind='einstellungen'`).
- Laufender Timer belegt nur die verstrichene Zeit (`[start, max(now, start+1s))`): spätere Einträge blockieren den
  Start nicht; Stop/Pause/Wechsel kappen am nächsten Eintrag (`entries.next_entry_start`, Hinweis in `notices`).
- Tagesbestätigung im UI: `app/web/days.py` (`/tage`, `/tag/{date}`), ruft `timetable.confirm_day` mit Abweichungen;
  `/heute` und `/tag/{date}` erzeugen fehlende Vorschläge beim ersten Öffnen (Konzept §7.2).
