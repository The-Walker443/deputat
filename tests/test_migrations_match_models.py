"""Alembic migrations (incl. 0002–0004) produce exactly the schema of app/db/models.py."""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect


def test_migrated_schema_matches_models(db):
    from app.db.base import Base

    conn = db.connection()
    diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
    columns = {c["name"] for c in inspect(conn).get_columns("month_close")}
    assert {"note", "forced_items"} <= columns
    assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == "0005"
    settings_columns = {c["name"] for c in inspect(conn).get_columns("settings")}
    assert "password_hash" not in settings_columns  # no login (0003)
    assert "api_token_hash" in settings_columns


def test_upgrade_from_0001_keeps_data_and_drops_password_hash(settings_env, tmp_path):
    """A database created by 0001 (with a settings row) upgrades to head; 0003 drops ``password_hash``."""
    from alembic import command
    from sqlalchemy import create_engine

    from app.db.session import alembic_config

    url = f"sqlite:///{(tmp_path / 'old.sqlite').as_posix()}"
    engine = create_engine(url)
    try:
        cfg = alembic_config()
        cfg.set_main_option("sqlalchemy.url", url)
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "0001")
            conn.exec_driver_sql(
                "INSERT INTO month_close (year_month, closed_at, entries_hash_sha256, soll_seconds, ist_seconds, "
                "saldo_seconds, status, hash_history) VALUES ('2026-09', '2026-10-01 08:00:00', 'abc', 1, 2, 1, "
                "'abgeschlossen', '[]')"
            )
            columns = {c["name"] for c in inspect(conn).get_columns("settings")}
            assert "password_hash" in columns
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == "0005"
            assert "password_hash" not in {c["name"] for c in inspect(conn).get_columns("settings")}
            row = conn.exec_driver_sql("SELECT note, forced_items FROM month_close").one()
            assert row == (None, "[]")
        with engine.begin() as conn:  # and back down again
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "0001")
            assert "password_hash" in {c["name"] for c in inspect(conn).get_columns("settings")}
    finally:
        engine.dispose()


def test_0005_adds_category_az_to_an_existing_database(settings_env, tmp_path):
    """An installation at 0004 (catalogue seeded, no AZ) gets the AZ main category from migration 0005;
    an empty catalogue (fresh database) is left to the seed. Downgrade removes AZ only while unused."""
    from alembic import command
    from sqlalchemy import create_engine

    from app.db.session import alembic_config

    url = f"sqlite:///{(tmp_path / 'existing.sqlite').as_posix()}"
    engine = create_engine(url)
    query = "SELECT name, sort, is_unterricht, is_pseudo, active, parent_id FROM category WHERE code = 'AZ'"
    try:
        cfg = alembic_config()
        cfg.set_main_option("sqlalchemy.url", url)
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "0004")
            conn.exec_driver_sql(
                "INSERT INTO category (code, name, color, sort, is_unterricht, counts_as_mehrarbeit_candidate, "
                "active, is_pseudo, requires_note) VALUES ('U', 'Unterricht', '#2563EB', 10, 1, 1, 1, 0, 0)"
            )
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            assert conn.exec_driver_sql(query).one() == (
                "Arbeitszeit (pauschal, nicht aufgeschlüsselt)", 150, 0, 0, 1, None)
            command.upgrade(cfg, "head")  # no-op
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM category WHERE code = 'AZ'").scalar() == 1
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "0004")
            assert conn.exec_driver_sql(query).first() is None
    finally:
        engine.dispose()

    fresh = f"sqlite:///{(tmp_path / 'fresh.sqlite').as_posix()}"
    engine = create_engine(fresh)
    try:
        cfg = alembic_config()
        cfg.set_main_option("sqlalchemy.url", fresh)
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            assert conn.exec_driver_sql("SELECT COUNT(*) FROM category").scalar() == 0  # seed fills it in order
    finally:
        engine.dispose()


def test_json_backup_of_0004_restores_into_0005(db):
    """0005 is data-only: an older JSON backup (without AZ) restores, the seed afterwards adds AZ."""
    from app.db.models import Category
    from app.services import export

    backup = export.export_json(db)
    backup["schema_revision"] = "0004"
    backup["tables"]["category"] = [c for c in backup["tables"]["category"] if c["code"] != "AZ"]
    backup["sha256"] = export._checksum(backup["tables"])
    export.restore_json(db, backup, replace=True, safety_backup=False)
    assert db.query(Category).filter_by(code="AZ", parent_id=None).count() == 1

    backup["schema_revision"] = "0003"
    import pytest

    with pytest.raises(export.ExportError):
        export.restore_json(db, backup, replace=True, safety_backup=False)
