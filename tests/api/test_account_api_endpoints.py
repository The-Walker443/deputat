"""/api/account/*: summary, series, holiday forecast (planned work), leave-mode comparison, statistics."""

from __future__ import annotations

from datetime import date, time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

EXAMPLE_ICS = (Path(__file__).resolve().parents[2] / "examples" / "ferien_bayern_BEISPIEL.ics").read_text("utf-8")
H = 3600
Q = {"stichtag": "2026-10-29"}


@pytest.fixture
def year(client) -> dict:
    period = client.post("/api/calendar/periods", json={"label": "2026/27"}).json()
    client.post(f"/api/calendar/periods/{period['id']}/ics/commit", json={"content": EXAMPLE_ICS})
    return period


@pytest.fixture
def worked(year, settings_env) -> dict:
    """10 h on every school day 15.09.–28.10.2026 (rows inserted directly as test data)."""
    from app.db.models import CalendarDay, Category, TimeEntry
    from app.db.session import SessionLocal
    from app.util import local_to_utc

    with SessionLocal() as db:
        vn = db.scalars(select(Category).where(Category.code == "VN")).one()
        u = db.scalars(select(Category).where(Category.code == "U")).one()
        for row in db.scalars(
            select(CalendarDay).where(
                CalendarDay.date.between(date(2026, 9, 15), date(2026, 10, 28)), CalendarDay.day_type == "schultag"
            )
        ):
            for start, hours, cat in ((time(8), 5, u), (time(14), 5, vn)):
                begin = local_to_utc(row.date, start)
                db.add(TimeEntry(start_at=begin, end_at=begin + timedelta(hours=hours), duration_seconds=hours * H,
                                 day=row.date, category_id=cat.id, flags=[]))
        db.commit()
    return year


def test_account_without_period_returns_hint(client):
    for path in ("/api/account/summary", "/api/account/series", "/api/account/holiday-forecast",
                 "/api/account/compare-leave-modes", "/api/account/statistics"):
        res = client.get(path)
        assert res.status_code == 404, path
        assert res.json()["detail"] == "Bitte zuerst ein Schuljahr anlegen."


def test_summary(client, worked):
    res = client.get("/api/account/summary", params=Q)
    assert res.status_code == 200, res.text
    s = res.json()
    assert s["period"]["label"] == "2026/27" and s["stichtag"] == "2026-10-29"
    assert s["j_soll"] == 222 * 8 * H and s["j_soll_hm"] == "1776:00"
    assert s["leave_mode"] == "sommerferien_zuerst" and s["klassenfahrt_variante"] == "B"
    assert s["ist_bisher"] == 32 * 10 * H  # 32 school days between 15.09. and 28.10.
    assert s["saldo_heute_hm"].startswith(("+", "-"))
    assert s["saldo_tage"] == pytest.approx(s["saldo_heute"] / (8 * H), abs=0.01)
    assert s["heute"]["day_type"] == "schultag" and s["heute"]["soll"] == 8 * H
    assert s["woche"]["iso_week"] == 44
    assert s["eu_mittel"]["ampel"] in ("gruen", "gelb", "rot")
    assert s["naechste_ferien"]["name"] == "BEISPIEL – Herbstferien 2026"
    assert s["naechste_ferien"]["abbau_kapazitaet"] == 40 * H
    assert s["urlaub"]["assigned"] == 30
    assert s["w_max"] > 0 and s["w_max_hm"]
    assert s["hinweise"] == []

    by_label = client.get("/api/account/summary", params={**Q, "period": "2026/27"}).json()
    assert by_label["saldo_heute"] == s["saldo_heute"]
    prop = client.get("/api/account/summary", params={**Q, "leave_mode": "proportional"}).json()
    assert prop["leave_mode"] == "proportional" and prop["saldo_heute"] < s["saldo_heute"]


@pytest.mark.parametrize(
    "params",
    [{"leave_mode": "nie"}, {"klassenfahrt_variante": "C"}, {"stichtag": "29.10.2026"}],
)
def test_summary_validation(client, year, params):
    assert client.get("/api/account/summary", params=params).status_code == 422


