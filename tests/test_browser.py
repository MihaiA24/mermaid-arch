from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import Page

from mermaid_arch.browser import BrowserSession, _candidates, svg_to_pdf, svg_to_png

SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 40">'
    '<rect width="30" height="30"/><text x="40" y="20">Gateway</text></svg>'
)


def test_other_playwright_chromium_revisions_are_tried_newest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = Path("chrome-linux64", "chrome")
    for revision in ("chromium-1248", "chromium-1300", "chromium_headless_shell-1300"):
        (tmp_path / revision / executable).parent.mkdir(parents=True)
        (tmp_path / revision / executable).touch()
    (tmp_path / "chromium-1254").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path))
    playwright = SimpleNamespace(executable_path=str(tmp_path / "chromium-1254" / executable))

    found = [path for path, origin in _candidates(playwright) if origin == "playwright"]

    assert found == [
        str(tmp_path / "chromium-1300/chrome-linux64/chrome"),
        str(tmp_path / "chromium-1248/chrome-linux64/chrome"),
    ]


def _kill_chromium(page: Page) -> None:
    """SIGKILL the browser behind `page`, as a crash would, and wait until Playwright notices."""
    info = page.context.browser.new_browser_cdp_session().send("SystemInfo.getProcessInfo")
    os.kill(next(p["id"] for p in info["processInfo"] if p["type"] == "browser"), signal.SIGKILL)
    page.wait_for_event("close")


@pytest.mark.browser
def test_session_outlives_a_chromium_that_died(in_own_thread) -> None:
    files = {"/": "<!doctype html>"}

    def scenario() -> None:
        with BrowserSession() as session:
            with session.page(files) as page:
                _kill_chromium(page)

            assert svg_to_png(session, SVG).startswith(b"\x89PNG\r\n\x1a\n")
            with session.page(files) as page:
                _kill_chromium(page)

    in_own_thread(scenario)


@pytest.mark.browser
def test_pdf_bytes_do_not_depend_on_the_time(browser_session) -> None:
    first = svg_to_pdf(browser_session, SVG)
    time.sleep(1.05 - time.time() % 1)  # into the next second of the PDF's timestamp clock
    second = svg_to_pdf(browser_session, SVG)
    assert first.startswith(b"%PDF-")
    assert first == second
