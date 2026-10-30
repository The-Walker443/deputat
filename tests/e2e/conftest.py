"""Playwright smoke tests (Konzept §13 "Smoke-Tests (Playwright)", without login – there is none).

Run:   uv run playwright install chromium     (once)
       uv run pytest -m e2e                   (or: make e2e; add --headed / --slowmo 300 to watch)

The tests are marked ``e2e`` and are *deselected* unless explicitly requested (``-m e2e`` or a path inside
tests/e2e), so a plain ``uv run pytest`` never needs a browser. If Chromium is not installed they skip.

Fixtures:
- ``live_server``: fresh uvicorn subprocess per test (temp DATA_DIR, unique ``*.localhost`` host).
- ``seeded``: school-year period with its ferien on ``live_server`` (official dates from the app's catalogue;
  example ICS via the calendar API only for a school year the catalogue does not cover).
- ``open_context``: factory for browser contexts (viewport preset, de-DE, Europe/Berlin) whose pages
  collect console errors, page errors and CSP violations into ``problems``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from e2e_support import LiveServer, seed_calendar, start_server, stop_server, today_berlin

E2E_DIR = Path(__file__).resolve().parent

VIEWPORTS: dict[str, dict[str, Any]] = {
    "mobile": {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 3, "is_mobile": True,
               "has_touch": True},
    "desktop": {"viewport": {"width": 1280, "height": 900}},
}

# Recorded in every page before any app script runs (CSP violations also show up as console errors in
# Chromium; this catches them even if the browser stops logging them).
CSP_LISTENER = """
document.addEventListener("securitypolicyviolation", (e) => {
  try { window.__e2eCsp(e.violatedDirective + " " + (e.blockedURI || "inline") + " @ " + e.documentURI); }
  catch (_) {}
});
"""


# --------------------------------------------------------------------------- selection


def _e2e_requested(config: pytest.Config) -> bool:
    markexpr = config.getoption("markexpr", "") or ""
    if "e2e" in markexpr.replace("not e2e", ""):
        return True
    for arg in config.args:
        path = Path(arg.split("::", 1)[0]).resolve()
        if path == E2E_DIR or E2E_DIR in path.parents:
            return True
    return False


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect the browser tests unless they were asked for (``-m e2e`` or a tests/e2e path)."""
    if _e2e_requested(config):
        return
    keep, drop = [], []
    for item in items:
        (drop if E2E_DIR in Path(str(item.path)).parents else keep).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


# --------------------------------------------------------------------------- browser availability


@pytest.fixture(scope="session", autouse=True)
def _require_chromium(playwright) -> None:
    """Skip (not fail) when the Playwright Chromium build is not installed."""
    executable = Path(playwright.chromium.executable_path)
    if not executable.exists():
        pytest.skip("Playwright-Chromium fehlt – einmalig `uv run playwright install chromium` ausführen.")


# --------------------------------------------------------------------------- server


@pytest.fixture
def live_server(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[LiveServer]:
    server = start_server(tmp_path)
    try:
        yield server
    finally:
        stop_server(server)
        report = getattr(request.node, "rep_call", None)
        if report is not None and report.failed:
            print(f"\n--- server log ({server.url}) ---\n{server.log_tail()}")


@pytest.fixture
def seeded(live_server: LiveServer) -> dict[str, Any]:
    info = seed_calendar(live_server, today_berlin())
    assert info["ferien"], f"Schuljahr ohne Ferien angelegt ({info['source']}): {info['period']}"
    return info


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


# --------------------------------------------------------------------------- browser contexts


@dataclass
class Problems:
    """Console errors, uncaught page errors and CSP violations seen by all pages of a test."""

    items: list[str] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)  # substrings of expected messages (e.g. offline fetches)

    def add(self, kind: str, text: str, url: str) -> None:
        if any(part in text for part in self.ignore):
            return
        self.items.append(f"[{kind}] {text} (auf {url})")

    def assert_clean(self) -> None:
        assert not self.items, "Konsolenfehler / CSP-Verstöße:\n" + "\n".join(self.items)


@pytest.fixture
def problems() -> Problems:
    return Problems()


@pytest.fixture
def open_context(new_context, live_server: LiveServer, problems: Problems) -> Callable[..., Any]:
    """``open_context("mobile"|"desktop", **extra)`` -> BrowserContext with problem collection wired up."""

    def _open(preset: str, **extra: Any):
        ctx = new_context(
            base_url=live_server.url, locale="de-DE", timezone_id="Europe/Berlin",
            **VIEWPORTS[preset], **extra,
        )
        ctx.set_default_timeout(15_000)
        ctx.expose_function("__e2eCsp", lambda text: problems.add("csp", text, ""))
        ctx.add_init_script(CSP_LISTENER)

        def on_page(page) -> None:
            page.on("console", lambda msg: msg.type == "error" and problems.add("console", msg.text, page.url))
            page.on("pageerror", lambda err: problems.add("pageerror", str(err), page.url))

        ctx.on("page", on_page)
        return ctx

    return _open
