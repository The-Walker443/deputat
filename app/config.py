"""Runtime configuration read from environment variables.

Use ``get_config()`` everywhere instead of reading ``os.environ`` directly. The result is cached;
``reset_config()`` clears the cache (tests change the environment per test).

Note: the DB table ``settings`` holds the *user-editable* domain settings (UPZ, Urlaubsmodus, ...).
This module only covers deployment/runtime configuration.

There is deliberately no login (see DEPLOY.md): external access is protected by
Cloudflare Access, the LAN is open. The only credential is the Bearer token for ``/api/sensors``
(its hash lives in ``settings.api_token_hash``).

Variables: DATA_DIR, DATABASE_URL, APP_SECRET, BASE_URL, ALLOWED_HOSTS, PROXY_TRUSTED_NETS,
BACKUP_CRON, BACKUP_RETENTION_DAYS, LOG_LEVEL, CSRF_ENFORCE, SESSION_MAX_AGE, COOKIE_SECURE.

``COOKIE_SECURE`` (``auto`` | ``true`` | ``false``, default ``false``) sets the ``Secure`` flag of the session
cookie. The cookie only carries the CSRF token and flash messages (there is no login), and the LAN may use
plain http: a Secure cookie never reaches an http client, so every form POST there would fail with 403.
``auto`` = Secure when ``BASE_URL`` is https.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

APP_VERSION = "0.1.0"
RULES_VERSION = "1"

# Docker networks (NPM reaches the app through proxy_net, 172.19.0.0/16).
DEFAULT_TRUSTED_NETS = "172.16.0.0/12"
DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_BACKUP_CRON = "30 0 * * *"
DEFAULT_BACKUP_RETENTION_DAYS = 14
COOKIE_SECURE_MODES = ("auto", "true", "false")

# Always accepted Host names: the container healthcheck calls http://127.0.0.1:8000/healthz. A DNS
# rebinding attacker cannot make a browser send these, so allowing them is safe.
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
# Added to the *default* host list only (Starlette's TestClient uses Host: testserver).
TEST_HOSTS = ("testserver",)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value is not None else default


@dataclass(frozen=True)
class Config:
    data_dir: Path
    database_url: str
    app_secret: str
    backup_cron: str
    backup_retention_days: int
    base_url: str
    allowed_hosts: frozenset[str]
    log_level: str
    proxy_trusted_nets: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    csrf_enforce: bool
    session_max_age: int
    cookie_secure: str = "false"  # auto | true | false (see module docstring)
    app_version: str = APP_VERSION
    rules_version: str = RULES_VERSION

    @property
    def base_url_https(self) -> bool:
        return self.base_url.lower().startswith("https://")

    @property
    def secure_cookies(self) -> bool:
        """``Secure`` flag of the session cookie (COOKIE_SECURE; ``auto`` follows BASE_URL)."""
        if self.cookie_secure == "auto":
            return self.base_url_https
        return self.cookie_secure == "true"

    @property
    def db_path(self) -> Path | None:
        """Filesystem path of the SQLite DB, or None for non-file URLs."""
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix) and ":memory:" not in self.database_url:
            return Path(self.database_url[len(prefix):])
        return None

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def imports_dir(self) -> Path:
        return self.data_dir / "imports"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.backups_dir, self.reports_dir, self.exports_dir, self.imports_dir):
            path.mkdir(parents=True, exist_ok=True)


def _load_or_create_secret(data_dir: Path) -> str:
    """Persist a generated secret in DATA_DIR so sessions survive restarts when APP_SECRET is unset."""
    secret_file = data_dir / ".app_secret"
    try:
        if secret_file.exists():
            return secret_file.read_text(encoding="utf-8").strip()
        data_dir.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_urlsafe(48)
        secret_file.write_text(secret, encoding="utf-8")
        return secret
    except OSError:
        return secrets.token_urlsafe(48)


def _parse_nets(raw: str) -> tuple:
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return tuple(nets)


def normalize_host(value: str) -> str:
    """Lower-case host name without port; IPv6 literals without brackets (``[::1]:8000`` -> ``::1``)."""
    value = value.strip().lower().rstrip(".")
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else value[1:]
    if value.count(":") == 1:  # host:port (a bare IPv6 address has several colons)
        value = value.split(":", 1)[0]
    return value


def _parse_allowed_hosts(raw: str | None, base_url: str) -> frozenset[str]:
    if raw is None:
        hosts = {normalize_host(urlsplit(base_url).hostname or ""), *TEST_HOSTS}
    else:
        hosts = {normalize_host(part) for part in raw.split(",")}
    hosts.update(LOOPBACK_HOSTS)
    hosts.discard("")
    return frozenset(hosts)


def _parse_cookie_secure(raw: str | None) -> str:
    value = (raw or "false").strip().lower()
    aliases = {"1": "true", "yes": "true", "on": "true", "0": "false", "no": "false", "off": "false"}
    value = aliases.get(value, value)
    if value not in COOKIE_SECURE_MODES:
        raise ValueError(f"COOKIE_SECURE must be one of {', '.join(COOKIE_SECURE_MODES)} (got {raw!r})")
    return value


def load_config() -> Config:
    data_dir = Path(_env("DATA_DIR", "./data")).resolve()
    database_url = _env("DATABASE_URL") or f"sqlite:///{(data_dir / 'deputat.sqlite').as_posix()}"
    base_url = (_env("BASE_URL", DEFAULT_BASE_URL) or "").rstrip("/")

    return Config(
        data_dir=data_dir,
        database_url=database_url,
        app_secret=_env("APP_SECRET") or _load_or_create_secret(data_dir),
        backup_cron=_env("BACKUP_CRON", DEFAULT_BACKUP_CRON),
        backup_retention_days=_env_int("BACKUP_RETENTION_DAYS", DEFAULT_BACKUP_RETENTION_DAYS),
        base_url=base_url,
        allowed_hosts=_parse_allowed_hosts(_env("ALLOWED_HOSTS"), base_url),
        log_level=(_env("LOG_LEVEL", "info") or "info").lower(),
        proxy_trusted_nets=_parse_nets(_env("PROXY_TRUSTED_NETS", DEFAULT_TRUSTED_NETS)),
        csrf_enforce=_env_bool("CSRF_ENFORCE", True),
        session_max_age=_env_int("SESSION_MAX_AGE", 30 * 24 * 3600),
        cookie_secure=_parse_cookie_secure(_env("COOKIE_SECURE")),
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()


def reset_config() -> None:
    """Drop the cached config (and DB engines bound to it). Used by tests."""
    get_config.cache_clear()
    try:
        from app.db.session import dispose_engines
    except ImportError:  # pragma: no cover - during partial imports
        return
    dispose_engines()
