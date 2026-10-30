"""Unit tests for app.security helpers and the security-related parts of app.config."""

from __future__ import annotations

import asyncio
import hashlib
import re

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.config import get_config, normalize_host, reset_config
from app.db.models import Settings


def _request(client=("10.0.0.1", 1234), headers=None, method="GET", path="/") -> Request:
    raw = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw,
        "client": client,
        "server": ("testserver", 80),
        "scheme": "http",
    }
    return Request(scope)


# --------------------------------------------------------------------------- config


def test_config_defaults(settings_env, monkeypatch):
    for var in ("BACKUP_CRON", "BACKUP_RETENTION_DAYS", "BASE_URL", "ALLOWED_HOSTS", "PROXY_TRUSTED_NETS",
                "LOG_LEVEL", "CSRF_ENFORCE"):
        monkeypatch.delenv(var, raising=False)
    reset_config()
    cfg = get_config()
    assert cfg.backup_cron == "30 0 * * *"
    assert cfg.backup_retention_days == 14
    assert cfg.base_url == "http://localhost:8000"
    assert cfg.allowed_hosts == frozenset({"localhost", "127.0.0.1", "::1", "testserver"})
    assert [str(n) for n in cfg.proxy_trusted_nets] == ["172.16.0.0/12"]
    assert cfg.log_level == "info"
    assert cfg.csrf_enforce is True
    assert cfg.secure_cookies is False
    for removed in ("auth_mode", "admin_password_hash", "admin_password", "proxy_user_header"):
        assert not hasattr(cfg, removed)


def test_config_from_env(settings_env, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://time.bauer.center/")
    monkeypatch.setenv("BACKUP_CRON", "0 23 * * *")
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "30")
    monkeypatch.setenv("PROXY_TRUSTED_NETS", "10.0.0.0/8, 192.168.0.0/16")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    reset_config()
    cfg = get_config()
    assert cfg.base_url == "https://time.bauer.center"
    assert cfg.secure_cookies is False  # COOKIE_SECURE defaults to false (http LAN clients)
    assert cfg.base_url_https is True
    assert cfg.allowed_hosts == frozenset({"time.bauer.center", "testserver", "localhost", "127.0.0.1", "::1"})
    assert cfg.backup_cron == "0 23 * * *"
    assert cfg.backup_retention_days == 30
    assert [str(n) for n in cfg.proxy_trusted_nets] == ["10.0.0.0/8", "192.168.0.0/16"]
    assert cfg.log_level == "debug"


def test_config_explicit_allowed_hosts(settings_env, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://time.bauer.center")
    monkeypatch.setenv("ALLOWED_HOSTS", " Time.Bauer.Center:443 , deputat,, [fd00::5] ")
    reset_config()
    assert get_config().allowed_hosts == frozenset(
        {"time.bauer.center", "deputat", "fd00::5", "localhost", "127.0.0.1", "::1"}
    )


def test_app_secret_generated_and_persisted(settings_env, monkeypatch, tmp_path):
    data_dir = tmp_path / "fresh-data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.delenv("APP_SECRET", raising=False)
    reset_config()
    secret = get_config().app_secret
    assert len(secret) >= 32
    assert (data_dir / ".app_secret").read_text(encoding="utf-8").strip() == secret
    reset_config()
    assert get_config().app_secret == secret  # survives restarts


def test_app_secret_from_env_wins(settings_env):
    assert settings_env.app_secret == "test-secret-0123456789-0123456789"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("example.org", "example.org"),
        ("Example.ORG:8443", "example.org"),
        ("example.org.", "example.org"),
        ("127.0.0.1:8000", "127.0.0.1"),
        ("[::1]:8000", "::1"),
        ("[::1]", "::1"),
        ("::1", "::1"),
        ("  localhost  ", "localhost"),
    ],
)
def test_normalize_host(raw, expected):
    assert normalize_host(raw) == expected


# --------------------------------------------------------------------------- host_allowed


def test_host_allowed():
    from app.security import host_allowed

    allowed = frozenset({"time.bauer.center", "localhost"})
    assert host_allowed("time.bauer.center", allowed)
    assert host_allowed("TIME.bauer.center:443", allowed)
    assert host_allowed("localhost:8000", allowed)
    assert not host_allowed("evil.example", allowed)
    assert not host_allowed("time.bauer.center.evil.example", allowed)
    assert not host_allowed("", allowed)
    assert not host_allowed(None, allowed)


# --------------------------------------------------------------------------- client_ip


