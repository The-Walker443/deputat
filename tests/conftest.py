"""Shared fixtures for service/API/web tests.

- ``settings_env``: isolated environment (tmp DATA_DIR, CSRF_ENFORCE=false) and a
  fresh ``get_config()``; yields the Config. Tests may ``monkeypatch.setenv`` more variables and call
  ``app.config.reset_config()`` afterwards.
- ``db``: ORM Session on a freshly migrated + seeded SQLite file in tmp_path.
- ``client``: ``fastapi.testclient.TestClient`` on ``create_app()`` (lifespan runs: migrate + seed).
- ``no_ferien_catalog``: empty ferien catalogue, so new periods start without the official ferien (by default
  the bundled ``config/ferien_bayern.yaml`` is used and covered school years get their ferien automatically).

App imports happen inside the fixtures so the pure core tests never depend on the web stack.
"""

from __future__ import annotations

import sqlite3

import pytest

# Every variable app.config reads, so a developer's shell environment never leaks into tests.
ENV_VARS = (
    "DATA_DIR", "DATABASE_URL", "APP_SECRET", "ALLOWED_HOSTS", "BACKUP_CRON", "BACKUP_RETENTION_DAYS",
    "BASE_URL", "LOG_LEVEL", "PROXY_TRUSTED_NETS", "CSRF_ENFORCE", "SESSION_MAX_AGE", "DEPUTAT_SCHEDULER",
    "COOKIE_SECURE", "FERIEN_CATALOG_FILE",
)

EMPTY_FERIEN_CATALOG = """\
# Empty ferien catalogue for tests: periods start without the official ferien.
bundesland: BY
source_url: "https://example.invalid/leer"
source_title: "Leerer Testkatalog"
retrieved: 2026-10-30
blocks: []
"""


@pytest.fixture(scope="session")
def _migrated_template(tmp_path_factory):
    """Migrate + seed one template DB per test session; ``db`` copies it (much faster than migrating)."""
    from app.config import reset_config
    from app.db.seed import seed
    from app.db.session import SessionLocal, init_db

    data_dir = tmp_path_factory.mktemp("template-data")
    mp = pytest.MonkeyPatch()
    for var in ENV_VARS:
        mp.delenv(var, raising=False)
    mp.setenv("DATA_DIR", str(data_dir))
    mp.setenv("APP_SECRET", "template-secret")
    reset_config()
    try:
        init_db()
        with SessionLocal() as session:
            seed(session)
    finally:
        reset_config()
        mp.undo()
    return data_dir / "deputat.sqlite"


@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    from app.config import get_config, reset_config

    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("APP_SECRET", "test-secret-0123456789-0123456789")
    monkeypatch.setenv("CSRF_ENFORCE", "false")
    reset_config()
    yield get_config()
    reset_config()


@pytest.fixture
def no_ferien_catalog(settings_env, tmp_path, monkeypatch):
    """Empty ferien catalogue (``FERIEN_CATALOG_FILE``): new periods start without the official ferien.

    For tests that build their own ferien scenario (ICS example, hand-made blocks).
    """
    path = tmp_path / "ferien_leer.yaml"
    path.write_text(EMPTY_FERIEN_CATALOG, encoding="utf-8")
    monkeypatch.setenv("FERIEN_CATALOG_FILE", str(path))
    return path


def _ensure_db_from_template(template, dst) -> None:
    """Copy the migrated template DB to ``dst`` unless a DB already exists there."""
    if dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    source, target = sqlite3.connect(template), sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()


@pytest.fixture
def db(settings_env, _migrated_template):
    from app.db.seed import seed
    from app.db.session import SessionLocal, init_db

    _ensure_db_from_template(_migrated_template, settings_env.db_path)
    init_db()  # no-op upgrade, keeps the fixture honest if the template is stale
    session = SessionLocal()
    seed(session)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(settings_env, _migrated_template):
    from fastapi.testclient import TestClient

    from app.main import create_app

    _ensure_db_from_template(_migrated_template, settings_env.db_path)
    with TestClient(create_app()) as test_client:
        yield test_client
