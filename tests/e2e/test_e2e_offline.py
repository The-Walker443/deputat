"""Offline queue (Konzept §8.4): start + stop while offline, sync after reconnect.

The two timer actions are stored on the device with the device time of the tap. After going online again
the queue flushes; the result must be exactly ONE new timer entry carrying the device timestamps (not the
sync time), even when the flush is triggered several times concurrently and the page is reloaded.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from playwright.sync_api import expect

from e2e_support import LiveServer, parse_iso, today_berlin

pytestmark = pytest.mark.e2e

OFFLINE_WAIT_MS = 3_000  # sync happens clearly later than the taps


@pytest.mark.parametrize("preset", ["mobile"])
def test_offline_start_stop_syncs_once(preset, live_server: LiveServer, seeded, open_context, problems) -> None:
    server = live_server
    today = today_berlin()
    context = open_context(preset)
    page = context.new_page()
    page.goto("/heute")
    main = page.locator("[data-timer-main]")
    expect(main).to_be_visible()
    page.wait_for_function("() => !!window.deputatQueue")
    before = {e["id"] for e in server.entries(today)}

    # Failed requests while offline are expected (queue POSTs); only ignored during the offline phase.
    problems.ignore += ["ERR_INTERNET_DISCONNECTED", "Failed to fetch", "Failed to load resource"]
    context.set_offline(True)
    page.wait_for_function("() => navigator.onLine === false")

    page.locator('[data-chip][data-code="VN"]').click()
    expect(main).to_have_class(re.compile(r"\bis-running\b"))
    page.wait_for_timeout(1_500)
    page.locator("[data-timer-stop]").click()
    expect(main).not_to_have_class(re.compile(r"\bis-running\b"))

    queued = page.evaluate("() => window.deputatQueue.list()")
    assert [q["type"] for q in queued] == ["start", "stop"], queued
    started_at = parse_iso(queued[0]["body"]["started_at"])
    stopped_at = parse_iso(queued[1]["body"]["stopped_at"])
    expect(page.locator("[data-sync-badge]")).to_be_visible()
    expect(page.locator("[data-sync-text]")).to_contain_text("offline")

    page.wait_for_timeout(OFFLINE_WAIT_MS)
    assert {e["id"] for e in server.entries(today)} == before, "offline darf nichts beim Server ankommen"

    context.set_offline(False)
    # Hammer the queue: the "online" event already flushes; extra flushes must not send anything twice.
    page.evaluate("() => Promise.all([window.deputatQueue.flush(), window.deputatQueue.flush()])")
    page.wait_for_function(
        "async () => !window.deputatQueue.blocked && (await window.deputatQueue.count()) === 0"
    )
    expect(page.locator("[data-sync-badge]")).to_be_hidden()
    problems.ignore.clear()

    new = [e for e in server.entries(today) if e["id"] not in before]
    assert len(new) == 1, new
    entry = new[0]
    assert entry["source"] == "timer" and entry["category_code"] == "VN" and not entry["running"], entry
    assert abs(parse_iso(entry["start_at"]) - started_at) < timedelta(seconds=1), (entry, started_at)
    assert abs(parse_iso(entry["end_at"]) - stopped_at) < timedelta(seconds=1), (entry, stopped_at)
    assert parse_iso(entry["created_at"]) - started_at >= timedelta(milliseconds=OFFLINE_WAIT_MS - 500), entry
    expect(page.locator(f'#today-entries a[href="/eintraege/{entry["id"]}"]')).to_be_visible()

    # A reload (new flush on DOMContentLoaded) must not resend anything.
    page.reload()
    page.wait_for_function("async () => (await window.deputatQueue.count()) === 0")
    expect(page.locator(f'#today-entries a[href="/eintraege/{entry["id"]}"]')).to_have_count(1)
    status = server.api.get("/api/timer/status").json()
    assert status["running"] is None, status
    assert len([e for e in server.entries(today) if e["id"] not in before]) == 1

    problems.assert_clean()
