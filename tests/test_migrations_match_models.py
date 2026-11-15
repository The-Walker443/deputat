"""Alembic migrations (incl. 0002–0006) produce exactly the schema of app/db/models.py."""

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
    assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == "0006"
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
            assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == "0006"
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


def test_0006_marks_existing_tagessummen(settings_env, tmp_path):
    """A database at 0005 with day totals (incl. a backfilled one with a note) gets ``time_entry.tagessumme``:
    per day the newest entry of the old criteria is marked; deleted, bounded, other-category, subcategory and
    imported entries are not. ``day_override`` gets ``erfassung_typ``/``erfassung_typ_at``. Downgrade drops them."""
    from alembic import command
    from sqlalchemy import create_engine

    from app.db.session import alembic_config

    url = f"sqlite:///{(tmp_path / 'live.sqlite').as_posix()}"
    engine = create_engine(url)
    entry_sql = (
        "INSERT INTO time_entry (id, start_at, end_at, duration_seconds, has_bounds, day, category_id, "
        "subcategory_id, note, flags, source, status, created_at, updated_at, deleted_at, locked) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[\"ohne_zeitgrenzen\"]', ?, 'active', ?, ?, ?, 0)"
    )
    try:
        cfg = alembic_config()
        cfg.set_main_option("sqlalchemy.url", url)
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "0005")
            for code, sort in (("U", 10), ("AZ", 150)):
                conn.exec_driver_sql(
                    "INSERT INTO category (code, name, color, sort, is_unterricht, counts_as_mehrarbeit_candidate, "
                    "active, is_pseudo, requires_note) VALUES (?, ?, '#2563EB', ?, 0, 0, 1, 0, 0)", (code, code, sort))
            u, az = (conn.exec_driver_sql("SELECT id FROM category WHERE code = ?", (c,)).scalar() for c in ("U", "AZ"))
            conn.exec_driver_sql(
                "INSERT INTO category (code, name, color, sort, parent_id, is_unterricht, "
                "counts_as_mehrarbeit_candidate, active, is_pseudo, requires_note) "
                "VALUES ('AZ.1', 'Sub', '#2563EB', 10, ?, 0, 0, 1, 0, 0)", (az,))
            sub = conn.exec_driver_sql("SELECT id FROM category WHERE code = 'AZ.1'").scalar()
            rows = [
                # id, day, category, sub, note, source, has_bounds, created, deleted
                ("a-mon", "2026-11-02", az, None, None, "manuell", 0, "2026-11-02 20:00:00", None),
                ("b-tue-old", "2026-11-03", az, None, None, "manuell", 0, "2026-11-03 18:00:00", None),
                ("c-tue-new", "2026-11-03", az, None, None, "manuell", 0, "2026-11-03 19:00:00", None),
                ("d-backfill", "2026-10-05", az, None, "Nachtrag aus Papierliste", "manuell", 0,
                 "2026-11-10 09:00:00", None),
                ("e-deleted", "2026-11-04", az, None, None, "manuell", 0, "2026-11-04 20:00:00",
                 "2026-11-04 21:00:00"),
                ("f-bounded", "2026-11-05", az, None, None, "manuell", 1, "2026-11-05 20:00:00", None),
                ("g-other-cat", "2026-11-06", u, None, None, "manuell", 0, "2026-11-06 20:00:00", None),
                ("h-sub", "2026-11-09", az, sub, None, "manuell", 0, "2026-11-09 20:00:00", None),
                ("i-import", "2026-11-10", az, None, None, "import", 0, "2026-11-10 20:00:00", None),
            ]
            for rid, day, cat, subcat, note, source, bounds, created, deleted in rows:
                conn.exec_driver_sql(entry_sql, (rid, f"{day} 00:00:00", f"{day} 06:00:00", 21600, bounds, day, cat,
                                                 subcat, note, source, created, created, deleted))
            conn.exec_driver_sql("INSERT INTO day_override (date, absence_type, urlaub_manuell, updated_at) "
                                 "VALUES ('2026-11-03', 'krank', 0, '2026-11-03 07:00:00')")
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            marked = dict(conn.exec_driver_sql("SELECT id, tagessumme FROM time_entry").fetchall())
            assert {k for k, v in marked.items() if v} == {"a-mon", "c-tue-new", "d-backfill"}
            assert conn.exec_driver_sql(
                "SELECT absence_type, erfassung_typ, erfassung_typ_at FROM day_override").one() == ("krank", None, None)
            # the hash fields are untouched (closed months keep their Prüf-Hash)
            assert conn.exec_driver_sql("SELECT flags FROM time_entry WHERE id = 'a-mon'").scalar() == \
                '["ohne_zeitgrenzen"]'
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            conn.exec_driver_sql("INSERT INTO day_override (date, urlaub_manuell, erfassung_typ, updated_at) "
                                 "VALUES ('2026-11-12', 0, 'klassenfahrt', '2026-11-12 07:00:00')")
            command.downgrade(cfg, "0005")
            assert "tagessumme" not in {c["name"] for c in inspect(conn).get_columns("time_entry")}
            assert "erfassung_typ" not in {c["name"] for c in inspect(conn).get_columns("day_override")}
            assert [r[0] for r in conn.exec_driver_sql("SELECT date FROM day_override").fetchall()] == ["2026-11-03"]
    finally:
        engine.dispose()


