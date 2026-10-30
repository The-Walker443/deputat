"""Request security: CSRF, Host-header check, security headers, client IP, sensor API token.

There is no login (decision 29.10.2026, DEPLOY.md): external access is protected by
Cloudflare Access, the LAN is deliberately open. Because the browser of every LAN device is attack
surface, the app enforces:

- ``HostCheckMiddleware``: only ``ALLOWED_HOSTS`` (DNS rebinding); other hosts -> 400 plain text.
- ``SecurityHeadersMiddleware``: CSP, X-Frame-Options, nosniff, Referrer-Policy, Permissions-Policy,
  HSTS on every response. No CORS headers anywhere (any ``Access-Control-*`` header is stripped).
- ``csrf_protect`` (global dependency, installed by ``app.main``): every unsafe request carries the
  session's CSRF token as form field ``csrf_token`` or header ``X-CSRF-Token``. The session cookie
  (SessionMiddleware, signed with APP_SECRET) only holds that token and flash messages.
  Exempt: Bearer requests to ``/api/sensors`` and ``/healthz``.
- ``require_sensor_token``: Bearer token for ``/api/sensors`` (404 without configured token, 401 when
  wrong/missing). The token's SHA-256 is stored in ``settings.api_token_hash``.
- ``BodySizeLimitMiddleware``: request bodies are capped per path (Content-Length check plus a streaming
  counter) BEFORE anything parses them – a cross-site ``no-cors`` multipart POST cannot spool large files
  into the tmpfs ``/tmp`` even though its CSRF check fails later.
- ``CsrfPrecheckMiddleware`` (inside the SessionMiddleware): unsafe requests without a session CSRF token
  (SameSite=Lax: cross-site POSTs carry no cookie) or with ``Sec-Fetch-Site: cross-site`` get 403 before
  FastAPI reads the body (FastAPI parses form/File bodies before it runs dependencies such as
  ``csrf_protect``, which then does the full token comparison).

Request headers (X-Real-IP, CF-*, X-Forwarded-*) are spoofable in the LAN: they are NEVER used to
derive trust. ``client_ip()`` uses X-Real-IP for access logging only.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import secrets
import time
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_config, normalize_host
from app.db.models import Settings
from app.db.session import get_db

SESSION_CSRF_KEY = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

SENSORS_PATH = "/api/sensors"
CSRF_EXEMPT_PATHS = frozenset({"/healthz"})

MB = 1024 * 1024
DEFAULT_MAX_BODY_BYTES = 4 * MB
# (path prefix, limit); first match wins. Import files: 10 MB (+ multipart overhead); JSON restore: 64 MB.
BODY_LIMITS: tuple[tuple[str, int], ...] = (
    ("/api/export/restore", 65 * MB),
    ("/export/wiederherstellen", 65 * MB),
    ("/api/import", 11 * MB),
    ("/import", 11 * MB),
)

access_log = logging.getLogger("app.access")

# 'unsafe-inline' for styles only: app.css documents category swatches via style="--c: #hex".
# Scripts stay strictly 'self' (Alpine CSP build, htmx with allowEval=false).
CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "manifest-src 'self'",
        "worker-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CSP.encode()),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
    # Always, without includeSubDomains/preload (ops requirement); browsers ignore it over http.
    (b"strict-transport-security", b"max-age=31536000"),
)


# --------------------------------------------------------------------------- middleware


class SecurityHeadersMiddleware:
    """Pure ASGI middleware adding security headers to every HTTP response (also errors, 400s, static)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if not name.lower().startswith(b"access-control-")  # never any CORS
                ]
                existing = {name.lower() for name, _ in headers}
                headers.extend((k, v) for k, v in SECURITY_HEADERS if k not in existing)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def host_allowed(host_header: str | None, allowed: frozenset[str] | None = None) -> bool:
    """True if the (port-stripped, lower-cased) Host header is in ``allowed`` (default: ALLOWED_HOSTS)."""
    if not host_header:
        return False
    allowed = get_config().allowed_hosts if allowed is None else allowed
    return normalize_host(host_header) in allowed


