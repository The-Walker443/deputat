"""JSON API – Backups (Konzept §14.4): run a backup now, list backups, download a snapshot file.

The nightly job is scheduled by ``services/backup.start_scheduler`` (called by app.main).
Mounted by app.main under the ``/api`` prefix; unsafe methods need the CSRF header. No login: backup
downloads are reachable in the LAN (accepted, see DEPLOY.md).

- ``POST /backup/run``          – create a snapshot now (``backup_log.kind='manuell'``)
- ``GET  /backup``              – ``backup_log`` rows, files on disk, retention, cron
- ``GET  /backup/files/{name}`` – download one ``YYYY-MM-DD[...].sqlite.gz`` file
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import backup as svc

router = APIRouter(tags=["backup"])

DB = Annotated[Session, Depends(get_db)]


@router.post("/backup/run")
def run_backup(db: DB) -> dict[str, Any]:
    result = svc.create_backup(db, kind="manuell")
    if not result.ok:
        raise HTTPException(500, result.message or "Backup fehlgeschlagen.")
    return {
        "ok": True,
        "file": result.file,
        "size_bytes": result.size_bytes,
        "deleted": result.deleted,
        "log_id": result.log_id,
        "download_url": f"/api/backup/files/{result.file}",
    }


@router.get("/backup")
def list_backups(db: DB) -> dict[str, Any]:
    return svc.list_backups(db)


@router.get("/backup/files/{name}")
def download_backup(name: str) -> FileResponse:
    path = svc.backup_file_path(name)
    if path is None:
        raise HTTPException(404, "Sicherung nicht gefunden.")
    return FileResponse(
        path,
        media_type="application/gzip",
        filename=path.name,
        headers={"Cache-Control": "no-store"},
    )
