"""JSON API – Settings: GET/PATCH /settings, sensor API token create/rotate (POST) and revoke (DELETE).

Mounted by app.main under the ``/api`` prefix. Unsafe methods need the CSRF header.
There is no login (DEPLOY.md); the token only protects ``/api/sensors``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict

from app.api.calendar import DB, service_errors
from app.services import settings_service as svc

router = APIRouter(tags=["settings"])


class SettingsPatch(BaseModel):
    """Partial update; only the fields sent are changed (``null`` clears nullable fields)."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    schule: str | None = None
    bundesland: str | None = None
    schulart: str | None = None
    upz_voll: int | None = None
    upz_ist: float | None = None
    anrechnungsstunden: float | None = None
    anrechnungs_grund: str | None = None
    altersermaessigung: float | None = None
    altersermaessigung_manuell: bool | None = None
    geburtsdatum: date | None = None
    wochen_soll_voll_h: float | None = None
    urlaubstage: int | None = None
    unterrichtsstunde_min: int | None = None
    kontoperiode: str | None = None
    periode_start_md: str | None = None
    urlaubs_modus: str | None = None
    grosse_pause_zaehlt: bool | None = None
    krank_neutralisiert: bool | None = None
    uebertrag_verrechnen: bool | None = None
    regionale_feiertage: list[str] | None = None
    klassen: list[str] | None = None
    faecher: list[str] | None = None
    zeitraster: list[dict[str, Any]] | None = None
    klassenfahrt_variante: str | None = None
    klassenfahrt_cap_h: float | None = None
    prognose_schultage: int | None = None
    ferienarbeit_schaetzung: dict[str, float | None] | None = None
    ui_default_location_rule: dict[str, Any] | None = None
    erfassung_ab: date | None = None
    reason: str | None = None  # required when the change alters a closed month (otherwise 423)


class TokenOut(BaseModel):
    token: str
    hinweis: str


def _out(db) -> dict[str, Any]:
    data = svc.settings_dict(svc.get_settings(db))
    data["regionale_feiertage_optionen"] = svc.REGIONAL_OPTIONS
    for key, value in list(data.items()):
        if isinstance(value, date | datetime):
            data[key] = value.isoformat()
    return data


@router.get("/settings")
def get_settings(db: DB) -> dict[str, Any]:
    return _out(db)


@router.patch("/settings")
def patch_settings(body: SettingsPatch, db: DB) -> dict[str, Any]:
    with service_errors():
        svc.update_settings(db, **body.model_dump(exclude_unset=True))
    return _out(db)


@router.get("/settings/api-token")
def api_token_status(db: DB) -> dict[str, bool]:
    return {"configured": svc.api_token_configured(db)}


@router.post("/settings/api-token", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
def create_api_token(db: DB) -> TokenOut:
    """Create or rotate the token for /api/sensors. The plaintext is returned only here."""
    token = svc.generate_api_token(db)
    return TokenOut(
        token=token,
        hinweis="Das Token wird nur jetzt angezeigt. Bitte sicher speichern (z. B. in Home Assistant).",
    )


@router.delete("/settings/api-token", status_code=status.HTTP_204_NO_CONTENT)
def delete_api_token(db: DB) -> Response:
    svc.clear_api_token(db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
