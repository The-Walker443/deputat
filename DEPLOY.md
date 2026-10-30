# Deputat – Betrieb auf dem Homeserver

Zielbild: ein Container `deputat` als Portainer-Stack auf der Docker-VM, erreichbar unter
**https://time.bauer.center**. Sicherheits- und Betriebsentscheidungen stehen unten.

```
extern:  Browser ─https─▶ Cloudflare (Access + Geo-Block DACH+IT) ─▶ Tunnel ─▶ cloudflared ─http─▶ NPM ─http─▶ deputat:8000
LAN:     Browser ─http/https─▶ NPM (Pi-hole-Wildcard) ─http─▶ deputat:8000
```

## Image

- `ghcr.io/the-walker443/deputat` (öffentlich, kein Registry-Login nötig), nur `linux/amd64`.
- Gebaut von GitHub Actions (`.github/workflows/image.yml`) bei jedem Push auf `main`, nach grünen Tests.
- Tags: `latest` und unveränderlich `sha-<kurz>`. Für Rollouts und Rollback einen `sha-…`-Tag pinnen.

## Stack-Variablen (Portainer)

| Variable | Pflicht | Beispiel / Default | Zweck |
|---|---|---|---|
| `IMAGE_REPOSITORY` | nein | `ghcr.io/the-walker443/deputat` | Image |
| `IMAGE_TAG` | nein | `latest` bzw. `sha-1a2b3c4` | Version pinnen |
| `APP_SECRET` | **ja** | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` | Signatur des CSRF-Cookies. Ohne Wert wird einer erzeugt und in `/data/.app_secret` gespeichert. |
| `BASE_URL` | ja | `https://time.bauer.center` | Absolute URLs; bei `https://` zeigt die App Aufrufen über reines `http` im LAN einen Hinweis mit Link |
| `ALLOWED_HOSTS` | ja | `time.bauer.center` | Host-Header-Prüfung (DNS-Rebinding). `localhost`/`127.0.0.1` sind immer erlaubt (Healthcheck). |
| `COOKIE_SECURE` | nein | `false` (Default) \| `auto` \| `true` | `Secure`-Flag des CSRF-Cookies. `auto` = `Secure`, wenn `BASE_URL` https ist. Solange das LAN auch per `http` zugreift, **`false` lassen** – sonst bekommt ein http-Client das Cookie nie und jedes Formular scheitert mit 403. Das Cookie enthält nur CSRF-Token und Hinweistexte (kein Login). |
| `TZ` | nein | `Europe/Berlin` | Zeitzone |
| `BACKUP_CRON` | nein | `30 0 * * *` | Nächtliches Backup, **vor** dem PBS-Lauf um 01:00 |
| `BACKUP_RETENTION_DAYS` | nein | `14` | Aufbewahrung der In-Container-Backups |
| `PROXY_TRUSTED_NETS` | nein | `172.16.0.0/12` | Peers, deren `X-Real-IP` im Log als Client-IP erscheint (nur Logging) |
| `LOG_LEVEL` | nein | `info` | Log-Level |
| `FERIEN_CATALOG_FILE` | nein | `/data/ferien_bayern.yaml` | Eigener/aktualisierter Ferienkatalog (gleiches Format wie `config/ferien_bayern.yaml` im Image). Leer = mitgelieferter Katalog. Ungültige Datei: Die App startet trotzdem, übernimmt aber keine Ferien (Fehler im Log). |

Es gibt **keine** Login-Variablen: Die App hat bewusst keinen Login.

## Sicherheitsmodell (bewusste Entscheidung des Betreibers)

- **Extern** schützt ausschließlich **Cloudflare Access** (Self-hosted Application `time.bauer.center`, Policy nur für den Betreiber).
- **LAN** ist offen: Jedes Gerät im Heimnetz (inkl. IoT) kann die App öffnen, Daten ändern und **Exporte/Backups
  herunterladen**. Das FritzBox-Gäste-WLAN ist getrennt.