@pytest.mark.parametrize(
    "peer,headers,expected",
    [
        ("172.19.0.2", {"X-Real-IP": "203.0.113.7"}, "203.0.113.7"),          # NPM in proxy_net
        ("172.31.255.254", {"X-Real-IP": "192.168.10.23"}, "192.168.10.23"),  # edge of 172.16.0.0/12
        ("172.19.0.2", {}, "172.19.0.2"),                                     # trusted, no header
        ("172.19.0.2", {"X-Real-IP": "nonsense"}, "172.19.0.2"),              # garbage header
        ("172.19.0.2", {"X-Real-IP": " 2001:db8::1 "}, "2001:db8::1"),
        ("192.168.178.20", {"X-Real-IP": "203.0.113.7"}, "192.168.178.20"),   # untrusted peer
        ("172.32.0.1", {"X-Real-IP": "203.0.113.7"}, "172.32.0.1"),           # outside /12
        ("10.0.0.5", {"X-Forwarded-For": "203.0.113.7", "CF-Connecting-IP": "203.0.113.8"}, "10.0.0.5"),
        ("172.19.0.2", {"X-Forwarded-For": "203.0.113.7"}, "172.19.0.2"),    # only X-Real-IP counts
        ("testclient", {"X-Real-IP": "203.0.113.7"}, "testclient"),          # non-IP peer
    ],
)
def test_client_ip(settings_env, peer, headers, expected):
    from app.security import client_ip

    request = _request(client=(peer, 1234), headers=headers)
    assert client_ip(request) == expected
    assert client_ip(request.scope) == expected  # also accepts the raw ASGI scope


def test_client_ip_without_peer(settings_env):
    from app.security import client_ip

    assert client_ip(_request(client=None, headers={"X-Real-IP": "203.0.113.7"})) == "unknown"


def test_client_ip_respects_configured_nets(settings_env, monkeypatch):
    from app.security import client_ip

    monkeypatch.setenv("PROXY_TRUSTED_NETS", "192.168.178.0/24")
    reset_config()
    assert client_ip(_request(client=("192.168.178.20", 1), headers={"X-Real-IP": "203.0.113.7"})) == "203.0.113.7"
    assert client_ip(_request(client=("172.19.0.2", 1), headers={"X-Real-IP": "203.0.113.7"})) == "172.19.0.2"


# --------------------------------------------------------------------------- API token helpers


def test_generate_api_token():
    from app.security import generate_api_token, hash_api_token

    token, token_hash = generate_api_token()
    assert len(token) >= 40
    assert token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert hash_api_token(token) == token_hash
    assert re.fullmatch(r"[0-9a-f]{64}", token_hash)
    assert generate_api_token()[0] != token


def test_verify_api_token(db):
    from app.security import generate_api_token, verify_api_token

    token, token_hash = generate_api_token()
    assert verify_api_token(db, token) is False  # nothing configured
    db.get(Settings, 1).api_token_hash = token_hash
    db.commit()
    assert verify_api_token(db, token) is True
    assert verify_api_token(db, token + "x") is False
    assert verify_api_token(db, "") is False
    assert verify_api_token(db, None) is False
    assert verify_api_token(db, token_hash) is False  # the hash itself is not a valid token


def test_verify_api_token_empty_hash_is_not_configured(db):
    from app.security import configured_api_token_hash, verify_api_token

    db.get(Settings, 1).api_token_hash = ""
    db.commit()
    assert configured_api_token_hash(db) is None
    assert verify_api_token(db, "") is False


def test_require_sensor_token_dependency(db):
    from app.security import generate_api_token, require_sensor_token

    with pytest.raises(HTTPException) as exc:
        require_sensor_token(_request(headers={"Authorization": "Bearer x"}), db)
    assert exc.value.status_code == 404

    token, token_hash = generate_api_token()
    db.get(Settings, 1).api_token_hash = token_hash
    db.commit()

    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": token}):
        with pytest.raises(HTTPException) as exc:
            require_sensor_token(_request(headers=headers), db)
        assert exc.value.status_code == 401
        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}

    request = _request(headers={"Authorization": f"Bearer {token}"})
    assert require_sensor_token(request, db) is None
    assert request.state.bearer is True


# --------------------------------------------------------------------------- csrf_protect (unit)


def _csrf_request(method, path="/x", headers=None, session=None):
    request = _request(headers=headers, method=method, path=path)
    request.scope["session"] = {} if session is None else session
    return request


def test_csrf_protect_unit(settings_env, monkeypatch):
    from app.security import csrf_protect

    monkeypatch.setenv("CSRF_ENFORCE", "true")
    reset_config()

    def run(request):
        return asyncio.run(csrf_protect(request))

    for method in ("GET", "HEAD", "OPTIONS"):
        assert run(_csrf_request(method)) is None

    with pytest.raises(HTTPException) as exc:
        run(_csrf_request("POST", session={"csrf_token": "abc"}))
    assert exc.value.status_code == 403

    assert run(_csrf_request("POST", headers={"X-CSRF-Token": "abc"}, session={"csrf_token": "abc"})) is None

    # /healthz and Bearer requests to /api/sensors are exempt; Bearer elsewhere is not.
    assert run(_csrf_request("POST", path="/healthz")) is None
    assert run(_csrf_request("POST", path="/api/sensors", headers={"Authorization": "Bearer t"})) is None
    assert run(_csrf_request("POST", path="/api/sensors/x", headers={"Authorization": "Bearer t"})) is None
    for path in ("/api/sensorsX", "/api/entries"):
        with pytest.raises(HTTPException):
            run(_csrf_request("POST", path=path, headers={"Authorization": "Bearer t"}))

