"""settings_service: validation, Altersermäßigung (Stichtag), Teilzeitfaktor, API token."""

from __future__ import annotations

import hashlib
from datetime import date

import pytest

from app.core.types import SollParams
from app.services import settings_service as svc

# =========================================================================== Altersermäßigung


@pytest.mark.parametrize(
    ("birthday", "school_year", "expected"),
    [
        (None, 2026, 0),
        (date(1980, 5, 5), 2026, 0),
        # Birthday 01.08.–31.01. → reduction in the school year it falls into.
        (date(1968, 9, 15), 2026, 1),
        (date(1968, 9, 15), 2025, 0),
        (date(1968, 1, 31), 2025, 1),
        (date(1968, 8, 1), 2026, 1),
        (date(1968, 7, 31), 2025, 0),  # 58 on 31.07.2026 → from 2026/27
        (date(1968, 7, 31), 2026, 1),
        # Birthday 01.02.–31.07. → only from the following school year.
        (date(1968, 3, 10), 2025, 0),
        (date(1968, 3, 10), 2026, 1),
        (date(1968, 2, 1), 2025, 0),
        (date(1968, 2, 29), 2026, 1),
        # Higher levels.
        (date(1966, 8, 1), 2026, 2),
        (date(1966, 8, 1), 2025, 1),
        (date(1964, 12, 1), 2026, 3),
        (date(1950, 1, 1), 2026, 3),
    ],
)
def test_compute_altersermaessigung(birthday, school_year, expected):
    assert svc.compute_altersermaessigung(birthday, school_year) == expected


def test_school_year_start_year():
    assert svc.school_year_start_year(date(2026, 7, 31)) == 2025
    assert svc.school_year_start_year(date(2026, 8, 1)) == 2026


def test_altersermaessigung_computed_and_overridable(db):
    s = svc.update_settings(db, geburtsdatum=date(1966, 9, 1), today=date(2026, 10, 1))
    assert s.altersermaessigung == 2 and not s.altersermaessigung_manuell

    s = svc.update_settings(db, altersermaessigung=1, today=date(2026, 10, 1))
    assert s.altersermaessigung == 1 and s.altersermaessigung_manuell
    assert svc.get_settings(db, today=date(2026, 10, 1)).altersermaessigung == 1  # kept

    s = svc.update_settings(db, altersermaessigung_manuell=False, today=date(2026, 10, 1))
    assert s.altersermaessigung == 2

    # get_settings refreshes the automatic value for the current school year.
    assert svc.get_settings(db, today=date(2025, 10, 1)).altersermaessigung == 1


def test_settings_dict_hides_secrets_and_shows_derived(db):
    svc.generate_api_token(db)
    data = svc.settings_dict(svc.get_settings(db))
    assert "api_token_hash" not in data and "password_hash" not in data
    assert data["api_token_configured"] is True
    assert data["teilzeitfaktor"] == 1.0
    assert data["urlaubs_modus"] == "sommerferien_zuerst"


# =========================================================================== Teilzeitfaktor


def test_soll_params_adds_reductions_back(db):
    s = svc.get_settings(db)
    assert svc.soll_params(s) == SollParams(40.0, 28, 28.0)
    svc.update_settings(db, upz_ist=24, altersermaessigung=1, anrechnungsstunden=1)
    s = svc.get_settings(db)
    assert svc.soll_params(s).upz_ist == 26.0
    assert svc.teilzeitfaktor(s) == pytest.approx(26 / 28)
    assert svc.settings_hints(s) == []


def test_soll_params_capped_and_hint(db):
    svc.update_settings(db, upz_ist=28, altersermaessigung=2)
    s = svc.get_settings(db)
    assert svc.soll_params(s).upz_ist == 28.0
    assert any("übersteigt" in h for h in svc.settings_hints(s))


# =========================================================================== validation


def test_update_valid_fields(db):
    s = svc.update_settings(
        db,
        name=" Frau Muster ",
        klassen=["3b", " 3b", "", "4a"],
        zeitraster=[{"slot_no": 2, "start": "08:45", "end": "09:30"}, {"slot_no": 1, "start": "08:00", "end": "08:45"}],
        klassenfahrt_variante="A",
        klassenfahrt_cap_h=12,
        kontoperiode="kalenderjahr",
        periode_start_md="09-01",
        ferienarbeit_schaetzung={"abc": 12, "def": None},
        ui_default_location_rule={"inside": "schule", "outside": "zuhause", "from": "07:00", "to": "14:00"},
    )
    assert s.name == "Frau Muster"
    assert s.klassen == ["3b", "4a"]
    assert [slot["slot_no"] for slot in s.zeitraster] == [1, 2]
    assert s.zeitraster[0]["label"] == "1. Stunde"
    assert (s.klassenfahrt_variante, s.klassenfahrt_cap_h) == ("A", 12.0)
    assert s.ferienarbeit_schaetzung == {"abc": 12.0}


