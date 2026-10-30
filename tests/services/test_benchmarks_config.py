"""services/benchmarks.py: editable study reference values (Konzept §9.4)."""

from __future__ import annotations

import pytest

from app.services import benchmarks as bm


@pytest.fixture(autouse=True)
def _isolated(settings_env, monkeypatch):
    monkeypatch.delenv(bm.ENV_VAR, raising=False)
    bm._cache.clear()
    yield
    bm._cache.clear()


def test_default_file_loads_konzept_entries():
    assert bm.benchmarks_path() == bm.DEFAULT_FILE
    items = bm.load_benchmarks(strict=True)
    by_label = {i["label"]: i for i in items}
    nds = by_label["Grundschule Niedersachsen"]
    assert nds["hours_per_week"] == pytest.approx(47 + 58 / 60, abs=1e-3)
    assert nds["hours_text"] == "47:58"
    assert nds["year"] == "2015/16" and nds["source"]
    assert nds["hint"] == "Studienwert, nicht Norm"
    assert by_label["Grundschule Berlin"]["hours_text"] == "49:45"
    bllv = next(i for i in items if "BLLV" in i["label"])
    assert bllv["hours_per_week"] == 52.5
    assert bllv["kind"] == "verbandsschaetzung"
    assert bllv["kind_label"] == "Verbandsschätzung, nicht Norm"
    for item in items:
        assert set(item) >= {"label", "hours_per_week", "source", "year", "kind", "note"}


def test_env_override_relative_and_absolute(tmp_path, monkeypatch):
    custom = tmp_path / "bench.yaml"
    custom.write_text(
        "benchmarks:\n  - {label: Test, hours_per_week: '45,5', source: Eigene Quelle, year: 2026}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(bm.ENV_VAR, str(custom))
    assert bm.benchmarks_path() == custom
    items = bm.load_benchmarks()
    assert [(i["label"], i["hours_per_week"], i["year"], i["kind"]) for i in items] == [("Test", 45.5, "2026", "studie")]
    monkeypatch.setenv(bm.ENV_VAR, "config/benchmarks.yaml")
    assert bm.benchmarks_path() == bm.DEFAULT_FILE


def test_data_dir_file_is_picked_up(settings_env):
    settings_env.data_dir.mkdir(parents=True, exist_ok=True)
    target = settings_env.data_dir / "benchmarks.yaml"
    target.write_text("benchmarks:\n  - {label: Data, hours_per_week: 44, source: S, year: '2025'}\n", encoding="utf-8")
    assert bm.benchmarks_path() == target
    assert [i["label"] for i in bm.load_benchmarks()] == ["Data"]


def test_validation_requires_source_and_year():
    items, errors = bm.validate({"benchmarks": [
        {"label": "ok", "hours_per_week": 48, "source": "Q", "year": "2024"},
        {"label": "ohne Quelle", "hours_per_week": 48, "year": "2024"},
        {"label": "ohne Jahr", "hours_per_week": 48, "source": "Q"},
        {"label": "zu viel", "hours_per_week": 500, "source": "Q", "year": "2024"},
        {"label": "falsche Art", "hours_per_week": 40, "source": "Q", "year": "2024", "kind": "norm"},
        "kein Objekt",
        {"hours_per_week": 40, "source": "Q", "year": "2024"},
    ]})
    assert [i["label"] for i in items] == ["ok"]
    assert len(errors) == 6
    assert any("source" in e for e in errors) and any("year" in e for e in errors)
    assert bm.validate({"benchmarks": "nein"}) == ([], ["„benchmarks“ muss eine Liste sein."])
    assert bm.validate(None) == ([], [])


def test_invalid_entries_skipped_or_strict_error(tmp_path):
    f = tmp_path / "b.yaml"
    f.write_text(
        "benchmarks:\n  - {label: gut, hours_per_week: '47:30', source: Q, year: 2020}\n"
        "  - {label: schlecht, hours_per_week: 40}\n",
        encoding="utf-8",
    )
    assert [i["label"] for i in bm.load_benchmarks(f)] == ["gut"]
    with pytest.raises(bm.BenchmarkError, match="source"):
        bm.load_benchmarks(f, strict=True)


def test_missing_and_broken_files(tmp_path):
    assert bm.load_benchmarks(tmp_path / "fehlt.yaml") == []
    with pytest.raises(bm.BenchmarkError):
        bm.load_benchmarks(tmp_path / "fehlt.yaml", strict=True)
    broken = tmp_path / "kaputt.yaml"
    broken.write_text("benchmarks: [ {label: x", encoding="utf-8")
    assert bm.load_benchmarks(broken) == []


def test_parse_hours_formats():
    assert bm.parse_hours("47:58") == pytest.approx(47.9667, abs=1e-3)
    assert bm.parse_hours("52,5") == 52.5
    assert bm.parse_hours(49) == 49.0
    for bad in (True, None, "abc", 0, 101, "nan"):
        with pytest.raises(ValueError):
            bm.parse_hours(bad)


def test_cache_follows_file_changes(tmp_path):
    import os

    f = tmp_path / "b.yaml"
    f.write_text("benchmarks:\n  - {label: eins, hours_per_week: 40, source: Q, year: 2020}\n", encoding="utf-8")
    assert bm.load_benchmarks(f)[0]["label"] == "eins"
    f.write_text("benchmarks:\n  - {label: zwei, hours_per_week: 41, source: Q, year: 2020}\n", encoding="utf-8")
    st = f.stat()
    os.utime(f, (st.st_atime, st.st_mtime + 5))
    assert bm.load_benchmarks(f)[0]["label"] == "zwei"
