"""Security behaviour through the HTTP stack: no login, security headers, Host check, CSRF, no CORS,
/healthz, /api/csrf, sensor Bearer token, session cookie flags, access log."""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager

import pytest
from fastapi import Depends, Request

from app.config import reset_config
from app.db.models import Settings

SECURITY_ENV = ("ALLOWED_HOSTS", "PROXY_TRUSTED_NETS", "BASE_URL", "CSRF_ENFORCE", "COOKIE_SECURE")


@pytest.fixture
def make_client(settings_env, db, monkeypatch):
    """Factory: ``with make_client(BASE_URL=..., ...) as client`` -> TestClient on a fresh app.

    CSRF is enforced unless ``CSRF_ENFORCE`` is passed. The app gets test-only probe routes:
    ``POST /_probe`` (global CSRF only) and ``GET|POST /api/sensors/_probe`` (``require_sensor_token``).
    Extra keyword ``client_addr`` sets the TCP peer, ``base_url`` the TestClient base URL.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.security import require_sensor_token

    @contextmanager
    def factory(client_addr=("testclient", 50000), base_url="http://testserver", **env):
        for var in SECURITY_ENV:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("CSRF_ENFORCE", "true")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        reset_config()
        app = create_app()

        def probe(request: Request) -> dict:
            return {"ok": True, "bearer": getattr(request.state, "bearer", False)}

        app.add_api_route("/_probe", probe, methods=["POST", "PUT", "PATCH", "DELETE"])
        app.add_api_route(
            "/api/sensors/_probe", probe, methods=["GET", "POST"], dependencies=[Depends(require_sensor_token)]
        )
        with TestClient(app, client=client_addr, base_url=base_url) as test_client:
            yield test_client

    return factory


@pytest.fixture
def sensor_token(db):
    from app.security import generate_api_token

    token, token_hash = generate_api_token()
    db.get(Settings, 1).api_token_hash = token_hash
    db.commit()
    return token


def _meta_csrf(html: str) -> str:
    match = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert match, "csrf meta tag missing"
    return match.group(1)


# --------------------------------------------------------------------------- no login


def test_pages_and_api_open_without_login(make_client):
    with make_client() as client:
        res = client.get("/heute", follow_redirects=False)
        assert res.status_code == 200
        assert client.get("/mehr").status_code == 200
        assert client.get("/", follow_redirects=False).headers["location"] == "/heute"


@pytest.mark.parametrize("path", ["/login", "/setup", "/api/auth/status"])
def test_login_endpoints_removed(make_client, path):
    with make_client() as client:
        assert client.get(path, follow_redirects=False).status_code == 404


def test_logout_post_is_gone(make_client):
    with make_client(CSRF_ENFORCE="false") as client:
        assert client.post("/logout", follow_redirects=False).status_code in (404, 405)


# --------------------------------------------------------------------------- security headers


def _assert_security_headers(headers) -> None:
    csp = headers["content-security-policy"]
    for directive in (
        "default-src 'self'",
        "script-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ):
        assert directive in csp
    assert re.search(r"style-src 'self'( 'unsafe-inline')?(;|$)", csp)
    script_src = re.search(r"script-src ([^;]*)", csp).group(1)
    assert script_src.strip() == "'self'", "scripts must never allow inline/eval"
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "same-origin"
    pp = headers["permissions-policy"]
    for feature in ("camera=()", "microphone=()", "geolocation=()"):
        assert feature in pp
    assert headers["strict-transport-security"] == "max-age=31536000"


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/heute", 200),
        ("/healthz", 200),
        ("/static/css/app.css", 200),
        ("/sw.js", 200),
        ("/api/csrf", 200),
        ("/gibt-es-nicht", 404),
        ("/api/gibt-es-nicht", 404),
    ],
)
def test_security_headers_on_every_response(make_client, path, expected):
    with make_client() as client:
        res = client.get(path)
        assert res.status_code == expected
        _assert_security_headers(res.headers)


def test_security_headers_on_csrf_reject_and_host_reject(make_client):
    with make_client() as client:
        res = client.post("/_probe")
        assert res.status_code == 403
        _assert_security_headers(res.headers)
        res = client.get("/heute", headers={"Host": "evil.example"})
        assert res.status_code == 400
        _assert_security_headers(res.headers)


def test_hsts_also_on_http_base_url(make_client):
    with make_client(BASE_URL="http://localhost:8000") as client:
        hsts = client.get("/healthz").headers["strict-transport-security"]
        assert hsts == "max-age=31536000"
        assert "includesubdomains" not in hsts.lower()


# --------------------------------------------------------------------------- no CORS


def test_no_cors_headers_on_preflight_or_cross_origin_requests(make_client):
    with make_client() as client:
        preflight = client.options(
            "/api/csrf",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-CSRF-Token",
            },
        )
        cross = client.get("/api/csrf", headers={"Origin": "https://evil.example"})
        page = client.get("/heute", headers={"Origin": "https://evil.example"})
        for res in (preflight, cross, page):
            assert not [h for h in res.headers if h.lower().startswith("access-control-")]


def test_security_headers_strip_cors_headers_from_endpoints(make_client):
    with make_client() as client:
        from fastapi.responses import JSONResponse

        client.app.add_api_route(
            "/_cors_leak",
            lambda: JSONResponse({}, headers={"Access-Control-Allow-Origin": "*"}),
            methods=["GET"],
        )
        res = client.get("/_cors_leak")
        assert res.status_code == 200
        assert "access-control-allow-origin" not in res.headers


# --------------------------------------------------------------------------- Host check


@pytest.mark.parametrize(
    "host", ["testserver", "localhost", "localhost:8000", "127.0.0.1", "127.0.0.1:8000", "[::1]:8000", "LOCALHOST"]
)
def test_default_allowed_hosts(make_client, host):
    with make_client() as client:
        assert client.get("/healthz", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize(
    "host", ["evil.example", "time.bauer.center.evil.example", "localhost.evil.example", "192.168.1.20:8000", ""]
)
def test_unknown_hosts_rejected(make_client, host):
    with make_client() as client:
        res = client.get("/heute", headers={"Host": host})
        assert res.status_code == 400
        assert res.headers["content-type"].startswith("text/plain")
        assert "Host" in res.text
        assert "set-cookie" not in res.headers


def test_host_check_blocks_unsafe_requests_too(make_client):
    with make_client(CSRF_ENFORCE="false") as client:
        assert client.post("/_probe", headers={"Host": "evil.example"}).status_code == 400
        assert client.post("/_probe").status_code == 200


def test_base_url_host_is_allowed_by_default(make_client):
    with make_client(BASE_URL="https://time.bauer.center") as client:
        assert client.get("/healthz", headers={"Host": "time.bauer.center"}).status_code == 200
        assert client.get("/healthz", headers={"Host": "Time.Bauer.Center:443"}).status_code == 200
        assert client.get("/healthz", headers={"Host": "testserver"}).status_code == 200
        assert client.get("/healthz", headers={"Host": "other.bauer.center"}).status_code == 400


def test_explicit_allowed_hosts_replace_default_but_keep_loopback(make_client):
    with make_client(
        BASE_URL="https://time.bauer.center",
        ALLOWED_HOSTS="time.bauer.center, deputat",
        base_url="http://deputat:8000",
    ) as client:
        assert client.get("/healthz").status_code == 200  # Host: deputat:8000
        assert client.get("/healthz", headers={"Host": "time.bauer.center"}).status_code == 200
        # testserver is only part of the default list
        assert client.get("/healthz", headers={"Host": "testserver"}).status_code == 400


def test_healthz_from_container_loopback_even_with_restricted_hosts(make_client):
    with make_client(
        ALLOWED_HOSTS="time.bauer.center", client_addr=("127.0.0.1", 40000), base_url="http://127.0.0.1:8000"
    ) as client:
        res = client.get("/healthz")
        assert res.status_code == 200
        assert res.json() == {"status": "ok"}


# --------------------------------------------------------------------------- CSRF


def test_csrf_missing_or_wrong_token_rejected(make_client):
    with make_client() as client:
        client.get("/heute")  # session with token exists
        for kwargs in (
            {},
            {"headers": {"X-CSRF-Token": "falsch"}},
            {"data": {"csrf_token": "falsch"}},
            {"json": {"csrf_token": "falsch"}},
        ):
            res = client.post("/_probe", **kwargs)
            assert res.status_code == 403, kwargs
            assert "CSRF" in res.json()["detail"]


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_csrf_applies_to_all_unsafe_methods(make_client, method):
    with make_client() as client:
        token = client.get("/api/csrf").json()["csrf_token"]
        assert getattr(client, method)("/_probe").status_code == 403
        assert getattr(client, method)("/_probe", headers={"X-CSRF-Token": token}).status_code == 200


def test_csrf_rejected_without_session(make_client):
    with make_client() as client:
        res = client.post("/_probe", headers={"X-CSRF-Token": "irgendwas"})
        assert res.status_code == 403


def test_csrf_header_token_accepted(make_client):
    with make_client() as client:
        token = client.get("/api/csrf").json()["csrf_token"]
        res = client.post("/_probe", headers={"X-CSRF-Token": token})
        assert res.status_code == 200
        assert res.json() == {"ok": True, "bearer": False}


def test_csrf_form_field_accepted(make_client):
    with make_client() as client:
        token = _meta_csrf(client.get("/heute").text)
        assert client.post("/_probe", data={"csrf_token": token}).status_code == 200
        assert client.post("/_probe", data={"csrf_token": token}, files={"f": ("a.txt", b"x")}).status_code == 200


def test_csrf_token_stable_and_shared_between_meta_hx_headers_and_api(make_client):
    with make_client() as client:
        html = client.get("/heute").text
        meta = _meta_csrf(html)
        assert f'"X-CSRF-Token": "{meta}"' in html.replace("&#34;", '"')
        assert client.get("/api/csrf").json()["csrf_token"] == meta
        assert client.get("/api/csrf").headers["cache-control"] == "no-store"
        assert _meta_csrf(client.get("/mehr").text) == meta


def test_csrf_token_of_other_session_rejected(make_client):
    with make_client() as client:
        token_a = client.get("/api/csrf").json()["csrf_token"]
        client.cookies.clear()
        client.get("/api/csrf")  # new session, new token
        assert client.post("/_probe", headers={"X-CSRF-Token": token_a}).status_code == 403


def test_csrf_tampered_session_cookie_rejected(make_client):
    with make_client() as client:
        token = client.get("/api/csrf").json()["csrf_token"]
        cookie = client.cookies.get("deputat_session")
        client.cookies.set("deputat_session", cookie[:-3] + ("AAA" if not cookie.endswith("AAA") else "BBB"))
        assert client.post("/_probe", headers={"X-CSRF-Token": token}).status_code == 403


def test_csrf_not_enforced_when_disabled(make_client):
    with make_client(CSRF_ENFORCE="false") as client:
        assert client.post("/_probe").status_code == 200


def test_bearer_header_does_not_exempt_other_api_paths(make_client, sensor_token):
    with make_client() as client:
        client.app.add_api_route("/api/_other", lambda: {"ok": True}, methods=["POST"])
        res = client.post("/api/_other", headers={"Authorization": f"Bearer {sensor_token}"})
        assert res.status_code == 403


# --------------------------------------------------------------------------- session cookie


def test_session_cookie_flags_http(make_client):
    with make_client(BASE_URL="http://localhost:8000") as client:
        cookie = client.get("/api/csrf").headers["set-cookie"].lower()
        assert cookie.startswith("deputat_session=")
        assert "httponly" in cookie
        assert "samesite=lax" in cookie
        assert "secure" not in cookie


def test_session_cookie_not_secure_by_default_even_with_https_base_url(make_client):
    """COOKIE_SECURE defaults to false: LAN clients over plain http must still get the CSRF cookie."""
    with make_client(BASE_URL="https://time.bauer.center") as client:
        cookie = client.get("/api/csrf").headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=lax" in cookie
        assert "secure" not in cookie


@pytest.mark.parametrize(
    ("base_url", "mode", "secure"),
    [
        ("https://time.bauer.center", "auto", True),
        ("http://localhost:8000", "auto", False),
        ("http://localhost:8000", "true", True),
        ("https://time.bauer.center", "false", False),
    ],
)
def test_session_cookie_secure_setting(make_client, base_url, mode, secure):
    with make_client(BASE_URL=base_url, COOKIE_SECURE=mode) as client:
        cookie = client.get("/api/csrf").headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie
        assert ("secure" in cookie) is secure


def test_form_post_over_http_works_with_https_base_url(make_client):
    """LAN over plain http (BASE_URL https): the cookie arrives, so the CSRF-protected POST succeeds."""
    with make_client(BASE_URL="https://time.bauer.center", ALLOWED_HOSTS="time.bauer.center,testserver") as client:
        token = client.get("/api/csrf").json()["csrf_token"]
        assert client.cookies.get("deputat_session")
        assert client.post("/_probe", headers={"X-CSRF-Token": token}).status_code == 200


# --------------------------------------------------------------------------- /healthz


def test_healthz_ok_and_head(make_client):
    with make_client() as client:
        res = client.get("/healthz")
        assert res.status_code == 200
        assert res.json() == {"status": "ok"}
        assert res.headers["cache-control"] == "no-store"
        assert "set-cookie" not in res.headers
        assert client.head("/healthz").status_code == 200


def test_healthz_503_when_db_query_fails(make_client, monkeypatch):
    import app.main as main

    def broken_engine():
        raise RuntimeError("database is locked")

    with make_client() as client:
        monkeypatch.setattr(main, "get_engine", broken_engine)
        res = client.get("/healthz")
        assert res.status_code == 503
        assert res.json() == {"status": "error"}


def test_healthz_503_when_schema_missing(make_client):
    from sqlalchemy import text

    from app.db.session import get_engine

    with make_client() as client:
        with get_engine().begin() as conn:
            conn.execute(text("ALTER TABLE settings RENAME TO settings_gone"))
        try:
            assert client.get("/healthz").status_code == 503
        finally:
            with get_engine().begin() as conn:
                conn.execute(text("ALTER TABLE settings_gone RENAME TO settings"))
        assert client.get("/healthz").status_code == 200


# --------------------------------------------------------------------------- sensor token


def test_sensor_endpoint_404_without_configured_token(make_client):
    with make_client() as client:
        assert client.get("/api/sensors/_probe").status_code == 404
        res = client.get("/api/sensors/_probe", headers={"Authorization": "Bearer irgendwas"})
        assert res.status_code == 404


def test_sensor_endpoint_401_for_missing_or_wrong_token(make_client, sensor_token):
    with make_client() as client:
        for headers in (
            {},
            {"Authorization": "Bearer falsch"},
            {"Authorization": f"Basic {sensor_token}"},
            {"Authorization": "Bearer "},
            {"Authorization": f"Bearer {sensor_token}x"},
        ):
            res = client.get("/api/sensors/_probe", headers=headers)
            assert res.status_code == 401, headers
            assert res.headers["www-authenticate"] == "Bearer"


def test_sensor_endpoint_ok_with_token(make_client, sensor_token):
    with make_client() as client:
        res = client.get("/api/sensors/_probe", headers={"Authorization": f"Bearer {sensor_token}"})
        assert res.status_code == 200
        assert res.json() == {"ok": True, "bearer": True}
        # Scheme is case-insensitive.
        assert client.get("/api/sensors/_probe", headers={"Authorization": f"bearer {sensor_token}"}).status_code == 200


def test_sensor_bearer_requests_are_csrf_exempt(make_client, sensor_token):
    with make_client() as client:
        res = client.post("/api/sensors/_probe", headers={"Authorization": f"Bearer {sensor_token}"})
        assert res.status_code == 200
        # Without Bearer the CSRF check applies first.
        assert client.post("/api/sensors/_probe").status_code == 403
        # Wrong Bearer: CSRF-exempt, but the token dependency rejects it.
        assert client.post("/api/sensors/_probe", headers={"Authorization": "Bearer falsch"}).status_code == 401


def test_sensors_router_is_mounted_with_token_dependency(make_client):
    from app.main import API_DEPENDENCIES
    from app.security import require_sensor_token

    assert [d.dependency for d in API_DEPENDENCIES["sensors"]] == [require_sensor_token]
    with make_client() as client:
        for route in client.app.routes:
            path = getattr(route, "path", "")
            if path == "/api/sensors/_probe":
                continue
            if path.startswith("/api/sensors"):
                calls = [dep.call for dep in route.dependant.dependencies]
                assert require_sensor_token in calls, path
            elif path.startswith("/api/") and hasattr(route, "dependant"):
                calls = [dep.call for dep in route.dependant.dependencies]
                assert require_sensor_token not in calls, path


# --------------------------------------------------------------------------- access log


def test_access_log_uses_x_real_ip_from_trusted_proxy(make_client, caplog):
    with make_client(client_addr=("172.19.0.2", 51000)) as client:
        with caplog.at_level(logging.INFO, logger="app.access"):
            client.get("/healthz?secret=1", headers={"X-Real-IP": "203.0.113.7"})
    lines = [r.getMessage() for r in caplog.records if r.name == "app.access"]
    assert any(line.startswith('203.0.113.7 "GET /healthz" 200') for line in lines), lines
    assert not any("secret" in line for line in lines)


def test_access_log_ignores_x_real_ip_from_untrusted_peer(make_client, caplog):
    with make_client(client_addr=("192.168.178.20", 51000)) as client:
        with caplog.at_level(logging.INFO, logger="app.access"):
            client.get("/healthz", headers={"X-Real-IP": "203.0.113.7"})
            client.get("/heute", headers={"Host": "evil.example"})
    lines = [r.getMessage() for r in caplog.records if r.name == "app.access"]
    assert any(line.startswith('192.168.178.20 "GET /healthz" 200') for line in lines), lines
    assert any(line.startswith('192.168.178.20 "GET /heute" 400') for line in lines), lines
    assert not any("203.0.113.7" in line for line in lines)