@pytest.mark.parametrize(
    ("fields", "field"),
    [
        ({"upz_voll": 0}, "upz_voll"),
        ({"upz_voll": 28.5}, "upz_voll"),
        ({"upz_ist": 29}, "upz_ist"),
        ({"upz_ist": 20.3}, "upz_ist"),
        ({"upz_ist": -1}, "upz_ist"),
        ({"upz_ist": True}, "upz_ist"),
        ({"upz_ist": "28"}, "upz_ist"),
        ({"anrechnungsstunden": 41}, "anrechnungsstunden"),
        ({"altersermaessigung": 11}, "altersermaessigung"),
        ({"kontoperiode": "monat"}, "kontoperiode"),
        ({"periode_start_md": "13-01"}, "periode_start_md"),
        ({"periode_start_md": "8-1"}, "periode_start_md"),
        ({"urlaubs_modus": "irgendwann"}, "urlaubs_modus"),
        ({"regionale_feiertage": ["fasching"]}, "regionale_feiertage"),
        ({"regionale_feiertage": "mariae_himmelfahrt"}, "regionale_feiertage"),
        ({"klassenfahrt_variante": "C"}, "klassenfahrt_variante"),
        ({"klassenfahrt_cap_h": 0}, "klassenfahrt_cap_h"),
        ({"wochen_soll_voll_h": 100}, "wochen_soll_voll_h"),
        ({"urlaubstage": -1}, "urlaubstage"),
        ({"prognose_schultage": 0}, "prognose_schultage"),
        ({"geburtsdatum": date(2999, 1, 1)}, "geburtsdatum"),
        ({"geburtsdatum": "gestern"}, "geburtsdatum"),
        ({"bundesland": "BW"}, "bundesland"),
        ({"krank_neutralisiert": "ja"}, "krank_neutralisiert"),
        ({"zeitraster": [{"slot_no": 1, "start": "9:00", "end": "09:45"}]}, "zeitraster"),
        ({"zeitraster": [{"slot_no": 1, "start": "09:45", "end": "09:00"}]}, "zeitraster"),
        ({"zeitraster": [{"slot_no": 1, "start": "08:00", "end": "08:45"}] * 2}, "zeitraster"),
        ({"ferienarbeit_schaetzung": {"x": -1}}, "ferienarbeit_schaetzung"),
        ({"ui_default_location_rule": {"inside": "mond"}}, "ui_default_location_rule"),
        ({"password_hash": "x"}, "password_hash"),
        ({"api_token_hash": "x"}, "api_token_hash"),
        ({"gibt_es_nicht": 1}, "gibt_es_nicht"),
    ],
)
def test_update_validation_errors(db, fields, field):
    with pytest.raises(svc.SettingsValidationError) as exc:
        svc.update_settings(db, **fields)
    assert exc.value.field == field
    assert str(exc.value)


def test_failed_update_changes_nothing(db):
    with pytest.raises(svc.SettingsValidationError):
        svc.update_settings(db, name="Neu", upz_ist=99)
    db.expire_all()
    assert svc.get_settings(db).name == ""


def test_set_ferienarbeit_schaetzung_merges(db):
    assert svc.set_ferienarbeit_schaetzung(db, {"a": 10, "b": 2.5}) == {"a": 10.0, "b": 2.5}
    assert svc.set_ferienarbeit_schaetzung(db, {"a": None, "c": 1}) == {"b": 2.5, "c": 1.0}
    db.expire_all()
    assert svc.get_settings(db).ferienarbeit_schaetzung == {"b": 2.5, "c": 1.0}
    with pytest.raises(svc.SettingsValidationError):
        svc.set_ferienarbeit_schaetzung(db, {"a": 5000})


# =========================================================================== API token


def test_api_token_lifecycle(db):
    assert not svc.api_token_configured(db)
    token = svc.generate_api_token(db)
    stored = svc.get_settings(db).api_token_hash
    assert len(token) >= 32 and token not in stored
    assert stored == hashlib.sha256(token.encode()).hexdigest()
    assert svc.api_token_configured(db)
    assert svc.verify_api_token(db, token)
    assert not svc.verify_api_token(db, token + "x")

    rotated = svc.generate_api_token(db)
    assert rotated != token
    assert not svc.verify_api_token(db, token)
    assert svc.verify_api_token(db, rotated)

    svc.clear_api_token(db)
    assert not svc.api_token_configured(db)
    assert not svc.verify_api_token(db, rotated)