def test_0006_runs_with_foreign_keys_on_revisions_and_a_leftover_copy(settings_env, tmp_path):
    """Like production: foreign_keys=ON (app engine pragmas), day totals with revisions referencing
    time_entry, and the ``_alembic_tmp_time_entry`` table a failed first release of 0006 left behind."""
    from alembic import command
    from sqlalchemy import create_engine, event

    from app.db.session import _set_sqlite_pragmas, alembic_config

    url = f"sqlite:///{(tmp_path / 'prod.sqlite').as_posix()}"
    engine = create_engine(url)
    event.listen(engine, "connect", _set_sqlite_pragmas)
    try:
        cfg = alembic_config()
        cfg.set_main_option("sqlalchemy.url", url)
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "0005")
            conn.exec_driver_sql(
                "INSERT INTO category (code, name, color, sort, is_unterricht, counts_as_mehrarbeit_candidate, "
                "active, is_pseudo, requires_note) VALUES ('AZ', 'AZ', '#2563EB', 150, 0, 0, 1, 0, 0)")
            az = conn.exec_driver_sql("SELECT id FROM category WHERE code = 'AZ'").scalar()
            conn.exec_driver_sql(
                "INSERT INTO time_entry (id, start_at, end_at, duration_seconds, has_bounds, day, category_id, "
                "note, flags, source, status, created_at, updated_at, locked) VALUES ('nachtrag', "
                "'2026-09-15 00:00:00', '2026-09-15 09:00:00', 32400, 0, '2026-09-15', ?, 'Nachtrag', "
                "'[\"nachtraeglich\", \"ohne_zeitgrenzen\"]', 'manuell', 'active', '2026-11-15 12:00:00', "
                "'2026-11-15 12:00:00', 0)", (az,))
            conn.exec_driver_sql(
                "INSERT INTO time_entry_revision (id, entry_id, revision_no, changed_at, change_reason, snapshot_json) "
                "VALUES ('rev-1', 'nachtrag', 1, '2026-11-15 12:00:00', 'Nachtrag', '{}')")
            conn.exec_driver_sql("CREATE TABLE _alembic_tmp_time_entry (id VARCHAR PRIMARY KEY)")
        with engine.begin() as conn:
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
            assert conn.exec_driver_sql("SELECT tagessumme FROM time_entry WHERE id = 'nachtrag'").scalar() == 1
            assert conn.exec_driver_sql("SELECT count(*) FROM time_entry_revision").scalar() == 1
            assert "_alembic_tmp_time_entry" not in inspect(conn).get_table_names()
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "0005")
            assert "tagessumme" not in {c["name"] for c in inspect(conn).get_columns("time_entry")}
            assert conn.exec_driver_sql("SELECT count(*) FROM time_entry_revision").scalar() == 1
    finally:
        engine.dispose()


def test_json_backup_of_0005_restores_into_0006_and_marks_day_totals(db):
    """A JSON backup taken before 0006 (no ``tagessumme`` / ``erfassung_typ`` columns) restores; its day totals
    are marked afterwards like migration 0006 does, and they keep working on "Erfassen"."""
    from datetime import date, datetime

    from sqlalchemy import select

    from app.db.models import Category, TimeEntry
    from app.services import export
    from app.services import quick_entry as qe

    az = db.scalar(select(Category.id).where(Category.code == "AZ", Category.parent_id.is_(None)))
    day = date(2026, 11, 3)
    now = datetime(2026, 11, 3, 19, 0)
    qe.set_day_total(db, day, 6 * 3600, now=now, today=day)
    backup = export.export_json(db)
    backup["schema_revision"] = "0005"
    for row in backup["tables"]["time_entry"]:
        row.pop("tagessumme")
    for row in backup["tables"]["day_override"]:
        row.pop("erfassung_typ"), row.pop("erfassung_typ_at")
    backup["sha256"] = export._checksum(backup["tables"])
    export.restore_json(db, backup, replace=True, safety_backup=False)
    (entry,) = db.scalars(select(TimeEntry).where(TimeEntry.day == day)).all()
    assert entry.tagessumme is True and entry.category_id == az
    qe.set_day_typ(db, day, "klassenfahrt", now=now, today=day)  # works without a school year
    db.refresh(entry)
    assert db.get(Category, entry.category_id).code == "SV" and "klassenfahrt" in entry.flags
    assert qe.day_state(db, day).tagessumme.id == entry.id