class HostCheckMiddleware:
    """Reject requests whose Host header is not in ``ALLOWED_HOSTS`` (DNS rebinding protection)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if host_allowed(_scope_header(scope, "host")):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = "Ungültiger Host-Header.".encode()
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class AccessLogMiddleware:
    """One INFO line per HTTP request on logger ``app.access`` (client address via ``client_ip``).

    Logs the path without query string (queries may contain personal data).
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not access_log.isEnabledFor(logging.INFO):
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            access_log.info(
                '%s "%s %s" %d %.1fms',
                client_ip(scope),
                scope.get("method", "-"),
                scope.get("path", "-"),
                status_code,
                (time.perf_counter() - started) * 1000,
            )


# --------------------------------------------------------------------------- body size limit


def body_limit(path: str) -> int:
    for prefix, limit in BODY_LIMITS:
        if path == prefix or path.startswith(prefix + "/"):
            return limit
    return DEFAULT_MAX_BODY_BYTES


class _BodyTooLarge(HTTPException):
    """Raised from ``receive``; an HTTPException so FastAPI's body parsing re-raises it as 413."""

    def __init__(self, limit: int):
        super().__init__(413, f"Die Anfrage ist zu groß (höchstens {limit // MB} MB).")


class BodySizeLimitMiddleware:
    """Reject request bodies above :func:`body_limit` with 413 – via Content-Length up front and via a
    streaming counter for chunked/lying clients, before any form or JSON parser buffers them."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") in SAFE_METHODS:
            await self.app(scope, receive, send)
            return
        limit = body_limit(scope.get("path", ""))
        declared = _scope_header(scope, "content-length")
        if declared is not None:
            try:
                too_large = int(declared) > limit
            except ValueError:
                too_large = False
            if too_large:
                await self._reject(send, limit)
                return

        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge(limit)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await self._reject(send, limit)

    @staticmethod
    async def _reject(send: Send, limit: int) -> None:
        body = ('{"detail":"Die Anfrage ist zu groß (höchstens %d MB)."}' % (limit // MB)).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                        (b"connection", b"close")],
        })
        await send({"type": "http.response.body", "body": body})


# --------------------------------------------------------------------------- CSRF pre-check


def _csrf_exempt(method: str, path: str, has_bearer: bool) -> bool:
    if method in SAFE_METHODS or not get_config().csrf_enforce or path in CSRF_EXEMPT_PATHS:
        return True
    return _is_sensors_path(path) and has_bearer


class CsrfPrecheckMiddleware:
    """403 for unsafe requests that cannot pass ``csrf_protect`` anyway, before the body is read."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            auth = (_scope_header(scope, "authorization") or "").partition(" ")
            has_bearer = auth[0].lower() == "bearer" and bool(auth[2].strip())
            if not _csrf_exempt(scope.get("method", ""), scope.get("path", ""), has_bearer):
                session = scope.get("session") or {}
                cross_site = (_scope_header(scope, "sec-fetch-site") or "").lower() == "cross-site"
                if not session.get(SESSION_CSRF_KEY) or cross_site:
                    body = '{"detail":"CSRF-Prüfung fehlgeschlagen. Bitte Seite neu laden."}'.encode("utf-8")
                    await send({
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())],
                    })
                    await send({"type": "http.response.body", "body": body})
                    return
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------- redirect targets


def safe_redirect_target(value: str | None) -> str | None:
    """A same-site path for ``next=`` parameters, else None.

    Browsers strip ASCII tab/CR/LF from URLs and treat a backslash like ``/``, so ``/<TAB>/evil.example``
    would become the protocol-relative ``//evil.example``: any control character, whitespace or backslash
    is rejected, and the result must parse without scheme and host.
    """
    if not value or not value.startswith("/") or value.startswith("//"):
        return None
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in value) or "\\" in value:
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or parts.path.startswith("//"):
        return None
    return value


# --------------------------------------------------------------------------- client IP


def _scope_header(scope: Scope, name: str) -> str | None:
    raw = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key == raw:
            return value.decode("latin-1")
    return None


def _in_trusted_nets(host: str | None) -> bool:
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in get_config().proxy_trusted_nets)


