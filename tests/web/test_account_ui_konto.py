"""Web tests for the screen 'Konto' (app/web/account.py): page, chart JSON, tiles, Ferien-Guthaben-Rechner,
display switches, leave-mode comparison, Geltendmachung marker, empty state."""

from __future__ import annotations

import re
from datetime import datetime, time as dtime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Category, Document, Settings, TimeEntry
from app.db.session import SessionLocal
from app.util import local_to_utc, today_local


def add_entry(code: str, day, start: dtime, minutes: int, **extra) -> str:
    start_at = local_to_utc(day, start)
    with SessionLocal() as s:
        entry = TimeEntry(
            start_at=start_at,
            end_at=start_at + timedelta(minutes=minutes),
            duration_seconds=minutes * 60,
            has_bounds=True,
            day=day,
            category_id=s.scalar(select(Category.id).where(Category.code == code)),
            status="active",
            source="manuell",
            flags=extra.pop("flags", []),
            **extra,
        )
        s.add(entry)
        s.commit()
        return entry.id


def setup_period(*, future_ferien: bool = True):
    """Current period + one future ferien block (if it fits) + a few days of work."""
    from app.services import calendar_service as cal

    today = today_local()
    with SessionLocal() as s:
        period = cal.create_period(s)
        s.commit()
        ferien_id = None
        start, end = today + timedelta(days=10), today + timedelta(days=14)
        if future_ferien and end <= period.end:
            ferien_id = cal.create_ferien(s, period, name="Testferien", start=start, end=end).id
            s.commit()
        period_id, p_start, p_end = period.id, period.start, period.end
    for i in range(5):
        d = today - timedelta(days=i)
        if d >= p_start:
            add_entry("U", d, dtime(8, 0), 300)
    return {"period_id": period_id, "start": p_start, "end": p_end, "ferien_id": ferien_id, "today": today}


# --------------------------------------------------------------------------- empty state


def test_no_period_states(client):
    r = client.get("/konto")
    assert r.status_code == 200
    assert "Noch kein Schuljahr angelegt" in r.text
    assert 'href="/kalender"' in r.text

    r = client.get("/konto/daten.json")
    assert r.status_code == 404
    assert r.json()["state"] == "no_period"

    r = client.get("/konto/teil/kacheln")
    assert r.status_code == 200
    assert "Noch kein Schuljahr angelegt" in r.text

    r = client.get("/konto/teil/urlaubsvergleich")
    assert r.status_code == 200
    assert "Noch kein Schuljahr angelegt" in r.text


# --------------------------------------------------------------------------- page


def test_page_renders_chart_tiles_and_sections(client):
    setup_period()
    r = client.get("/konto")
    assert r.status_code == 200
    html = r.text
    assert "Saldo über die Periode" in html
    assert 'data-saldo-chart data-src="/konto/daten.json"' in html
    assert "Referenz 40h-Job" in html
    assert "/static/js/charts.js" in html and "/static/vendor/chart.umd.min.js" in html
    assert "/static/css/account.css" in html
    for label in ("Saldo heute", "Prognose Periodenende", "J<sub>soll</sub>", "Ist bisher", "Ferienarbeit bisher",
                  "W<sub>max</sub>", "EU-12-Monats-Mittel", "Schulwochen &gt; 48 h"):
        assert label in html, label
    assert "Arbeitstage" in html  # saldo in days
    assert re.search(r'class="ampel ampel-(gruen|gelb|rot)"', html)
    assert "Ferien-Guthaben-Rechner" in html
    assert "maßgeblich" in html
    assert 'hx-get="/konto/teil/urlaubsvergleich"' in html
    # no inline scripts (strict CSP)
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)


def test_chart_json_shape(client):
    info = setup_period()
    r = client.get("/konto/daten.json")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    data = r.json()
    n_days = (info["end"] - info["start"]).days + 1
    assert data["n_days"] == n_days
    assert data["x0"] == info["start"].isoformat()
    assert data["labels"]["zero"] == "Referenz 40h-Job"
    assert data["saldo"], "saldo series up to today"
    assert all(0 <= p[0] < n_days for p in data["saldo"])
    assert data["saldo"][-1][0] == (info["today"] - info["start"]).days
    if info["today"] < info["end"]:
        assert data["prognose"][0][0] == data["saldo"][-1][0]
        assert data["prognose"][-1][0] == n_days - 1
    if info["ferien_id"]:
        names = [f["name"] for f in data["ferien"]]
        assert "Testferien" in names
    assert isinstance(data["urlaub"], list)
    assert all("name" in f and "i" in f for f in data["feiertage"])
    assert data["months"] and data["months"][0]["i"] == 0
    assert data["geltendmachung"] is None


def test_tiles_partial(client):
    setup_period()
    r = client.get("/konto/teil/kacheln")
    assert r.status_code == 200
    assert 'data-tile="saldo"' in r.text
    assert 'data-tile="wmax"' in r.text
    assert 'data-tile="eu"' in r.text


# --------------------------------------------------------------------------- Ferien-Guthaben-Rechner


def _ferien_or_skip(info):
    if not info["ferien_id"]:
        pytest.skip("no room for future ferien before the period end")
    return info["ferien_id"]


def test_rechner_lists_upcoming_ferien(client):
    fid = _ferien_or_skip(setup_period())
    r = client.get("/konto/teil/ferienrechner")
    assert r.status_code == 200
    assert "Testferien" in r.text
    assert f'name="plan_{fid}"' in r.text
    assert "Abbau-Kapazität" in r.text
    assert "Du gehst voraussichtlich mit" in r.text


