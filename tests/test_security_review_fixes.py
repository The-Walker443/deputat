"""Review fixes in app/security.py and app/main.py: request body limits, early CSRF rejection, German error
bodies, no Swagger UI, out-of-range dates as 422."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.config import reset_config
from app.security import DEFAULT_MAX_BODY_BYTES, MB, body_limit


@pytest.fixture
def csrf_client(settings_env, db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    @contextmanager
    def factory():
        monkeypatch.setenv("CSRF_ENFORCE", "true")
        reset_config()
        with TestClient(create_app()) as client:
            yield client

    return factory


def _token(client) -> str:
    return client.get("/api/csrf").json()["csrf_token"]


# ------------------------------------------------------------------ body size


def test_body_limits_per_path():
    assert body_limit("/api/export/restore") == 65 * MB
    assert body_limit("/api/import/preview") == 11 * MB
    assert body_limit("/import") == 11 * MB
    assert body_limit("/importx") == DEFAULT_MAX_BODY_BYTES
    assert body_limit("/api/entries") == DEFAULT_MAX_BODY_BYTES


def test_declared_oversize_body_is_rejected_before_parsing(client, monkeypatch):
    import starlette.requests

    async def boom(*_a, **_k):  # pragma: no cover - must never be reached
        raise AssertionError("form parsed")

    monkeypatch.setattr(starlette.requests.Request, "form", boom)
    r = client.post("/api/import/preview", content=b"x" * (11 * MB + 1),
                    headers={"content-type": "multipart/form-data; boundary=abc"})
    assert r.status_code == 413
    assert r.json()["detail"].startswith("Die Anfrage ist zu groß")


def test_streamed_oversize_body_without_length_is_rejected(client):
    def chunks():
        for _ in range(5):
            yield b"x" * MB

    r = client.post("/api/entries", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_normal_requests_pass_the_limit(client):
    r = client.post("/api/entries", json={"category_id": 1, "day": "2026-10-01", "duration_seconds": 60})
    assert r.status_code == 201


# ------------------------------------------------------------------ CSRF before parsing


def test_cross_site_multipart_without_session_is_rejected_without_parsing(csrf_client, monkeypatch):
    import starlette.requests

    async def boom(*_a, **_k):  # pragma: no cover
        raise AssertionError("form parsed")

    with csrf_client() as client:
        monkeypatch.setattr(starlette.requests.Request, "form", boom)
        r = client.post("/api/import/preview", files={"file": ("a.csv", b"x" * 1000)})
        assert r.status_code == 403


def test_sec_fetch_site_cross_site_is_rejected_even_with_token(csrf_client):
    with csrf_client() as client:
        token = _token(client)
        body = {"category_id": 1, "day": "2026-10-01", "duration_seconds": 60}
        r = client.post("/api/entries", json=body, headers={"X-CSRF-Token": token, "Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
        r = client.post("/api/entries", json=body, headers={"X-CSRF-Token": token, "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 201


# ------------------------------------------------------------------ German errors


def test_request_validation_error_is_german_string(client):
    r = client.post("/api/entries", json={"category_id": 1, "start_at": "nope"})
    assert r.status_code == 422
    body = r.json()
    assert isinstance(body["detail"], str) and body["detail"].startswith("Ungültige Eingabe")
    assert "start_at" in body["detail"] and isinstance(body["errors"], list)
    r = client.post("/api/timetable", json={})
    assert r.status_code == 422 and "fehlt" in r.json()["detail"]
    r = client.get("/api/entries", params={"limit": 0})
    assert r.status_code == 422 and "zu klein" in r.json()["detail"]


def test_unknown_route_and_method_are_german(client):
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404 and r.json()["detail"] == "Nicht gefunden."
    r = client.put("/api/csrf")
    assert r.status_code == 405 and r.json()["detail"] == "Diese Methode ist hier nicht erlaubt."


def test_swagger_ui_disabled_but_openapi_available(client):
    assert client.get("/api/docs").status_code == 404
    assert client.get("/api/openapi.json").status_code == 200


# ------------------------------------------------------------------ out-of-range dates


def test_extreme_dates_never_give_500(client):
    period = client.post("/api/calendar/periods", json={"label": "2026/27"}).json()
    assert client.get("/api/months/9999-12").status_code == 422
    r = client.get("/api/account/summary", params={"stichtag": "0001-01-01", "period": period["id"]})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    assert client.post("/api/calendar/periods", json={"label": "9999/00"}).status_code == 422
    r = client.post("/api/timer/start", json={"category_id": 1, "started_at": "0001-01-01T00:00:00"})
    assert r.status_code == 400 and "2000" in r.json()["detail"]


# ------------------------------------------------------------------ redirect targets


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/eintraege?tag=2026-10-01", "/eintraege?tag=2026-10-01"),
        ("/", "/"),
        ("/\t/evil.example/x", None),
        ("/\n/evil.example", None),
        ("//evil.example", None),
        ("/\\evil.example", None),
        ("https://evil.example", None),
        ("/ x", None),
        ("", None),
        (None, None),
    ],
)
def test_safe_redirect_target(value, expected):
    from app.security import safe_redirect_target

    assert safe_redirect_target(value) == expected
