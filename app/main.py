"""FastAPI application factory.

``create_app()`` wires middleware (access log, security headers, Host check, CSRF session), the global
CSRF dependency, exception handlers, static files, PWA files and ALL routers. There is no login
(see DEPLOY.md and ``app.security``):

- ``app.api.<module>.router``  -> prefix ``/api``, no auth dependency;
  ``app.api.sensors`` additionally gets ``require_sensor_token`` (Bearer token, mandatory)
- ``app.web.<module>.router``  -> no prefix, no auth dependency (plus ``app.web.pages.public_router``)

App-level endpoints: ``GET|HEAD /healthz`` (no auth, CSRF-exempt, 200 only when the DB answers),
``GET /api/csrf`` (the session's CSRF token for JS/PWA), ``/sw.js`` (with injected asset hashes, see
``app.web.assets``), ``/manifest.webmanifest``,
``/favicon.ico``. ``/api/openapi.json`` stays available; the Swagger UI is disabled because it loads its
assets from a CDN, which the CSP (``script-src 'self'``) blocks and which would leak requests.

Errors are always ``{"detail": "<German text>"}``: request validation (422) is translated by
:func:`validation_message` (the raw pydantic list is kept in ``errors``), default Starlette texts
(404/405/…) are translated, and out-of-range dates (``OverflowError``) become 422 instead of 500.

Middleware order (outermost first): AccessLog -> SecurityHeaders -> HostCheck -> BodySizeLimit -> Session
-> CsrfPrecheck.

Startup (lifespan): Alembic upgrade head, seed, optional backup scheduler
(``app.services.backup.start_scheduler(app)`` / ``stop_scheduler(app)`` if present).
"""

from __future__ import annotations

import importlib
import logging
from contextlib import asynccontextmanager

from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.config import APP_VERSION, PROJECT_ROOT, get_config
from app.db.seed import seed
from app.db.session import SessionLocal, get_engine, init_db
from app.web import assets
from app.security import (
    AccessLogMiddleware,
    BodySizeLimitMiddleware,
    CsrfPrecheckMiddleware,
    HostCheckMiddleware,
    SecurityHeadersMiddleware,
    csrf_protect,
    get_csrf_token,
    require_sensor_token,
)

log = logging.getLogger("app")

STATIC_DIR = PROJECT_ROOT / "app" / "static"

API_MODULES = (
    "timer", "entries", "days", "calendar", "account", "timetable", "months",
    "export", "import_", "documents", "settings", "sensors", "backup",
)
WEB_MODULES = (
    "pages", "today", "week", "account", "year", "entries", "calendar", "timetable",
    "months", "import_", "documents", "settings", "days",
)

# API routers with an extra dependency; all others are mounted without one (no login).
API_DEPENDENCIES = {"sensors": [Depends(require_sensor_token)]}


def _start_backup_scheduler(app: FastAPI) -> None:
    try:
        backup = importlib.import_module("app.services.backup")
    except ImportError:
        return
    start = getattr(backup, "start_scheduler", None)
    if callable(start):
        try:
            start(app)
        except Exception:  # never block startup because of the scheduler
            log.exception("Backup scheduler could not be started")


def _stop_backup_scheduler(app: FastAPI) -> None:
    try:
        backup = importlib.import_module("app.services.backup")
    except ImportError:
        return
    stop = getattr(backup, "stop_scheduler", None)
    if callable(stop):
        try:
            stop(app)
        except Exception:
            log.exception("Backup scheduler could not be stopped")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with SessionLocal() as db:
        seed(db)
    _start_backup_scheduler(app)
    log.info("Deputat %s started (BASE_URL=%s)", APP_VERSION, get_config().base_url)
    yield
    _stop_backup_scheduler(app)


# German texts for Starlette's default status phrases (e.g. unknown route / wrong method).
GERMAN_STATUS_TEXT = {
    "Not Found": "Nicht gefunden.",
    "Method Not Allowed": "Diese Methode ist hier nicht erlaubt.",
    "Bad Request": "Ungültige Anfrage.",
    "Unauthorized": "Nicht autorisiert.",
    "Forbidden": "Zugriff verweigert.",
    "Request Entity Too Large": "Die Anfrage ist zu groß.",
    "Content Too Large": "Die Anfrage ist zu groß.",
    "Unsupported Media Type": "Nicht unterstütztes Format.",
    "Internal Server Error": "Interner Fehler.",
    "There was an error parsing the body": "Die Anfrage konnte nicht gelesen werden.",
}

_VALIDATION_TEXT = {
    "missing": "fehlt",
    "extra_forbidden": "ist nicht erlaubt",
    "json_invalid": "ist kein gültiges JSON",
    "greater_than": "ist zu klein",
    "greater_than_equal": "ist zu klein",
    "less_than": "ist zu groß",
    "less_than_equal": "ist zu groß",
    "string_too_long": "ist zu lang",
    "string_too_short": "ist zu kurz",
    "too_long": "hat zu viele Einträge",
    "enum": "hat einen unbekannten Wert",
    "literal_error": "hat einen unbekannten Wert",
}


