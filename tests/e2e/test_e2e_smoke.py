"""Smoke test through the main screens (Konzept §13), on a phone viewport and on desktop.

/heute: timer start via category chip -> switch -> stop; entries on /heute and /woche; manual entry via
/eintraege/neu, edit + revision in the history; close the previous month in the dialog (forced with a
reason if there are open items) and find the SHA-256 hash in the stored report; /konto renders its chart.
No console error, uncaught exception or CSP violation may occur on any visited page.
"""

from __future__ import annotations

import re
from datetime import datetime

import pytest
from playwright.sync_api import Page, expect

from e2e_support import BERLIN, LiveServer, seed_previous_month, today_berlin

pytestmark = pytest.mark.e2e

HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def wait_queue_empty(page: Page) -> None:
    """Wait until the offline queue has sent everything (and is not blocked by a conflict)."""
    page.wait_for_function(
        "async () => window.deputatQueue && !window.deputatQueue.blocked"
        " && (await window.deputatQueue.count()) === 0"
    )


def tap_and_sync(page: Page, selector: str, action: str) -> None:
    """Click a timer control and wait until its queued action reached the server (POST /api/timer/<action>)."""
    with page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith(f"/api/timer/{action}")
    ) as response:
        page.locator(selector).click()
    assert response.value.ok, f"{action}: {response.value.status} {response.value.text()}"
    wait_queue_empty(page)


def timer_entries(server: LiveServer) -> list[dict]:
    return [e for e in server.entries(today_berlin()) if e["source"] == "timer"]


@pytest.mark.parametrize("preset", ["mobile", "desktop"])
def test_smoke_main_flow(preset, live_server: LiveServer, seeded, open_context, problems) -> None:
    server = live_server
    today = today_berlin()
    ym, past = seed_previous_month(server, today)
    assert past, "keine Einträge im Vormonat angelegt"

    page = open_context(preset).new_page()

    # ------------------------------------------------------------ /heute: timer start -> switch -> stop
    page.goto("/heute")
    main = page.locator("[data-timer-main]")
    expect(main).to_be_visible()
    page.wait_for_function("() => !!window.deputatQueue")

    tap_and_sync(page, '[data-chip][data-code="VN"]', "start")
    expect(main).to_have_class(re.compile(r"\bis-running\b"))
    expect(page.locator('[data-chip][data-code="VN"]')).to_have_attribute("aria-pressed", "true")
    page.wait_for_timeout(1_200)  # distinct second -> both timer entries get a duration

    tap_and_sync(page, '[data-chip][data-code="K"]', "switch")
    expect(page.locator('[data-chip][data-code="K"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator("[data-timer-cat-label]")).to_contain_text("K ·")
    page.wait_for_timeout(1_200)

    tap_and_sync(page, "[data-timer-stop]", "stop")
    expect(main).not_to_have_class(re.compile(r"\bis-running\b"))

    timers = timer_entries(server)
    assert [e["category_code"] for e in timers] == ["VN", "K"], timers
    assert all(not e["running"] and e["end_at"] for e in timers), timers
    for entry in timers:
        expect(page.locator(f'#today-entries a[href="/eintraege/{entry["id"]}"]')).to_be_visible()

    # ------------------------------------------------------------ /woche shows the same entries
    page.goto("/woche")
    for entry in timers:
        expect(page.locator(f'a[href^="/eintraege/{entry["id"]}?"]:visible').first).to_be_visible()

    # ------------------------------------------------------------ manual entry via /eintraege/neu
    page.goto("/eintraege/neu?next=/heute")
    expect(page.locator("#f-datum")).to_have_value(today.isoformat())
    now = datetime.now(BERLIN)
    use_bounds = now.hour >= 1  # 00:10–00:40 lies in the past and before the timer entries
    if use_bounds:
        page.locator("#f-von").fill("00:10")
        page.locator("#f-bis").fill("00:40")
    else:
        page.locator('input[name="modus"][value="dauer"]').check(force=True)
        page.locator("#f-dauer").fill("0:30")
    page.locator("#f-kategorie").select_option(str(server.category_id("KO")))
    page.locator("#f-notiz").fill("E2E manuell")
    page.locator("form[data-entry-form] button[type=submit]").click()
    expect(page).to_have_url(re.compile(r"/heute$"))
    manual = [e for e in server.entries(today) if e["source"] == "manuell"]
    assert len(manual) == 1, manual
    manual_id = manual[0]["id"]
    expect(page.locator(f'#today-entries a[href="/eintraege/{manual_id}"]')).to_be_visible()

    # ------------------------------------------------------------ edit -> new revision in the history
    page.goto(f"/eintraege/{manual_id}")
    history = page.locator(".entry-history .history-item")
    expect(history).to_have_count(1)
    if use_bounds:
        page.locator("#f-bis").fill("00:55")
    else:
        page.locator("#f-dauer").fill("0:45")
    page.locator("#f-notiz").fill("E2E manuell – korrigiert")
    page.locator("form button[type=submit]", has_text="Speichern").first.click()
    expect(page).to_have_url(re.compile(rf"/eintraege/{manual_id}$"))
    expect(page.get_by_text("Änderung gespeichert")).to_be_visible()
    expect(history).to_have_count(2)
    expect(page.locator(".entry-history .history-diff").first).to_be_visible()
    revisions = server.api.get(f"/api/entries/{manual_id}/revisions").json()
    assert revisions, revisions

    # ------------------------------------------------------------ close the previous month (dialog)
    page.goto(f"/monate/{ym}")
    page.locator('[data-dialog-open="close-dialog"]').click()
    dialog = page.locator("#close-dialog")
    expect(dialog).to_be_visible()
    force = dialog.locator("[data-close-force]")
    if force.count():
        force.check()
        dialog.locator('textarea[name="begruendung"]').fill("E2E: Tage ohne Einträge sind Testdaten.")
    dialog.locator('input[name="bestaetigt"]').check()
    dialog.get_by_role("button", name="Jetzt abschließen").click()
    expect(page).to_have_url(re.compile(rf"/monate/{ym}\?abgeschlossen=1$"))
    expect(page.locator("[data-month-status]")).to_have_text(re.compile("bgeschlossen"))
    month_hash = page.locator("code.months-hash-full").first.inner_text().strip()
    assert HASH_RE.match(month_hash), month_hash

    # ------------------------------------------------------------ stored month report shows the hash
    page.get_by_role("link", name="Bericht ansehen").click()
    expect(page).to_have_url(re.compile(rf"/monate/{ym}/bericht$"))
    expect(page.locator(".hash").first).to_have_text(month_hash)
    expect(page.get_by_text("SHA-256").first).to_be_visible()

    # ------------------------------------------------------------ /konto renders the chart
    page.goto("/konto")
    canvas = page.locator("[data-saldo-chart] canvas")
    expect(canvas).to_be_visible()
    page.wait_for_function(
        "() => { const c = document.querySelector('[data-saldo-chart] canvas');"
        " return !!(window.Chart && c && window.Chart.getChart(c)); }"
    )
    box = canvas.bounding_box()
    assert box and box["width"] > 100 and box["height"] > 50, box

    problems.assert_clean()
