from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError

from mermaid_arch.browser import BrowserSession
from mermaid_arch.models import Engine, RenderOptions, Source
from mermaid_arch.pipeline import render_source


class CrashedChromium:
    """Stands in for a BrowserSession whose Chromium dies on every use."""

    def __getattr__(self, name: str):
        def crash(*args, **kwargs):
            raise PlaywrightError("Target page, context or browser has been closed\nCall log:")

        return crash


def test_browser_failure_fails_the_source_and_keeps_previous_outputs(tmp_path: Path) -> None:
    source = tmp_path / "diagram.d2"
    source.write_text("a -> b")
    targets = {"svg": tmp_path / "diagram.svg", "png": tmp_path / "diagram.png"}
    for target in targets.values():
        target.write_bytes(b"previous")
    result = render_source(Source(source), targets, RenderOptions(), CrashedChromium())
    assert [(d.code, d.message) for d in result.diagnostics] == [
        (
            "browser/failed",
            "Chromium failed while rendering: Target page, context or browser has been closed",
        )
    ]
    assert result.outputs == ()
    assert [target.read_bytes() for target in targets.values()] == [b"previous", b"previous"]


def test_diagnostic_lines_count_from_the_source_line(tmp_path: Path) -> None:
    source = Source(tmp_path / "notes.md", Engine.D2, "a -> b\nc ->\n", line=10, block=2)
    with BrowserSession() as session:
        result = render_source(source, {}, RenderOptions(), session)
    assert [(d.code, d.line) for d in result.diagnostics] == [("d2/compile", 11)]
    assert result.to_json()["block"] == 2


@pytest.mark.browser
def test_mermaid_svg_ids_follow_output_names_and_fonts_are_reported(
    browser_session, tmp_path: Path
) -> None:
    ids = []
    for name in ("first.mmd", "Second Draft.mmd"):
        path = tmp_path / name
        path.write_text("flowchart TB\n a --> b")
        target = path.with_suffix(".svg")
        result = render_source(Source(path), {"svg": target}, RenderOptions(), browser_session)
        assert result.ok, result.diagnostics
        assert result.to_json()["fonts"]
        ids.append(ET.parse(target).getroot().get("id"))
    assert ids == ["mermaid-arch-first", "mermaid-arch-second-draft"]