- Die App vertraut keinen Proxy-Headern (`CF-*`, `X-Real-IP`, `X-Forwarded-*`); sie dienen nur dem Logging.
- Schutz gegen Angriffe über den Browser von Heimnetz-Geräten: Host-Header-Prüfung, CSRF-Token für alle schreibenden
  Requests, kein CORS, strikte CSP inkl. `frame-ancestors 'none'`, HSTS (`max-age=31536000`).
- `/api/sensors` (Home Assistant) verlangt ein Bearer-Token. Ohne erzeugtes Token antwortet der Endpunkt mit 404.
- Die App leitet **nie** selbst auf https um (NPM/Tunnel-Kette, sonst Redirect-Schleife). Wird sie im LAN über
  `http://` geöffnet, funktioniert alles außer Service Worker (Offline-Timer) und App-Installation; ein
  wegklickbarer Hinweis verlinkt dann auf `BASE_URL` (https). Dafür muss `COOKIE_SECURE` auf `false` stehen (Default).

## Erst-Inbetriebnahme – Reihenfolge ist wichtig

1. **Cloudflare Access** (Betreiber, CF-Dashboard): Self-hosted Application für `time.bauer.center` anlegen,
   Policy nur für den Betreiber, Session Duration lang (z. B. 1 Monat, wegen PWA/Offline-Sync). Testen.
2. **Stack anlegen** (claude-ops): Compose aus `docker-compose.yml`, Variablen setzen, deployen.
   Prüfen: `docker inspect --format '{{.State.Health.Status}}' deputat` → `healthy`.
3. **NPM-Host** (claude-ops): `time.bauer.center` → `http://deputat:8000`, Wildcard-Zertifikat, kein „Force SSL“.
4. **Uptime Kuma** (claude-ops): HTTP-Monitor `https://time.bauer.center/healthz` (200 nur bei DB ok, ohne Auth).
5. **Tunnel-Route** (Betreiber, CF-Dashboard) erst **jetzt** aktivieren. claude-ops prüft von außen, dass ohne
   Access-Login nur die Cloudflare-Loginseite erscheint.
6. **In der App einrichten** (Nutzerin, Browser):
   1. *Kalender*: Schuljahr anlegen (z. B. 2026/27) – die amtlichen Ferien übernimmt die App automatisch aus dem
      mitgelieferten Katalog (Schuljahre 2024/25 bis 2029/30); nur für andere Schuljahre per ICS importieren oder
      manuell erfassen. Ist ein Schuljahr schon ohne Ferien oder mit den Beispiel-Ferien
      (`examples/ferien_bayern_BEISPIEL.ics`) angelegt, bietet der Kalender „Amtliche Ferien übernehmen“ an.
      Regionale Feiertage prüfen (Mariä Himmelfahrt), Urlaubszuordnung kontrollieren.
   2. *Einstellungen*: Name, Schule, UPZ, Geburtsdatum (Altersermäßigung), Klassen, Fächer, Zeitraster.
   3. *Stundenplan*: Version mit Gültigkeit anlegen.
   4. Optional *Import* bestehender Aufzeichnungen (CSV/XLSX).
   5. Optional *Einstellungen → Sensor-API-Token* erzeugen (wird nur einmal angezeigt) und in Home Assistant hinterlegen.
7. Auf dem Handy `https://time.bauer.center` öffnen und „Zum Startbildschirm hinzufügen“ (PWA, Offline-Timer).

## Daten, Backup, Wiederherstellung

- Alles liegt im Named Volume `deputat_data` (`/data`):
  `deputat.sqlite` (WAL), `backups/YYYY-MM-DD.sqlite.gz`, `reports/YYYY-MM.html`, `exports/`, `imports/<batch>/`, `.app_secret`.
- Nächtliches Backup um 00:30 per SQLite-Backup-API (konsistenter Snapshot, Integritätsprüfung, gzip), danach sichert
  PBS um 01:00 die ganze VM inkl. Volume (crash-konsistent; maßgeblich sind die Dateien in `backups/`).
- Manuell: in der App *Export & Backup → Backup jetzt*, oder
  `docker exec deputat python -c "from app.services.backup import create_backup; print(create_backup())"`.
