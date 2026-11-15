"""Day type chip on the home screen "Erfassen" on a phone (390x844): Krank disables the field and leaves the week
saldo unchanged (the removed value is credited as soll), Fortbildung 7,5 becomes a Tagessumme of category F,
Klassenfahrt 12 counts with the cap of variant B (10 h) in the account, back to Unterricht gives AZ again.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import pytest
from playwright.sync_api import Page, expect

from e2e_support import BERLIN, LiveServer, previous_month, today_berlin

pytestmark = pytest.mark.e2e

H = 3600


def iso_ref(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def wait_queue_empty(page: Page) -> None:
    page.wait_for_function(
        "async () => window.deputatQueue && !window.deputatQueue.blocked"
        " && (await window.deputatQueue.count()) === 0"
    )


def find_week(server: LiveServer, today: date) -> tuple[date, date]:
    """(monday, a past school day with soll) – this week or one of the four before."""
    monday = today - timedelta(days=today.weekday())
    for back in range(5):
        mon = monday - timedelta(days=7 * back)
        week = server.api.get(f"/api/weeks/{iso_ref(mon)}").json()
        for d in week["days"]:
            if d["date"] < today.isoformat() and d["day_type"] == "schultag" and not d["absence"] and d["soll"]:
                return mon, date.fromisoformat(d["date"])
    pytest.skip("kein vergangener Schultag in den letzten fünf Wochen")


def day_entries(server: LiveServer, d: date) -> list[dict]:
    return server.entries(d)


def saved(page: Page, d: date) -> None:
    """The row confirms the save (the queue may look empty for a moment before the IndexedDB write)."""
    expect(page.locator(f'[data-date="{d.isoformat()}"] [data-qe-status]')).to_contain_text("✓")
    wait_queue_empty(page)


def choose(page: Page, d: date, typ: str) -> None:
    page.locator(f'[data-date="{d.isoformat()}"] [data-qe-typ]').click()
    sheet = page.locator("[data-qe-sheet]")
    expect(sheet).to_be_visible()
    page.locator(f'[data-qe-typ-choice="{typ}"]').click()


@pytest.mark.parametrize("preset", ["mobile"])
def test_erfassen_day_types(preset, live_server: LiveServer, seeded, open_context, problems) -> None:
    server = live_server
    monday, krank_day = find_week(server, today_berlin())
    saturday, sunday = monday + timedelta(days=5), monday + timedelta(days=6)
    page = open_context(preset).new_page()
    page.goto(f"/erfassen/{iso_ref(monday)}")
    page.wait_for_function("() => !!window.deputatQueue")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    row = page.locator(f'[data-date="{krank_day.isoformat()}"]')
    field = page.locator(f"#qe-{krank_day.isoformat()}")
    expect(row.locator("[data-qe-typ]")).to_have_text("Unterricht")

    # ------------------------------------------------------------ Krank: field disabled, saldo unchanged
    field.click()
    page.keyboard.type("8")
    field.blur()  # saves the value
    saved(page, krank_day)
    saldo = page.locator("[data-qe-sum-delta]").inner_text()
    choose(page, krank_day, "krank")
    confirm = page.locator("[data-qe-confirm]")
    expect(confirm).to_be_visible()  # 8:00 would be removed: one-tap confirmation
    expect(page.locator("[data-qe-confirm-text]")).to_contain_text("8:00")
    page.locator("[data-qe-confirm-yes]").click()
    expect(page.locator("[data-qe-sheet]")).to_be_hidden()
    expect(field).to_be_disabled()
    expect(field).to_have_value("")
    saved(page, krank_day)
    expect(row.locator("[data-qe-typ]")).to_have_text("Krank")
    expect(row.locator("[data-qe-meta]")).to_contain_text("Soll wird gutgeschrieben")
    expect(page.locator("[data-qe-sum-delta]")).to_have_text(saldo)  # 8 h removed, 8 h credited
    assert [e for e in day_entries(server, krank_day) if e["tagessumme"]] == []
    assert server.api.get(f"/api/calendar/overrides/{krank_day}").json()["absence_type"] == "krank"
    page.reload()
    page.wait_for_function("() => !!window.deputatQueue")
    expect(field).to_be_disabled()
    expect(page.locator("[data-qe-sum-delta]")).to_have_text(saldo)

    # ------------------------------------------------------------ Fortbildung 7,5 -> category F
    choose(page, saturday, "fortbildung")
    expect(page.locator(f'[data-date="{saturday.isoformat()}"] [data-qe-typ]')).to_have_text("Fortbildung")
    page.locator(f"#qe-{saturday.isoformat()}").click()
    page.keyboard.type("7,5")
    page.keyboard.press("Enter")
    saved(page, saturday)
    (entry,) = day_entries(server, saturday)
    assert (entry["category_code"], entry["duration_seconds"], entry["tagessumme"]) == ("F", 27000, True)

    # ------------------------------------------------------------ Klassenfahrt 12 -> counts 10 h (variant B)
    choose(page, sunday, "klassenfahrt")
    page.locator(f"#qe-{sunday.isoformat()}").click()
    page.keyboard.type("12")
    page.keyboard.press("Enter")
    saved(page, sunday)
    (entry,) = day_entries(server, sunday)
    assert entry["category_code"] == "SV" and "klassenfahrt" in entry["flags"] and entry["duration_seconds"] == 12 * H
    week = server.api.get(f"/api/weeks/{iso_ref(monday)}").json()
    sun = next(d for d in week["days"] if d["date"] == sunday.isoformat())
    assert sun["ist"] == 12 * H and sun["ist_konto"] == 10 * H
    expect(page.locator(f'[data-date="{sunday.isoformat()}"] [data-qe-meta]')).to_contain_text("zählt 10:00")

    # ------------------------------------------------------------ back to Unterricht -> AZ
    choose(page, saturday, "unterricht")
    saved(page, saturday)
    expect(page.locator(f'[data-date="{saturday.isoformat()}"] [data-qe-typ]')).to_have_text("Wochenende")
    (entry,) = day_entries(server, saturday)
    assert entry["category_code"] == "AZ" and entry["duration_seconds"] == 27000
    expect(page.locator(f"#qe-{saturday.isoformat()}")).to_have_value("7:30")

    # ------------------------------------------------------------ the sheet closes with Escape, focus returns
    chip = page.locator(f'[data-date="{saturday.isoformat()}"] [data-qe-typ]')
    chip.click()
    expect(page.locator("[data-qe-sheet]")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("[data-qe-sheet]")).to_be_hidden()
    expect(chip).to_be_focused()
    assert re.match(r"^[+-]?\d+:\d\d$", saldo)
    problems.assert_clean()


def fmt_hm(seconds: int) -> str:
    minutes = (seconds + 30) // 60
    return f"{minutes // 60}:{minutes % 60:02d}"


def krank_note_matches_server(page: Page, server: LiveServer, monday: date) -> None:
    credit = server.api.get(f"/api/weeks/{iso_ref(monday)}").json()["krank_gutschrift"]
    note = page.locator("[data-qe-krank-note]")
    if credit:
        expect(note).to_be_visible()
        expect(note.locator("[data-qe-krank-sum]")).to_have_text(fmt_hm(credit))
    else:
        expect(note).to_be_hidden()


@pytest.mark.parametrize("preset", ["mobile"])
def test_erfassen_day_types_review_fixes(preset, live_server: LiveServer, seeded, open_context, problems) -> None:
    """Krank-Gutschrift note follows type changes, the krank info line wraps at 360 px, the info line describes a
    queued type, Tab skips the chip (Alt+ArrowDown opens it), the multi-day offer, a closed month asks for a reason."""
    server = live_server
    problems.ignore.append("Failed to load resource")  # the offline step aborts requests on purpose
    monday, day = find_week(server, today_berlin())
    # 2 h recorded individually + 7:30 day total
    start = datetime(day.year, day.month, day.day, 7, 0, tzinfo=BERLIN)
    body = {"start_at": start.isoformat(), "end_at": (start + timedelta(hours=2)).isoformat(),
            "category_id": server.category_id("K")}
    assert server.api.post("/api/entries", json=body).status_code == 201
    assert server.api.put(f"/api/days/{day}/total", json={"value": "9,5"}).status_code == 200

    page = open_context(preset).new_page()
    page.set_viewport_size({"width": 360, "height": 740})
    page.goto(f"/erfassen/{iso_ref(monday)}")
    page.wait_for_function("() => !!window.deputatQueue")
    row = page.locator(f'[data-date="{day.isoformat()}"]')
    meta = row.locator("[data-qe-meta]")
    krank_note_matches_server(page, server, monday)

    # ------------------------------------------------------------ Krank: note updated without reload, line not cut
    choose(page, day, "krank")
    page.locator("[data-qe-confirm-yes]").click()
    saved(page, day)
    krank_note_matches_server(page, server, monday)
    expect(meta).to_contain_text("+ 2:00 erfasst, zählt zusätzlich")
    assert page.evaluate("(el) => el.scrollHeight <= el.clientHeight + 1", meta.element_handle())
    link = meta.locator("[data-qe-detail]").bounding_box()
    box = meta.bounding_box()
    assert link and box and link["y"] + link["height"] <= box["y"] + box["height"] + 1
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    # ------------------------------------------------------------ queued type change: the line shows the new type
    page.route("**/api/days/**", lambda route: route.abort())
    choose(page, day, "unterricht")
    expect(row.locator("[data-qe-typ]")).to_have_class(re.compile(r"\bis-pending\b"))
    expect(meta).not_to_contain_text("krank")
    page.unroute("**/api/days/**")
    page.evaluate("() => window.deputatQueue.flush()")
    saved(page, day)
    krank_note_matches_server(page, server, monday)
    expect(page.locator(f"#qe-{day.isoformat()}")).to_be_enabled()

    # ------------------------------------------------------------ Tab goes field to field; Alt+ArrowDown opens the sheet
    rows = page.locator("[data-qe-day]")
    first_date = rows.nth(0).get_attribute("data-date")
    second_date = rows.nth(1).get_attribute("data-date")
    first = page.locator(f"#qe-{first_date}")
    if first.is_enabled() and page.locator(f"#qe-{second_date}").is_enabled():
        first.focus()
        page.keyboard.press("Tab")
        expect(page.locator(f"#qe-{second_date}")).to_be_focused()
    page.locator(f"#qe-{day.isoformat()}").focus()
    page.keyboard.press("Alt+ArrowDown")
    expect(page.locator("[data-qe-sheet]")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("[data-qe-sheet]")).to_be_hidden()

    # ------------------------------------------------------------ Klassenfahrt for several days in one tap
    next_monday = monday + timedelta(days=7 * (1 + (today_berlin() - monday).days // 7))
    week = server.api.get(f"/api/weeks/{iso_ref(next_monday)}").json()
    mon, tue = week["days"][0], week["days"][1]
    if mon["typ_changeable"] and mon["typ"] == "unterricht" and tue["typ_changeable"] and tue["typ"] == "unterricht":
        page.goto(f"/erfassen/{iso_ref(next_monday)}")
        page.wait_for_function("() => !!window.deputatQueue")
        choose(page, next_monday, "klassenfahrt")
        follow = page.locator(f'[data-date="{next_monday.isoformat()}"] [data-qe-follow]')
        expect(follow).to_contain_text("übernehmen")
        follow.click()
        wait_queue_empty(page)
        expect(page.locator(f'[data-date="{tue["date"]}"] [data-qe-typ]')).to_have_text("Klassenfahrt")
        week = server.api.get(f"/api/weeks/{iso_ref(next_monday)}").json()
        assert week["days"][1]["typ"] == "klassenfahrt"

    # ------------------------------------------------------------ closed month: the sheet asks for the reason
    first_prev, last_prev = previous_month(today_berlin())
    closed = server.api.post(f"/api/months/{first_prev:%Y-%m}/close", json={"force": True, "note": "E2E-Test"})
    assert closed.status_code == 200, closed.text
    target = next((first_prev + timedelta(days=i) for i in range((last_prev - first_prev).days + 1)
                   if (first_prev + timedelta(days=i)).weekday() < 5), None)
    tweek = server.api.get(f"/api/weeks/{iso_ref(target)}").json()
    tday = next(d for d in tweek["days"] if d["date"] == target.isoformat())
    if tday["typ_changeable"] and "schulveranstaltung" in tday["typ_options"]:
        page.goto(f"/erfassen/{iso_ref(target)}")
        page.wait_for_function("() => !!window.deputatQueue")
        page.locator(f'[data-date="{target.isoformat()}"] [data-qe-typ]').click()
        reason = page.locator("[data-qe-reason-input]")
        expect(reason).to_be_visible()
        expect(reason).to_be_focused()
        page.locator('[data-qe-typ-choice="schulveranstaltung"]').click()
        expect(page.locator("[data-qe-reason-error]")).to_be_visible()  # nothing sent without a reason
        expect(page.locator("[data-qe-sheet]")).to_be_visible()
        reason.fill("Wandertag nachgetragen")
        page.locator('[data-qe-typ-choice="schulveranstaltung"]').click()
        saved(page, target)
        tweek = server.api.get(f"/api/weeks/{iso_ref(target)}").json()
        assert next(d for d in tweek["days"] if d["date"] == target.isoformat())["typ"] == "schulveranstaltung"
    problems.assert_clean()
