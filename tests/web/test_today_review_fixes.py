"""Regression tests for the capture-UI review (Heute): offline queue flush order, midnight carry-over,
"Nachtragen" link."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from html import unescape
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import Category
from app.db.session import SessionLocal

ROOT = Path(__file__).resolve().parents[2]


def _cat(code: str) -> Category:
    with SessionLocal() as s:
        cat = s.scalar(select(Category).where(Category.code == code))
        s.expunge(cat)
        return cat


def _sub_of(code: str) -> int:
    with SessionLocal() as s:
        main = s.scalar(select(Category).where(Category.code == code))
        return s.scalar(select(Category.id).where(Category.parent_id == main.id).order_by(Category.sort))


def _state(html: str) -> dict:
    m = re.search(r"data-state='([^']*)'", html)
    assert m, "data-state missing"
    return json.loads(unescape(m.group(1)))


def test_running_state_carries_subcategory_and_note(client):
    """'Fortsetzen ab 00:00' re-starts with the running entry's values – subcategory and note included."""
    sub = _sub_of("U")
    started = (datetime.now(UTC) - timedelta(minutes=20)).replace(microsecond=0).isoformat()
    r = client.post("/api/timer/start", json={"category_id": _cat("U").id, "subcategory_id": sub,
                                              "note": "Projekt", "started_at": started})
    assert r.status_code == 200, r.text
    run = _state(client.get("/heute").text)["running"]
    assert run["subcategory_id"] == sub
    assert run["note"] == "Projekt"


def test_nachtragen_opens_new_entry_form(client):
    html = client.get("/heute").text
    assert 'href="/eintraege/neu?next=/heute"' in html


# A minimal browser stand-in for offline-queue.js: no IndexedDB (in-memory store), a fetch whose first
# response is slow, and a second action enqueued while the first request is still in flight.
QUEUE_HARNESS = r"""
const fs = require("fs");
const sent = [];
globalThis.window = globalThis;
window.addEventListener = () => {};
globalThis.document = { addEventListener: () => {}, querySelector: () => null, visibilityState: "visible" };
globalThis.setInterval = () => 0;
globalThis.fetch = (url, opts) => new Promise((resolve) => {
  const body = JSON.parse(opts.body);
  sent.push(body.client_action_id);
  const delay = sent.length === 1 ? 50 : 0;
  setTimeout(() => resolve({
    type: "basic", redirected: false, status: 200, ok: true, statusText: "OK",
    headers: { get: () => "application/json" },
    json: async () => ({ running: null }),
  }), delay);
});
eval(fs.readFileSync(process.argv[2], "utf8"));
const Q = window.deputatQueue;
let synced = [];
Q.on("synced", (d) => synced.push(d.sent));
(async () => {
  await Q.enqueue({ id: "a", type: "start", url: "/api/timer/start", body: { client_action_id: "a" } });
  await new Promise((r) => setTimeout(r, 10));            // first request is in flight now
  await Q.enqueue({ id: "b", type: "stop", url: "/api/timer/stop", body: { client_action_id: "b" } });
  await new Promise((r) => setTimeout(r, 200));
  console.log(JSON.stringify({ sent, left: (await Q.list()).length, synced }));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_queue_sends_actions_enqueued_during_flush(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(QUEUE_HARNESS, encoding="utf-8")
    result = subprocess.run(["node", str(harness), str(ROOT / "app/static/js/offline-queue.js")],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["sent"] == ["a", "b"]  # in order, each exactly once, without waiting for the 30 s retry
    assert out["left"] == 0
    assert out["synced"][-1] >= 1