- **Wiederherstellung** aus einem Backup:
  1. Stack stoppen.
  2. Backup entpacken und als `/data/deputat.sqlite` ins Volume legen, `deputat.sqlite-wal`/`-shm` entfernen
     (z. B. über einen Hilfscontainer mit gemountetem `deputat_data`).
  3. Stack starten; Migrationen laufen automatisch.
- Alternativ: JSON-Vollbackup (*Export & Backup*) in eine leere Instanz zurückspielen.

## Updates und Rollback

- Kein Watchtower, kein Auto-Deploy. claude-ops zieht nach Freigabe ein neues Image (`IMAGE_TAG=sha-…`) und erstellt den
  Container neu. Datenbank-Migrationen laufen beim Start automatisch.
- Rollback: vorherigen `sha-…`-Tag setzen. Achtung: Hat ein Update die DB migriert, vorher das Backup des Vortags
  bzw. ein manuelles Backup bereithalten.
- Monatsabschlüsse und Revisionen bleiben bei Updates unverändert. Berichte tragen App-Version und Rechenregeln-Version.

## Healthcheck und Monitoring

- `GET /healthz` → `200 {"status":"ok"}` nur wenn eine DB-Abfrage klappt, sonst `503`. Ohne Auth, schnell.
- Container-Healthcheck: alle 60 s, `python` + `urllib` auf `http://127.0.0.1:8000/healthz`.

## Home Assistant

```yaml
rest:
  - resource: https://time.bauer.center/api/sensors
    scan_interval: 300
    headers:
      Authorization: !secret deputat_token   # "Bearer <token>"
    sensor:
      - name: Deputat Saldo
        value_template: "{{ value_json.saldo_h }}"
        unit_of_measurement: h
      - name: Deputat heute
        value_template: "{{ value_json.heute_ist_h }}"
        unit_of_measurement: h
      - name: Deputat Woche
        value_template: "{{ value_json.woche_ist_h }}"
        unit_of_measurement: h
      - name: Deputat EU-Mittel
        value_template: "{{ value_json.eu_mittel_h }}"
        unit_of_measurement: h
      - name: Deputat EU-Ampel
        value_template: "{{ value_json.eu_ampel }}"
      - name: Deputat Saldo-Prognose Periodenende
        value_template: "{{ value_json.prognose_ende_h }}"
        unit_of_measurement: h
      - name: Deputat Timer-Kategorie
        value_template: "{{ value_json.timer_category or 'kein Timer' }}"
    binary_sensor:
      - name: Deputat Timer läuft
        value_template: "{{ value_json.timer_running }}"
```

Antwort von `GET /api/sensors` (Stunden als Dezimalzahl mit 2 Stellen; `null`, solange kein Schuljahr angelegt ist –
dann fallen `heute_ist_h`/`woche_ist_h` auf die erfasste Zeit zurück):

```json
{"saldo_h": 83.25, "heute_ist_h": 6.5, "woche_ist_h": 31.75, "eu_mittel_h": 44.1, "eu_ampel": "gelb",
 "timer_running": true, "timer_category": "Unterricht", "prognose_ende_h": 12.0,
 "updated_at": "2026-10-30T09:15:00Z"}
```

`eu_ampel` ist `gruen` | `gelb` | `rot`. HA ruft intern über NPM auf (nicht über den Tunnel), daher kein
Cloudflare-Access-Token nötig.

## Hinweise

- Externer Zugriff unterliegt dem Cloudflare-Geo-Block (nur DACH + IT) – auf Reisen beachten.
- Läuft die Cloudflare-Access-Sitzung ab, zeigt die App beim Synchronisieren „Sitzung abgelaufen – bitte Seite neu
  laden / neu anmelden“; offline erfasste Timer-Aktionen bleiben dabei erhalten.
- Root-Dateisystem ist read-only; die App schreibt nur nach `/data` und `/tmp`.
- Ferienkatalog aktualisieren (neue Ferienordnung): entweder ein neues Image mit geänderter
  `config/ferien_bayern.yaml` oder die Datei nach `/data/ferien_bayern.yaml` legen und
  `FERIEN_CATALOG_FILE=/data/ferien_bayern.yaml` setzen. Neue Schuljahre bekommen die Termine beim Anlegen;
  bestehende über *Kalender → Ferien → Durch amtliche Termine ersetzen*.