def test_summary_unknown_period(client, year):
    res = client.get("/api/account/summary", params={"period": "1999/00"})
    assert res.status_code == 404 and "nicht gefunden" in res.json()["detail"]


def test_series(client, worked):
    body = client.get("/api/account/series", params=Q).json()
    points = body["points"]
    assert points[0]["date"] == "2026-08-01" and points[-1]["date"] == "2026-10-29"
    assert points[-1]["saldo"] == client.get("/api/account/summary", params=Q).json()["saldo_heute"]
    assert body["prognose"][0] == {"date": "2026-10-29", "saldo": points[-1]["saldo"]}
    assert body["prognose"][-1]["date"] == "2027-07-31"
    assert len(body["ferien"]) == 6 and len(body["urlaub"]) == 30
    assert any(f["name"] == "Buß- und Bettag" for f in body["feiertage"])


def test_holiday_forecast_with_planned_work(client, worked):
    body = client.get("/api/account/holiday-forecast", params=Q).json()
    herbst = body["items"][0]
    assert herbst["name"] == "BEISPIEL – Herbstferien 2026" and herbst["laufend"] is False
    assert herbst["abbau_kapazitaet"] == 40 * H and herbst["erwartete_ferienarbeit"] == 0
    assert herbst["geplant_h"] is None
    assert herbst["saldo_nach"] == herbst["saldo_vor"] - 40 * H
    assert "Herbstferien" in herbst["text"]

    res = client.put("/api/account/holiday-forecast", params=Q, json={"planned": {herbst["ferien_id"]: 12}})
    assert res.status_code == 200
    herbst2 = res.json()["items"][0]
    assert herbst2["geplant_h"] == 12 and herbst2["erwartete_ferienarbeit"] == 12 * H
    assert herbst2["saldo_nach"] == herbst["saldo_nach"] + 12 * H
    assert client.get("/api/settings").json()["ferienarbeit_schaetzung"] == {herbst["ferien_id"]: 12.0}

    res = client.put("/api/account/holiday-forecast", params=Q, json={"planned": {herbst["ferien_id"]: None}})
    assert res.json()["items"][0]["geplant_h"] is None

    bad = client.put("/api/account/holiday-forecast", params=Q, json={"planned": {"unbekannt": 3}})
    assert bad.status_code == 422 and "Unbekannte Ferien" in bad.json()["detail"]
    bad = client.put("/api/account/holiday-forecast", params=Q, json={"planned": {herbst["ferien_id"]: -2}})
    assert bad.status_code == 422


def test_compare_leave_modes(client, worked):
    body = client.get("/api/account/compare-leave-modes", params=Q).json()
    sommer, prop = body["varianten"]
    assert (sommer["mode"], prop["mode"]) == ("sommerferien_zuerst", "proportional")
    assert sommer["massgeblich"] and not prop["massgeblich"]
    assert sommer["label"] == "Urlaub = Sommerferien"
    assert sommer["saldo_periodenende"] == prop["saldo_periodenende"]
    assert prop["saldo_heute"] < sommer["saldo_heute"]
    assert sommer["urlaub"]["assigned"] == prop["urlaub"]["assigned"] == 30
    assert sommer["saldo_heute_hm"]


def test_statistics(client, worked):
    body = client.get("/api/account/statistics", params=Q).json()
    assert {c["code"] for c in body["categories"]} == {"U", "VN"}
    assert body["unterricht_share"] == pytest.approx(0.5)
    # Months/weeks only up to the Stichtag (no phantom deficit for the rest of the year).
    assert [m["year_month"] for m in body["months"]] == ["2026-08", "2026-09", "2026-10"]
    assert body["weeks"][-1]["monday"] <= body["stichtag"]
    assert body["months"][1]["ist"] == 12 * 10 * H  # 12 school days 15.–30.09.
    assert len(body["heatmap"]) == 365
    assert body["schulwochen_ueber_48"] == 5  # full weeks 21.09.–23.10. with 5 × 10 h = 50 h
    assert body["weeks"][0]["monday"] == "2026-07-27"
