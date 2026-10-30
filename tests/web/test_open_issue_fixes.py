"""Regression tests for the open-issues round: http LAN hint, redirects, restore threadpool, asset hashes,
navigation, forced-close note, import period/overnight rules, manual leave info, HA docs, Docker files."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import Category, MonthClose, TimeEntry

ROOT = Path(__file__).resolve().parents[2]


def _cat(db, code: str) -> int:
    return db.scalar(select(Category.id).where(Category.code == code))


@pytest.fixture
def web(client, db):
    return client, db


# --------------------------------------------------------------------------- COOKIE_SECURE / http hint


@pytest.mark.parametrize(("raw", "expected"), [(None, "false"), ("auto", "auto"), ("TRUE", "true"), ("0", "false")])
def test_cookie_secure_parsing(settings_env, monkeypatch, raw, expected):
    from app.config import get_config, reset_config

    if raw is not None:
        monkeypatch.setenv("COOKIE_SECURE", raw)
    reset_config()
    assert get_config().cookie_secure == expected


def test_cookie_secure_invalid_value_fails_loudly(settings_env, monkeypatch):
    from app.config import load_config

    monkeypatch.setenv("COOKIE_SECURE", "vielleicht")
    with pytest.raises(ValueError, match="COOKIE_SECURE"):
        load_config()


def test_base_url_meta_and_https_hint_markup(settings_env, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import reset_config
    from app.main import create_app

    monkeypatch.setenv("BASE_URL", "https://time.bauer.center/")
    monkeypatch.setenv("ALLOWED_HOSTS", "time.bauer.center,testserver")
    reset_config()
    with TestClient(create_app()) as client:
        html = client.get("/mehr").text
    assert '<meta name="deputat-base-url" content="https://time.bauer.center">' in html
    banner = re.search(r'<div class="https-banner" data-https-banner[^>]*hidden>(.*?)</div>', html, re.S)
    assert banner, "hint banner must be rendered hidden (app.js shows it on plain http only)"
    assert 'href="https://time.bauer.center"' in banner.group(1)
    assert "Für Offline-Timer und App-Installation bitte" in banner.group(1)
    assert "data-https-banner-close" in banner.group(1)
    js = (ROOT / "app/static/js/app.js").read_text(encoding="utf-8")
    assert 'location.protocol !== "http:"' in js and "isLocalHost" in js


# --------------------------------------------------------------------------- open redirect


@pytest.mark.parametrize("target", ["/\t/evil.example", "//evil.example", "/\\evil.example", "https://evil.example"])
def test_entries_next_rejects_open_redirects(web, target):
    client, db = web
    data = {"datum": "2026-10-12", "modus": "zeit", "von": "13:30", "bis": "15:10", "dauer": "",
            "kategorie": str(_cat(db, "K")), "unterkategorie": "", "klasse": "", "fach": "", "ort": "zuhause",
            "notiz": "", "next": target}
    r = client.post("/eintraege/neu", data=data, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/eintraege")


def test_entries_safe_next_uses_security_helper():
    from app.web import calendar as web_calendar
    from app.web import entries as web_entries

    assert web_entries._safe_next("/\t/evil.example") is None
    assert web_entries._safe_next("/woche?x=1") == "/woche?x=1"
    assert web_calendar._safe_next("/kalender\t/x") == "/kalender"
    assert web_calendar._safe_next("/kalender//evil") == "/kalender"
    assert web_calendar._safe_next("/einstellungen#k") == "/einstellungen#k"
    assert web_calendar._safe_next("/woche") == "/kalender"


# --------------------------------------------------------------------------- restore in threadpool


def test_web_restore_runs_in_threadpool(client, monkeypatch):
    import app.web.months as web_months

    calls = []
    original = web_months.run_in_threadpool

    async def spy(func, *args, **kwargs):
        calls.append(func.__name__)
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(web_months, "run_in_threadpool", spy)
    backup = client.get("/api/export/json").content
    r = client.post("/export/wiederherstellen", files={"datei": ("b.json", backup, "application/json")},
                    data={"verstanden": "1", "bestaetigung": "ERSETZEN"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert calls == ["_restore_sync"]


# --------------------------------------------------------------------------- static asset hashes / service worker


def test_static_urls_carry_content_hashes(client):
    from app.web.assets import asset_hashes, static_url

    html = client.get("/heute").text
    urls = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert urls
    for url in urls:
        if url.startswith("/static/icons/"):
            continue  # icons stay unversioned (the manifest references them without query)
        assert re.search(r"\?v=[0-9a-f]{12}$", url), url
        assert client.get(url).status_code == 200
    assert static_url("css/app.css") == f"/static/css/app.css?v={asset_hashes()['css/app.css']}"
    assert static_url("/static/js/app.js").startswith("/static/js/app.js?v=")
    assert static_url("gibt/es/nicht.js") == "/static/gibt/es/nicht.js"
    assert "app_version }}" not in html and "?v=0.1.0" not in html


def test_service_worker_cache_name_and_precache_follow_asset_hashes(client):
    from app.security import CSP
    from app.web.assets import asset_hashes, asset_version

    r = client.get("/sw.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript")
    assert r.headers["content-security-policy"] == CSP
    sw = r.text
    assert "__ASSET_VERSION__" not in sw
    assert f'const ASSET_VERSION = "{asset_version()}";' in sw
    hashes = json.loads(re.search(r"const ASSET_HASHES = (\{.*?\});", sw).group(1))
    assert hashes["/static/js/timer.js"] == asset_hashes()["js/timer.js"]
    # the pages reference exactly the URLs the worker precaches
    page = client.get("/heute").text
    for rel in ("css/app.css", "js/app.js", "js/timer.js", "js/offline-queue.js", "vendor/htmx.min.js",
                "vendor/alpine.min.js"):
        assert f"/static/{rel}?v={asset_hashes()[rel]}" in page
        assert f'versioned("/static/{rel}")' in sw


def test_asset_version_changes_with_content(tmp_path, monkeypatch):
    from app.web import assets

    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "a.js").write_text("one", encoding="utf-8")
    (tmp_path / "sw.js").write_text('const ASSET_VERSION = "__ASSET_VERSION__";\nconst ASSET_HASHES = {};\n',
                                    encoding="utf-8")
    monkeypatch.setattr(assets, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(assets, "SW_FILE", tmp_path / "sw.js")
    assets.reset_asset_cache()
    try:
        first_url, first_version = assets.static_url("js/a.js"), assets.asset_version()
        assert '"/static/js/a.js"' in assets.service_worker_source()
        (tmp_path / "js" / "a.js").write_text("two", encoding="utf-8")
        assets.reset_asset_cache()
        assert assets.static_url("js/a.js") != first_url
        assert assets.asset_version() != first_version
        assert f'"{assets.asset_version()}"' in assets.service_worker_source()
    finally:
        monkeypatch.undo()
        assets.reset_asset_cache()


# --------------------------------------------------------------------------- navigation


@pytest.mark.parametrize("path", ["/export", "/import", "/schreiben", "/kalender", "/stundenplan", "/einstellungen",
                                  "/eintraege", "/monate", "/mehr"])
def test_more_tab_is_active_on_secondary_screens(client, path):
    html = client.get(path).text
    active = re.findall(r'<a href="([^"]+)" class="bottom-nav-item is-active"', html)
    assert active == ["/mehr"], (path, active)


def test_more_tab_matches_reports():
    from app.web.templating import NAV_ITEMS, is_active

    class _Req:
        def __init__(self, path):
            self.url = type("U", (), {"path": path})()

    more = next(i for i in NAV_ITEMS if i["href"] == "/mehr")
    assert is_active(_Req("/berichte/jahr"), more)
    assert is_active(_Req("/export"), more)
    assert not is_active(_Req("/exporte-gibt-es-nicht"), more)
    heute = next(i for i in NAV_ITEMS if i["href"] == "/heute")
    assert not is_active(_Req("/export"), heute)


# --------------------------------------------------------------------------- forced close note


def test_forced_close_note_on_detail_page_and_live_report(client):
    r = client.post("/api/calendar/periods", json={"label": "2026/27"})
    assert r.status_code == 201
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        k = _cat(s, "VN")
    start = datetime(2026, 9, 15, 8, 0)
    r = client.post("/api/entries", json={"start_at": start.isoformat() + "+02:00",
                                          "end_at": (start + timedelta(hours=1)).isoformat() + "+02:00",
                                          "category_id": k})
    assert r.status_code == 201, r.text
    r = client.post("/monate/2026-09/abschliessen",
                    data={"bestaetigt": "1", "trotzdem": "1", "begruendung": "Wandertag ohne Einträge"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    with SessionLocal() as s:
        mc = s.get(MonthClose, "2026-09")
        assert mc.note == "Wandertag ohne Einträge" and mc.forced_items
    detail = client.get("/monate/2026-09").text
    assert "data-close-note-display" in detail and "Wandertag ohne Einträge" in detail
    assert mc.forced_items[0]["message"] in detail.replace("&#34;", '"').replace("&#39;", "'")
    live = client.get("/monate/2026-09/bericht?aktuell=1").text
    assert "Abschluss trotz offener Punkte" in live and "Wandertag ohne Einträge" in live


# --------------------------------------------------------------------------- import: periods and overnight rows


def _validate(db, content: str, **options):
    from app.services import import_service as svc

    preview = svc.upload(db, content.encode("utf-8"), "zeiten.csv")
    report = svc.validate(db, preview["batch_id"], preview["mapping"], today=date(2026, 10, 29),
                          options=options or None)
    return preview, {r["ref"]: r for r in report["rows"]}, report


def test_import_rows_outside_every_period_are_excluded_by_default(db):
    from app.services import calendar_service as cal
    from app.services import import_service as svc

    cal.create_period(db, "2026/27")
    db.commit()
    content = "Datum;Dauer;Kategorie\n01.10.2026;2;K\n01.07.2026;1;K\n02.07.2026;1;K\n"
    preview, rows, report = _validate(db, content)
    assert rows["2"]["import"] and not rows["2"]["outside_period"]
    for ref in ("3", "4"):
        assert rows[ref]["outside_period"] and not rows[ref]["import"]
        assert any("liegt außerhalb jedes Schuljahres" in w for w in rows[ref]["warnings"])
    assert report["summary"]["ausserhalb"] == 2 and report["summary"]["importieren"] == 1
    assert any("außerhalb jedes Schuljahres" in h for h in report["hinweise"])

    # single row via include_rows, all via the option
    _, rows, _ = _validate(db, content, include_rows=["3"])
    assert rows["3"]["import"] and not rows["4"]["import"]
    preview, rows, report = _validate(db, content, include_outside_periods=True)
    assert all(rows[r]["import"] for r in ("2", "3", "4"))
    result = svc.commit(db, preview["batch_id"], options={"include_outside_periods": True})
    assert result["imported_rows"] == 3


def test_import_bis_before_von_is_an_error_without_next_day_notation(db):
    from app.services import calendar_service as cal

    cal.create_period(db, "2026/27")
    db.commit()
    content = ("Datum;Von;Bis;Kategorie\n"
               "01.10.2026;10:00;09:00;K\n"
               "02.10.2026;22:00;01:30+1;K\n"
               "05.10.2026;22:00;01:30 (+1 Tag);K\n"
               "06.10.2026;13:00;24:00;K\n"
               "07.10.2026;08:00;+1;K\n")
    _, rows, _ = _validate(db, content)
    assert rows["2"]["status"] == "fehler" and "Bis (09:00) liegt vor Von (10:00)" in rows["2"]["errors"][0]
    assert rows["2"]["duration_seconds"] is None
    assert rows["3"]["status"] == "ok" and rows["3"]["duration_seconds"] == 3 * 3600 + 1800
    assert rows["4"]["status"] == "ok" and rows["4"]["duration_seconds"] == 3 * 3600 + 1800
    assert rows["5"]["status"] == "ok" and rows["5"]["duration_seconds"] == 11 * 3600
    assert rows["6"]["status"] == "fehler"


def test_import_xlsx_datetime_bis_on_next_day_is_explicit(db):
    import io

    from openpyxl import Workbook

    from app.services import calendar_service as cal
    from app.services import import_service as svc

    cal.create_period(db, "2026/27")
    db.commit()
    wb = Workbook()
    ws = wb.active
    ws.append(["Datum", "Von", "Bis", "Kategorie"])
    ws.append([datetime(2026, 10, 1), datetime(2026, 10, 1, 22, 0), datetime(2026, 10, 2, 1, 0), "K"])
    ws.append([datetime(2026, 10, 5), datetime(2026, 10, 5, 22, 0), datetime(2026, 10, 5, 1, 0), "K"])
    buf = io.BytesIO()
    wb.save(buf)
    preview = svc.upload(db, buf.getvalue(), "zeiten.xlsx")
    report = svc.validate(db, preview["batch_id"], preview["mapping"], today=date(2026, 10, 29))
    statuses = [r["status"] for r in report["rows"]]
    assert statuses == ["ok", "fehler"]
    assert report["rows"][0]["duration_seconds"] == 3 * 3600


def test_import_wizard_offers_outside_option(client):
    files = {"datei": ("z.csv", "Datum;Dauer;Kategorie\n01.10.2026;2;K\n".encode(), "text/csv")}
    r = client.post("/import", files=files, follow_redirects=False)
    assert r.status_code == 303
    batch_id = r.headers["location"].rsplit("/", 1)[1]
    form = {"map_datum": "0", "map_von": "", "map_bis": "", "map_dauer": "1", "map_kategorie": "2",
            "map_klasse": "", "map_fach": "", "map_notiz": "", "map_ort": "", "kopfzeile": "", "aus_zuordnung": "1"}
    r = client.post(f"/import/{batch_id}/pruefen", data=form)
    assert r.status_code == 200, r.text
    assert 'name="ausserhalb_importieren"' in r.text and "außerhalb jedes Schuljahres" in r.text
    r = client.post(f"/import/{batch_id}/pruefen", data={**form, "optionen": "1", "ausserhalb_importieren": "1"})
    assert "werden trotzdem importiert" in r.text


# --------------------------------------------------------------------------- manual leave mode


def test_manual_leave_unassigned_days_are_info_not_warning(client):
    r = client.post("/api/calendar/periods", json={"label": "2026/27"})
    period = r.json()
    r = client.post(f"/api/calendar/periods/{period['id']}/ics/commit",
                    json={"content": (ROOT / "examples/ferien_bayern_BEISPIEL.ics").read_text(encoding="utf-8")})
    assert r.status_code == 200
    assert client.patch("/api/settings", json={"urlaubs_modus": "manuell"}).status_code == 200
    plan = client.get(f"/api/calendar/periods/{period['id']}/leave-plan").json()
    assert plan["noch_zuzuordnen"] == plan["requested"] > 0
    assert plan["hinweise"] == [] and plan["shortfall"] == 0
    assert plan["info"] == [f"Noch zuzuordnen: {plan['requested']} Urlaubstag(e)."]
    summary = client.get("/api/account/summary").json()
    assert not any("konnten nicht zugeordnet" in h for h in summary["hinweise"])
    html = client.get("/kalender").text
    assert "Noch zuzuordnen" in html and "konnten nicht zugeordnet" not in html


@pytest.mark.usefixtures("no_ferien_catalog")  # builds its own ferien (no catalogue)
def test_manual_leave_real_shortage_is_still_a_warning(db):
    from app.services import calendar_service as cal
    from app.services import settings_service as settings_svc

    period = cal.create_period(db, "2026/27")  # no Ferien: only a handful of candidate days
    settings_svc.update_settings(db, urlaubs_modus="manuell")
    info = cal.leave_plan_info(db, period)
    assert info.shortfall == info.requested - info.candidates > 0
    assert info.hinweise and "können nicht zugeordnet" in info.hinweise[0]


# --------------------------------------------------------------------------- docs and image


def test_deploy_md_home_assistant_fields_exist_in_sensor_response(db):
    from app.api.sensors import sensor_values

    text = (ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    ha = text[text.index("## Home Assistant"):]
    used = set(re.findall(r"value_json\.(\w+)", ha))
    assert {"saldo_h", "heute_ist_h", "timer_running"} <= used
    assert used <= set(sensor_values(db))
    documented = json.loads(re.search(r"```json\n(.*?)\n```", ha, re.S).group(1))
    assert set(documented) == set(sensor_values(db))


def test_docker_image_contains_runtime_files():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for line in ("COPY config ./config", "COPY app ./app", "COPY alembic ./alembic", "COPY alembic.ini ./",
                 "python -m compileall"):
        assert line in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    ignore = [ln.strip() for ln in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()]
    assert "!app/**/*.md" in ignore  # document templates (app/templates_docs/*.md) stay in the image
    assert not any(p in ignore for p in ("config", "config/", "*.yaml", "app", "alembic"))
    assert list((ROOT / "app/templates_docs").glob("*.md")) and (ROOT / "config/benchmarks.yaml").is_file()


def test_no_login_leftovers():
    from app.db.models import Settings

    assert "password_hash" not in Settings.__table__.columns
    hits = []
    for path in (ROOT / "app").rglob("*"):
        if path.suffix in (".py", ".html", ".js") and "vendor" not in path.parts:
            text = path.read_text(encoding="utf-8")
            for word in ("require_user", "require_api_user", "Abmelden", "/logout", "argon2", "AUTH_MODE"):
                if word in text:
                    hits.append(f"{path.relative_to(ROOT)}: {word}")
    assert hits == []
