"""HTML – app-shell pages owned by the scaffold (not by a feature step):

- ``GET /``        -> redirect to /heute
- ``GET /mehr``    -> menu of secondary screens (tab "Mehr")
- ``GET /offline`` -> offline fallback page cached by the service worker

Mounted by app.main without prefix (no login, see DEPLOY.md); ``/offline`` lives on
``public_router`` so the service worker can always precache it.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.web.templating import templates

router = APIRouter()
public_router = APIRouter()


@router.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse("/heute", status_code=303)


@router.get("/mehr", response_class=HTMLResponse)
def more(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "mehr.html", {"page_title": "Mehr"})


@public_router.get("/offline", response_class=HTMLResponse, include_in_schema=False)
def offline(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "offline.html", {"page_title": "Offline", "hide_nav": False})
