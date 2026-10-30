"""Helpers for the Playwright smoke tests (tests/e2e): live uvicorn server, API seeding, page checks.

The server runs in a subprocess (``python -m uvicorn app.main:app``) on a free port with its own temporary
DATA_DIR, so every test starts from a freshly migrated + seeded database. The browser talks to it under a
unique ``e2e-<random>.localhost`` host name (Chromium resolves ``*.localhost`` to loopback) so cookies never
clash with other local servers; the seeding client uses ``127.0.0.1``. Both are in ``ALLOWED_HOSTS``.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ICS_EXAMPLE = PROJECT_ROOT / "examples" / "ferien_bayern_BEISPIEL.ics"
BERLIN = ZoneInfo("Europe/Berlin")
STARTUP_TIMEOUT_S = 60

# Every variable app.config reads (kept in sync with tests/conftest.py) plus pytest's own marker, which
# would otherwise switch the backup scheduler off implicitly.
APP_ENV_VARS = (
    "DATA_DIR", "DATABASE_URL", "APP_SECRET", "ALLOWED_HOSTS", "BACKUP_CRON", "BACKUP_RETENTION_DAYS",
    "BASE_URL", "LOG_LEVEL", "PROXY_TRUSTED_NETS", "CSRF_ENFORCE", "SESSION_MAX_AGE", "DEPUTAT_SCHEDULER",
    "COOKIE_SECURE", "FERIEN_CATALOG_FILE", "PYTEST_CURRENT_TEST",
)


def today_berlin() -> date:
    return datetime.now(BERLIN).date()


def previous_month(day: date) -> tuple[date, date]:
    """First and last day of the month before ``day``."""
    last = day.replace(day=1) - timedelta(days=1)
    return last.replace(day=1), last


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class LiveServer:
    """A running Deputat instance. ``url`` is what the browser uses, ``api`` a CSRF-ready httpx client."""

    process: subprocess.Popen
    port: int
    host: str
    data_dir: Path
    log_path: Path
    api: httpx.Client = field(repr=False)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "deputat.sqlite"

    def log_tail(self, lines: int = 60) -> str:
        try:
            return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        except OSError:
            return "(kein Server-Log)"

    def category_id(self, code: str) -> int:
        """Id of a main category by code (read-only look-up; there is no category list API)."""
        con = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT id FROM category WHERE code = ? AND parent_id IS NULL", (code,)
            ).fetchone()
        finally:
            con.close()
        assert row is not None, f"Kategorie {code} fehlt im Seed"
        return int(row[0])

    def entries(self, day_from: date, day_to: date | None = None) -> list[dict[str, Any]]:
        res = self.api.get("/api/entries", params={"date_from": day_from.isoformat(),
                                                    "date_to": (day_to or day_from).isoformat()})
        res.raise_for_status()
        return res.json()["entries"]


def start_server(tmp_dir: Path) -> LiveServer:
    data_dir = tmp_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    host = f"e2e-{uuid.uuid4().hex[:10]}.localhost"
    env = {k: v for k, v in os.environ.items() if k not in APP_ENV_VARS}
    env.update({
        "DATA_DIR": str(data_dir),
        "APP_SECRET": "e2e-secret-" + uuid.uuid4().hex,
        "ALLOWED_HOSTS": f"127.0.0.1,{host}",
        "BASE_URL": f"http://{host}:{port}",
        "DEPUTAT_SCHEDULER": "0",
        "BACKUP_CRON": "off",
        "LOG_LEVEL": "warning",
        "PYTHONUNBUFFERED": "1",
    })
    log_path = tmp_dir / "server.log"
    log_file = open(log_path, "wb")  # noqa: SIM115 - closed with the process
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
         "--no-server-header", "--log-level", "warning"],
        cwd=PROJECT_ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT,
    )
    log_file.close()  # the child keeps its own handle
    api = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30)
    server = LiveServer(process=process, port=port, host=host, data_dir=data_dir, log_path=log_path, api=api)
    try:
        _wait_healthy(server)
        token = api.get("/api/csrf").json()["csrf_token"]
        api.headers["X-CSRF-Token"] = token
    except BaseException:
        stop_server(server)
        raise
    return server


def _wait_healthy(server: LiveServer) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if server.process.poll() is not None:
            raise RuntimeError(f"Server beendet (Code {server.process.returncode}):\n{server.log_tail()}")
        try:
            if server.api.get("/healthz", timeout=2).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Server nicht bereit nach {STARTUP_TIMEOUT_S} s:\n{server.log_tail()}")


def stop_server(server: LiveServer) -> None:
    server.api.close()
    if server.process.poll() is None:
        server.process.terminate()
        try:
            server.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.process.kill()
            server.process.wait(timeout=10)


# --------------------------------------------------------------------------- seeding


def _check(res: httpx.Response, *ok: int) -> dict[str, Any]:
    assert res.status_code in ok, f"{res.request.method} {res.request.url} -> {res.status_code}: {res.text}"
    return res.json() if res.content else {}


def seed_calendar(server: LiveServer, today: date) -> dict[str, Any]:
    """School-year period containing today (+ the previous one if the previous month lies before it) with its
    ferien: the app takes the official dates over from its catalogue when the period is created. Only for a
    school year the catalogue does not cover, the example ferien are imported (ICS through the calendar API)."""
    api = server.api
    period = _check(api.post("/api/calendar/periods", json={}), 201)
    prev_first, _ = previous_month(today)
    if prev_first < date.fromisoformat(period["start"]):
        start_year = date.fromisoformat(period["start"]).year - 1
        _check(api.post("/api/calendar/periods", json={"label": f"{start_year}/{(start_year + 1) % 100:02d}"}), 201)
    ferien = _check(api.get(f"/api/calendar/periods/{period['id']}/ferien"), 200)
    info: dict[str, Any] = {"period": period, "ferien": ferien, "source": "katalog"}
    if not ferien:
        ics = ICS_EXAMPLE.read_text(encoding="utf-8")
        _check(api.post(f"/api/calendar/periods/{period['id']}/ics/preview", json={"content": ics}), 200)
        committed = _check(api.post(f"/api/calendar/periods/{period['id']}/ics/commit", json={"content": ics}), 200)
        info.update(ferien=committed["created"], source="ics")
    return info


def seed_previous_month(server: LiveServer, today: date, code: str = "VN", days: int = 3) -> tuple[str, list[dict]]:
    """Manual entries with past timestamps on the first weekdays of the previous month. Returns (YYYY-MM, entries)."""
    first, last = previous_month(today)
    category_id = server.category_id(code)
    created = []
    day = first
    while day <= last and len(created) < days:
        if day.weekday() < 5:
            start = datetime(day.year, day.month, day.day, 14, 0, tzinfo=BERLIN)
            body = {"start_at": start.isoformat(), "end_at": (start + timedelta(hours=2)).isoformat(),
                    "category_id": category_id, "note": "E2E Vormonat"}
            created.append(_check(server.api.post("/api/entries", json=body), 201)["entry"])
        day += timedelta(days=1)
    return first.strftime("%Y-%m"), created


def parse_iso(value: str) -> datetime:
    """ISO string (``Z`` or offset) -> aware datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