def validation_message(errors: list[dict[str, Any]]) -> str:
    """One German sentence for pydantic/FastAPI validation errors (at most three fields named)."""
    parts = []
    for err in errors[:3]:
        loc = [str(x) for x in err.get("loc", ()) if x not in ("body", "query", "path", "header", "cookie")]
        field = ".".join(loc) or "Eingabe"
        kind = str(err.get("type", ""))
        text = _VALIDATION_TEXT.get(kind)
        if text is None:
            if kind.startswith(("date", "datetime", "time")):
                text = "ist kein gültiges Datum/keine gültige Uhrzeit"
            elif kind.startswith(("int", "float", "decimal")):
                text = "ist keine gültige Zahl"
            elif kind.startswith("bool"):
                text = "ist kein gültiger Wahrheitswert"
            else:
                text = "ist ungültig"
        parts.append(f"„{field}“ {text}")
    more = f" (und {len(errors) - 3} weitere)" if len(errors) > 3 else ""
    return "Ungültige Eingabe: " + "; ".join(parts) + more + "."


def _wants_html(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return False
    return "text/html" in request.headers.get("accept", "")


def _include_routers(app: FastAPI) -> None:
    from app.web import pages

    for name in API_MODULES:
        module = importlib.import_module(f"app.api.{name}")
        app.include_router(module.router, prefix="/api", dependencies=API_DEPENDENCIES.get(name, []))

    app.include_router(pages.public_router)
    for name in WEB_MODULES:
        module = importlib.import_module(f"app.web.{name}")
        app.include_router(module.router)


def _configure_logging(level: str) -> None:
    """Give the ``app`` logger (incl. ``app.access``) a handler when nothing else configured logging."""
    logger = logging.getLogger("app")
    logger.setLevel(level.upper())
    if not logger.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)


def create_app() -> FastAPI:
    cfg = get_config()
    _configure_logging(cfg.log_level)
    assets.asset_version()  # hash all static files once at startup (static_url, /sw.js)

    app = FastAPI(
        title="Deputat",
        version=APP_VERSION,
        lifespan=lifespan,
        dependencies=[Depends(csrf_protect)],
        docs_url=None,  # Swagger UI loads CDN assets, blocked by the CSP
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    # Middleware: the last added runs outermost. CsrfPrecheck runs inside the session (needs scope["session"]).
    # The session cookie only carries the CSRF token and flash messages (HttpOnly, SameSite=Lax; Secure per
    # COOKIE_SECURE, default off so plain-http LAN clients can still post forms).
    app.add_middleware(CsrfPrecheckMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.app_secret,
        session_cookie="deputat_session",
        max_age=cfg.session_max_age,
        same_site="lax",
        https_only=cfg.secure_cookies,
    )
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(HostCheckMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(AccessLogMiddleware)

    def _html_error(request: Request, status_code: int, detail: str, headers: Any = None) -> Response:
        from app.web.templating import templates

        return templates.TemplateResponse(
            request,
            "error.html",
            {"status_code": status_code, "detail": detail, "page_title": "Fehler"},
            status_code=status_code,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> Response:
        errors = list(exc.errors())
        message = validation_message(errors)
        if _wants_html(request) and not request.headers.get("hx-request"):
            return _html_error(request, 422, message)
        safe = [{k: v for k, v in e.items() if k in ("type", "loc", "msg")} for e in errors]
        return JSONResponse({"detail": message, "errors": jsonable_encoder(safe)}, status_code=422)

    @app.exception_handler(OverflowError)
    async def overflow_error_handler(request: Request, exc: OverflowError) -> Response:
        message = "Datum oder Zahl außerhalb des zulässigen Bereichs."
        if _wants_html(request) and not request.headers.get("hx-request"):
            return _html_error(request, 422, message)
        return JSONResponse({"detail": message}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> Response:
        if isinstance(exc.detail, str) and exc.detail in GERMAN_STATUS_TEXT:
            exc = StarletteHTTPException(exc.status_code, GERMAN_STATUS_TEXT[exc.detail], getattr(exc, "headers", None))
        if _wants_html(request) and not request.headers.get("hx-request"):
            from app.web.templating import templates

            return templates.TemplateResponse(
                request,
                "error.html",
                {"status_code": exc.status_code, "detail": exc.detail, "page_title": "Fehler"},
                status_code=exc.status_code,
                headers=getattr(exc, "headers", None),
            )
        return await http_exception_handler(request, exc)

    @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
    def healthz() -> JSONResponse:
        no_store = {"Cache-Control": "no-store"}
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1 FROM settings LIMIT 1"))
        except Exception as exc:  # report, don't raise: the healthcheck must answer
            log.warning("Health check failed: %s", exc)
            return JSONResponse({"status": "error"}, status_code=503, headers=no_store)
        return JSONResponse({"status": "ok"}, headers=no_store)

    @app.get("/api/csrf", tags=["csrf"])
    def csrf_token(request: Request) -> JSONResponse:
        """The session's CSRF token (send it as ``X-CSRF-Token`` on unsafe requests)."""
        return JSONResponse({"csrf_token": get_csrf_token(request)}, headers={"Cache-Control": "no-store"})

    # PWA files must be served from the root so the service worker controls the whole app. The worker's
    # cache name and precache URLs carry the static asset hashes (app.web.assets).
    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> Response:
        return Response(
            assets.service_worker_source(),
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "icons" / "icon-192.png", media_type="image/png")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    _include_routers(app)
    return app


app = create_app()