def client_ip(request: Request | Scope | Any) -> str:
    """Client address for LOGGING ONLY: ``X-Real-IP`` if the direct peer is in ``PROXY_TRUSTED_NETS``
    (the reverse proxy), otherwise the peer address (``"unknown"`` without peer). Accepts a
    ``Request`` or an ASGI scope.

    Never use the result for authorization decisions: headers are spoofable in the LAN.
    """
    scope = request if isinstance(request, dict) else request.scope
    client = scope.get("client")
    peer = client[0] if client else None
    if _in_trusted_nets(peer):
        real_ip = (_scope_header(scope, "x-real-ip") or "").strip()
        if real_ip:
            try:
                return str(ipaddress.ip_address(real_ip))
            except ValueError:
                pass  # garbage header -> fall back to the peer
    return peer or "unknown"


# --------------------------------------------------------------------------- CSRF


def get_csrf_token(request: Request) -> str:
    """Per-session CSRF token (created lazily, stored in the signed session cookie)."""
    token = request.session.get(SESSION_CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF_KEY] = token
    return token


def bearer_token(request: Request) -> str | None:
    """The token of an ``Authorization: Bearer <token>`` header, else None."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def _is_sensors_path(path: str) -> bool:
    return path == SENSORS_PATH or path.startswith(SENSORS_PATH + "/")


async def csrf_protect(request: Request) -> None:
    """Global dependency: validate the CSRF token on unsafe methods.

    The token is accepted from the ``X-CSRF-Token`` header (HTMX via ``hx-headers`` on <body>,
    fetch via ``deputat.fetch``) or the ``csrf_token`` form field. Bearer requests to ``/api/sensors``
    are exempt (a browser cannot attach that header cross-site without CORS, which the app never allows).
    """
    if request.method in SAFE_METHODS or not get_config().csrf_enforce:
        return
    path = request.url.path
    if path in CSRF_EXEMPT_PATHS:
        return
    if _is_sensors_path(path) and bearer_token(request) is not None:
        return

    expected = request.session.get(SESSION_CSRF_KEY)
    # Cheap rejections first, so a cross-site request never makes the app parse (and spool) its body:
    # without a session token there is nothing to compare against (SameSite=Lax: cross-site POSTs carry
    # no cookie), and browsers label cross-site requests themselves.
    if not expected or request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF-Prüfung fehlgeschlagen. Bitte Seite neu laden.")
    provided = request.headers.get(CSRF_HEADER)
    if not provided:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()  # cached by Starlette, the endpoint reuses it
            value = form.get(CSRF_FORM_FIELD)
            provided = value if isinstance(value, str) else None
    if not expected or not provided or not hmac.compare_digest(str(expected), str(provided)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF-Prüfung fehlgeschlagen. Bitte Seite neu laden.")


# --------------------------------------------------------------------------- sensor API token


def hash_api_token(token: str) -> str:
    """API tokens are random 256-bit values, so a plain SHA-256 is sufficient."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_api_token() -> tuple[str, str]:
    """Return ``(token, sha256hex)``; show the token once, store only the hash in ``settings.api_token_hash``."""
    token = secrets.token_urlsafe(32)
    return token, hash_api_token(token)


def configured_api_token_hash(db: Session) -> str | None:
    """``settings.api_token_hash`` or None when no sensor token is configured."""
    settings = db.get(Settings, 1)
    return (settings.api_token_hash or None) if settings is not None else None


def verify_api_token(db: Session, token: str | None) -> bool:
    """Constant-time comparison of ``token`` against ``settings.api_token_hash``.

    False when no token is configured or ``token`` is empty.
    """
    expected = configured_api_token_hash(db)
    if not expected or not token:
        return False
    return hmac.compare_digest(hash_api_token(token), expected)


def require_sensor_token(request: Request, db: Annotated[Session, Depends(get_db)]) -> None:
    """Dependency for ``/api/sensors``: 404 when no token is configured (the endpoint is never open),
    401 when the Bearer token is missing or wrong."""
    if not configured_api_token_hash(db):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    if not verify_api_token(db, bearer_token(request)):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Ungültiges oder fehlendes API-Token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.bearer = True
