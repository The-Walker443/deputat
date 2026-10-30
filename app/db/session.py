"""Engine/session handling and schema initialization.

- ``get_engine()`` returns a cached engine for the current ``get_config().database_url``
  (SQLite with WAL, foreign keys, busy timeout).
- ``SessionLocal()`` opens a new ORM session bound to that engine.
- ``get_db()`` is the FastAPI dependency (one session per request, closed afterwards).
- ``init_db()`` creates the data directories and runs ``alembic upgrade head`` programmatically.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from threading import Lock

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import PROJECT_ROOT, get_config

log = logging.getLogger(__name__)

_engines: dict[str, Engine] = {}
_engines_lock = Lock()
_session_factory = sessionmaker(autoflush=False, expire_on_commit=False)


def _set_sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def get_engine() -> Engine:
    url = get_config().database_url
    with _engines_lock:
        engine = _engines.get(url)
        if engine is None:
            if url.startswith("sqlite"):
                engine = create_engine(url, connect_args={"check_same_thread": False})
                event.listen(engine, "connect", _set_sqlite_pragmas)
            else:  # pragma: no cover - only SQLite is supported, kept for completeness
                engine = create_engine(url, pool_pre_ping=True)
            _engines[url] = engine
        return engine


def dispose_engines() -> None:
    with _engines_lock:
        for engine in _engines.values():
            engine.dispose()
        _engines.clear()


def SessionLocal() -> Session:  # noqa: N802 - conventional name
    return _session_factory(bind=get_engine())


def get_db() -> Iterator[Session]:
    """FastAPI dependency: ``db: Session = Depends(get_db)``."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def alembic_config():
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", get_config().database_url)
    cfg.attributes["configure_logger"] = False
    return cfg


def init_db() -> None:
    """Ensure directories exist and migrate the database to the latest revision."""
    from alembic import command

    get_config().ensure_dirs()
    cfg = alembic_config()
    with get_engine().begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    log.info("Database migrated to head")