def test_rechner_save_htmx_persists_and_triggers(client):
    fid = _ferien_or_skip(setup_period())
    r = client.post("/konto/ferienrechner", data={f"plan_{fid}": "12,5"}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert r.headers.get("HX-Trigger") == "deputat-konto-changed"
    assert 'value="12,5"' in r.text
    assert "gespeichert" in r.text
    with SessionLocal() as s:
        assert s.get(Settings, 1).ferienarbeit_schaetzung[fid] == pytest.approx(12.5)

    # empty value removes the estimate again (back to the default)
    r = client.post("/konto/ferienrechner", data={f"plan_{fid}": ""}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    with SessionLocal() as s:
        assert fid not in (s.get(Settings, 1).ferienarbeit_schaetzung or {})


def test_rechner_save_changes_saldo_nach(client):
    fid = _ferien_or_skip(setup_period())
    before = client.get("/konto/teil/ferienrechner").text
    client.post("/konto/ferienrechner", data={f"plan_{fid}": "30"}, headers={"HX-Request": "true"})
    after = client.get("/konto/teil/ferienrechner").text

    def saldo_nach(html):
        m = re.search(rf'data-ferien-row="{fid}".*?data-saldo-nach>([^<]+)<', html, re.S)
        assert m
        return m.group(1).strip()

    assert saldo_nach(before) != saldo_nach(after)


def test_rechner_invalid_value(client):
    fid = _ferien_or_skip(setup_period())
    r = client.post("/konto/ferienrechner", data={f"plan_{fid}": "viel"}, headers={"HX-Request": "true"})
    # 200, not 422: htmx 2 does not swap 4xx responses, so the message would never be shown.
    assert r.status_code == 200
    assert "keine gültige Stundenzahl" in r.text
    assert 'value="viel"' in r.text and 'aria-invalid="true"' in r.text
    assert "gespeichert" not in r.text
    assert "HX-Trigger" not in r.headers
    with SessionLocal() as s:
        assert fid not in (s.get(Settings, 1).ferienarbeit_schaetzung or {})


def test_rechner_plain_form_redirects(client):
    fid = _ferien_or_skip(setup_period())
    r = client.post("/konto/ferienrechner", data={f"plan_{fid}": "8", "urlaub": "proportional"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/konto?urlaub=proportional#ferienrechner"


def test_rechner_ignores_unknown_ferien(client):
    setup_period()
    r = client.post("/konto/ferienrechner", data={"plan_nope": "5"}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    with SessionLocal() as s:
        assert "nope" not in (s.get(Settings, 1).ferienarbeit_schaetzung or {})


# --------------------------------------------------------------------------- switches


def test_display_switches_do_not_change_settings(client):
    setup_period()
    with SessionLocal() as s:
        mode, variante = s.get(Settings, 1).urlaubs_modus, s.get(Settings, 1).klassenfahrt_variante
    other = "proportional" if mode != "proportional" else "sommerferien_zuerst"
    r = client.get(f"/konto?urlaub={other}&klassenfahrt=A")
    assert r.status_code == 200
    assert "Vergleichsansicht" in r.text
    assert f'data-src="/konto/daten.json?urlaub={other}&amp;klassenfahrt=A"' in r.text
    data = client.get(f"/konto/daten.json?urlaub={other}&klassenfahrt=A").json()
    assert data["leave_mode"] == other
    assert data["klassenfahrt_variante"] == "A"
    with SessionLocal() as s:
        assert s.get(Settings, 1).urlaubs_modus == mode
        assert s.get(Settings, 1).klassenfahrt_variante == variante

    # invalid switch values are ignored
    r = client.get("/konto?urlaub=quatsch&klassenfahrt=Z")
    assert r.status_code == 200
    assert "Vergleichsansicht" not in r.text


def test_leave_mode_comparison_partial(client):
    setup_period()
    r = client.get("/konto/teil/urlaubsvergleich")
    assert r.status_code == 200
    html = r.text
    assert "Konto bei Urlaub = Sommerferien" in html
    assert "Konto bei proportionaler Verteilung" in html
    assert html.count("maßgeblich") >= 1
    assert "Prognose Periodenende" in html


# --------------------------------------------------------------------------- Geltendmachung


def test_geltendmachung_marker(client):
    info = setup_period()
    sent = info["start"] + timedelta(days=3)
    if sent > info["today"]:
        pytest.skip("period just started")
    with SessionLocal() as s:
        s.add(Document(type="geltendmachung", sent_at=datetime.combine(sent, dtime(10, 0)), rendered_md=""))
        s.add(Document(type="ueberlastungsanzeige", sent_at=datetime.combine(sent, dtime(9, 0)), rendered_md=""))
        s.commit()
    wirksam = (sent.replace(day=1) + timedelta(days=32)).replace(day=1)
    html = client.get("/konto").text
    assert f"Geltendmachung wirksam ab {wirksam:%d.%m.%Y}" in html
    data = client.get("/konto/daten.json").json()
    if wirksam <= info["end"]:
        assert data["geltendmachung"]["i"] == (wirksam - info["start"]).days
        assert "Geltendmachung wirksam ab" in data["geltendmachung"]["label"]


def test_unsent_geltendmachung_has_no_marker(client):
    setup_period()
    with SessionLocal() as s:
        s.add(Document(type="geltendmachung", sent_at=None, rendered_md=""))
        s.commit()
    assert "Geltendmachung wirksam ab" not in client.get("/konto").text


def test_parse_hours_rejects_nan_and_inf():
    import pytest

    from app.web.account import _parse_hours

    assert _parse_hours("12,5") == 12.5 and _parse_hours("1:30") == 1.5 and _parse_hours("") is None
    for bad in ("nan", "inf", "-1", "1001", "abc"):
        with pytest.raises(ValueError):
            _parse_hours(bad)
