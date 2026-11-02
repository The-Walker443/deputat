"""Home screen "Erfassen" on a phone (390x844): type a whole week with the keyboard flow (Enter -> next day,
after Friday the keyboard closes), switch weeks, a value typed right before tapping another tab is saved before
the next page loads, values persist after a reload, an entry made offline is synced after reconnecting.

Each value becomes exactly one "Tagessumme" entry (category AZ, duration only) on the server.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import Page, expect

from e2e_support import LiveServer, today_berlin

pytestmark = pytest.mark.e2e

H = 3600
WEEK_VALUES = [("8", 8 * H, "8:00"), ("7,5", 7.5 * H, "7:30"), ("9:15", 9.25 * H, "9:15"), ("7,30", 7.5 * H, "7:30"),
               ("5", 5 * H, "5:00")]


def wait_queue_empty(page: Page) -> None:
    page.wait_for_function(
        "async () => window.deputatQueue && !window.deputatQueue.blocked"
        " && (await window.deputatQueue.count()) === 0"
    )


def field(page: Page, day: date):
    return page.locator(f"#qe-{day.isoformat()}")


def tagessummen(server: LiveServer, monday: date) -> dict[str, int]:
    return {e["day"]: e["duration_seconds"] for e in server.entries(monday, monday + timedelta(days=6))
            if e["category_code"] == "AZ"}


def iso_ref(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


@pytest.mark.parametrize("preset", ["mobile"])
def test_erfassen_week_keyboard_flow_switch_and_offline(preset, live_server: LiveServer, seeded, open_context,
                                                         problems) -> None:
    server = live_server
    today = today_berlin()
    monday = today - timedelta(days=today.weekday())
    page = open_context(preset).new_page()

    page.goto("/")
    expect(page).to_have_url(re.compile(r"/erfassen$"))
    page.wait_for_function("() => !!window.deputatQueue")
    expect(page.locator(".bottom-nav-item.is-active")).to_have_text(re.compile("Erfassen"))
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    # ------------------------------------------------------------ a whole week with Enter -> next day
    field(page, monday).click()
    for i, (typed, _seconds, _shown) in enumerate(WEEK_VALUES):
        expect(field(page, monday + timedelta(days=i))).to_be_focused()
        page.keyboard.type(typed)
        page.keyboard.press("Enter")
    # empty weekend: Enter on Friday is "done" (keyboard closes) instead of jumping to Saturday
    if today.weekday() < 5:  # (with "today" on the weekend the weekend fields stay in the flow)
        assert page.evaluate("() => !document.activeElement || !document.activeElement.matches('[data-qe-input]')")
    wait_queue_empty(page)
    for i, (_typed, _seconds, shown) in enumerate(WEEK_VALUES):
        expect(field(page, monday + timedelta(days=i))).to_have_value(shown)
    thursday = monday + timedelta(days=3)
    expect(page.locator(f'[data-date="{thursday.isoformat()}"] [data-qe-status]')).to_contain_text("7,30 = 7:30 h")
    expect(page.locator("[data-qe-sum-ist]")).to_have_text("37:15")
    expected = {(monday + timedelta(days=i)).isoformat(): int(s) for i, (_t, s, _v) in enumerate(WEEK_VALUES)}
    assert tagessummen(server, monday) == expected

    # invalid input stays on the day with an inline error, nothing is sent
    saturday = monday + timedelta(days=5)
    field(page, saturday).click()
    page.keyboard.type("abc")
    page.keyboard.press("Enter")
    expect(field(page, saturday)).to_be_focused()
    expect(page.locator(f'[data-date="{saturday.isoformat()}"] [data-qe-status]')).to_contain_text("7,5")
    page.keyboard.press("Escape")

    # ------------------------------------------------------------ switch weeks (in place) and back
    page.locator('a[rel="next"][data-qe-nav]').click()
    expect(page).to_have_url(re.compile(rf"/erfassen/{iso_ref(monday + timedelta(days=7))}$"))
    expect(page.locator("#qe-week-label")).to_contain_text(f"KW {(monday + timedelta(days=7)).isocalendar()[1]}")
    expect(field(page, monday + timedelta(days=7))).to_have_value("")
    page.locator("a.qe-today-link").click()
    expect(page).to_have_url(re.compile(r"/erfassen$"))
    expect(field(page, monday)).to_have_value("8:00")

    # ------------------------------------------------------------ type, then tap another tab at once
    field(page, saturday).click()
    page.keyboard.type("2")
    page.locator('.bottom-nav a[href="/konto"]').click()
    expect(page).to_have_url(re.compile(r"/konto$"))
    assert tagessummen(server, monday)[saturday.isoformat()] == 2 * H, "vor dem Seitenwechsel gespeichert"
    page.goto("/erfassen")

    # ------------------------------------------------------------ values persist after a reload
    page.reload()
    page.wait_for_function("() => !!window.deputatQueue")
    for i, (_typed, _seconds, shown) in enumerate(WEEK_VALUES):
        expect(field(page, monday + timedelta(days=i))).to_have_value(shown)

    # ------------------------------------------------------------ offline entry, synced after reconnect
    sunday = monday + timedelta(days=6)
    problems.ignore += ["ERR_INTERNET_DISCONNECTED", "Failed to fetch", "Failed to load resource"]
    page.context.set_offline(True)
    page.wait_for_function("() => navigator.onLine === false")
    field(page, sunday).click()
    page.keyboard.type("3")
    page.keyboard.press("Enter")  # last day: Enter leaves the field and saves
    status = page.locator(f'[data-date="{sunday.isoformat()}"] [data-qe-status]')
    expect(status).to_contain_text("nicht verbunden")
    expect(page.locator("[data-qe-provisional]")).to_be_visible()
    expect(page.locator("[data-qe-sum-ist]")).to_have_text("42:15")  # computed on the device
    page.wait_for_function("async () => (await window.deputatQueue.count()) === 1")
    queued = page.evaluate("() => window.deputatQueue.list()")
    assert [(q["type"], q["method"], q["body"]["seconds"]) for q in queued] == [("day_total", "PUT", 3 * H)], queued
    page.wait_for_timeout(1_500)
    assert sunday.isoformat() not in tagessummen(server, monday), "offline darf nichts beim Server ankommen"

    page.context.set_offline(False)
    wait_queue_empty(page)
    problems.ignore.clear()
    expect(status).to_contain_text("✓ gespeichert")
    assert tagessummen(server, monday)[sunday.isoformat()] == 3 * H
    expect(page.locator("[data-qe-sum-ist]")).to_have_text("42:15")
    expect(page.locator("[data-qe-provisional]")).to_be_hidden()

    problems.assert_clean()
