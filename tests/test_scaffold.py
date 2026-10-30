"""Scaffold tests: app boots, DB migrates/seeds, pages render, util helpers.

Security (CSRF, Host check, headers, sensor token, client IP) lives in tests/test_security_*.py.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC

from sqlalchemy import func, select

from app.db.models import Category, Settings, TimerState
from app.db.seed import MAIN_CATEGORY_CODES, seed
from app.util import fmt_dec, fmt_hm, local_day_bounds_utc, local_midnight_utc, uuid7, uuid7_time

# --------------------------------------------------------------------------- health & pages


def test_healthz_ok(client):
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_root_redirects_to_today_and_today_renders(client):
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/heute"

    res = client.get("/heute")
    assert res.status_code == 200
    assert "Heute" in res.text
    assert 'hx-headers=' in res.text
    assert "/static/vendor/htmx.min.js" in res.text


def test_more_page_links_secondary_screens(client):
    res = client.get("/mehr")
    assert res.status_code == 200
    for href in ("/eintraege", "/kalender", "/stundenplan", "/monate", "/import", "/schreiben", "/einstellungen"):
        assert f'href="{href}"' in res.text


def test_pwa_files_served_from_root(client):
    sw = client.get("/sw.js")
    assert sw.status_code == 200
    assert sw.headers["service-worker-allowed"] == "/"
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.json()["start_url"] == "/heute"
    assert client.get("/static/css/app.css").status_code == 200


def test_html_404_page(client):
    res = client.get("/gibt-es-nicht", headers={"Accept": "text/html"})
    assert res.status_code == 404
    assert "Seite nicht gefunden" in res.text


# --------------------------------------------------------------------------- database & seed


def test_seed_is_idempotent(db):
    count_before = db.scalar(select(func.count()).select_from(Category))
    seed(db)
    seed(db)
    assert db.scalar(select(func.count()).select_from(Category)) == count_before
    assert db.scalar(select(func.count()).select_from(Settings)) == 1
    assert db.scalar(select(func.count()).select_from(TimerState)) == 1


def test_categories_present(db):
    mains = db.scalars(select(Category).where(Category.parent_id.is_(None))).all()
    codes = {c.code for c in mains}
    assert set(MAIN_CATEGORY_CODES) <= codes
    assert len(MAIN_CATEGORY_CODES) == 13
    assert codes == set(MAIN_CATEGORY_CODES) | {"KR"}

    by_code = {c.code: c for c in mains}
    assert by_code["U"].is_unterricht and by_code["U"].counts_as_mehrarbeit_candidate
    assert by_code["S"].requires_note
    assert by_code["KR"].is_pseudo and by_code["KR"].name == "Krank (Teil)"
    assert len({c.color.lower() for c in mains}) == len(mains), "category colors must be distinct"

    u_children = {c.name for c in by_code["U"].children}
    assert {"Regelunterricht", "Förderunterricht", "Differenzierung", "Vertretung (gehalten)"} <= u_children


def test_settings_defaults(db):
    settings = db.get(Settings, 1)
    assert settings.upz_voll == 28
    assert settings.wochen_soll_voll_h == 40.0
    assert settings.urlaubstage == 30
    assert settings.urlaubs_modus == "sommerferien_zuerst"
    assert settings.krank_neutralisiert is True
    assert settings.klassen == ["1a", "1b", "2a", "2b", "3a", "3b", "4a", "4b"]
    assert "Mathematik" in settings.faecher
    assert settings.klassenfahrt_variante == "B"
    assert settings.name == "" and settings.schule == ""


def test_sqlite_pragmas(db):
    assert db.connection().exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
    assert db.connection().exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


# --------------------------------------------------------------------------- util


def test_uuid7_version_variant_and_monotonic():
    ids = [uuid7() for _ in range(5000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    for value in ids[:50]:
        parsed = uuid.UUID(value)
        assert parsed.version == 7
        assert parsed.variant == uuid.RFC_4122
    embedded = uuid7_time(ids[-1]).replace(tzinfo=UTC).timestamp()
    assert abs(embedded - time.time()) < 5


def test_fmt_hm():
    assert fmt_hm(0) == "0:00"
    assert fmt_hm(59) == "0:01"  # rounds to nearest minute
    assert fmt_hm(3600) == "1:00"
    assert fmt_hm(3 * 3600 + 5 * 60) == "3:05"
    assert fmt_hm(-(3 * 3600 + 5 * 60)) == "-3:05"
    assert fmt_hm(-20) == "0:00"
    assert fmt_hm(100 * 3600) == "100:00"
    assert fmt_hm(5400, plus=True) == "+1:30"
    assert fmt_hm(None) == ""


def test_fmt_dec():
    assert fmt_dec(9000) == "2,50"
    assert fmt_dec(-11100) == "-3,08"
    assert fmt_dec(9000, sep=".") == "2.50"
    assert fmt_dec(1) == "0,00"
    assert fmt_dec(-1) == "0,00"


def test_local_midnight_dst():
    from datetime import date, datetime

    # Summer (CEST, UTC+2) and winter (CET, UTC+1)
    assert local_midnight_utc(date(2026, 7, 1)) == datetime(2026, 6, 30, 22, 0)
    assert local_midnight_utc(date(2026, 12, 1)) == datetime(2026, 11, 30, 23, 0)
    start, end = local_day_bounds_utc(date(2026, 10, 25))  # DST ends: 25 h day
    assert (end - start).total_seconds() == 25 * 3600
