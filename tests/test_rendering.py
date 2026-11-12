from __future__ import annotations

import json
import math
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from mermaid_arch.browser import BrowserSession, svg_to_pdf, svg_to_png
from mermaid_arch.checks import check_svg, svg_size
from mermaid_arch.mermaid import render_mermaid
from mermaid_arch.models import DiagramError, Engine, RenderOptions
from mermaid_arch.pipeline import Result, render_source


@pytest.mark.parametrize(
    ("svg", "code"),
    [
        ("broken", "svg/malformed"),
        ('<svg viewBox="0 0 20 0"><path d="M0 0"/></svg>', "svg/empty"),
        ('<svg viewBox="0 0 20 20"><defs><path d="M0 0"/></defs></svg>', "svg/empty"),
        ('<svg viewBox="0 0 10 100"><rect width="10" height="100"/></svg>', "svg/aspect-ratio"),
    ],
)
def test_svg_checks(svg: str, code: str) -> None:
    assert [d.code for d in check_svg(svg)] == [code]


@pytest.mark.browser
@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
def test_raster_preserves_fractional_viewbox_size_and_alpha(browser_session, scale: float) -> None:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100%" viewBox="4 -49 101.3 75.15">'
        '<rect x="8" y="0" width="5" height="5" fill="red"/></svg>'
    )
    png = svg_to_png(browser_session, svg, scale=scale, background="transparent")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", png[16:24]) == (math.ceil(101.3 * scale), math.ceil(75.15 * scale))
    assert png[25] == 6  # RGBA, not a flattened white canvas.


@pytest.mark.browser
def test_pdf_renders_diagram(browser_session) -> None:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="4 -49 100 150">'
        '<rect x="10" y="0" width="30" height="30"/></svg>'
    )
    assert svg_to_pdf(browser_session, svg).startswith(b"%PDF-")


def test_oversize_png_fails_before_browser_launch() -> None:
    svg = '<svg viewBox="0 0 9000 100"><rect width="9000" height="100"/></svg>'
    with BrowserSession() as session, pytest.raises(DiagramError) as caught:
        svg_to_png(session, svg)
    assert caught.value.diagnostics[0].code == "png/too-large"
    assert "--scale" in caught.value.diagnostics[0].hint


@pytest.fixture
def icons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERMAID_ARCH_CACHE", str(tmp_path))
    path = tmp_path / "icons"
    path.mkdir()
    (path / "test.json").write_text(
        json.dumps(
            {
                "prefix": "test",
                "width": 24,
                "height": 24,
                "lastModified": 1700000000,
                "icons": {
                    "account": {
                        "body": (
                            '<defs><linearGradient id="paint"><stop stop-color="red"/>'
                            '<stop offset="1" stop-color="blue"/></linearGradient></defs>'
                            '<path fill="url(#paint)" d="M2 2h20v20H2z"/>'
                        )
                    }
                },
                "aliases": {"person": {"parent": "account"}},
            }
        )
    )


@pytest.mark.parametrize(
    ("source", "suggestion"),
    [
        ('flowchart TB\n a@{ icon: "test:acount" }', "test:account"),
        ("architecture-beta\n service db(test:acount)[Database]", "test:account"),
        ('flowchart TB\n a["fas:fa-acount Customer"]', "fas:fa-account"),
    ],
)
def test_unknown_icon_is_rejected_with_line_and_suggestion(
    icons, source: str, suggestion: str
) -> None:
    with BrowserSession() as session, pytest.raises(DiagramError) as caught:
        render_mermaid(session, source, icon_packs={"fas": "test"})
    [diagnostic] = caught.value.diagnostics
    assert (diagnostic.code, diagnostic.line) == ("mermaid/icon-unknown", 2)
    assert suggestion in diagnostic.hint


@pytest.mark.browser
def test_label_icon_draws_from_the_pack_served_under_its_name(browser_session, icons) -> None:
    source = 'flowchart TB\n a["fas:fa-person Customer"]'
    assert (
        "M2 2h20v20H2z" in render_mermaid(browser_session, source, icon_packs={"fas": "test"}).svg
    )


@pytest.mark.browser
def test_parenthesised_label_text_is_not_an_icon(browser_session, icons) -> None:
    source = 'flowchart LR\n api["Graph API (localhost:5050)"] --> db["Neo4j (bolt:7687)"]'
    assert "(localhost:5050)" in render_mermaid(browser_session, source).svg


@pytest.mark.browser
def test_mermaid_html_labels_icons_and_determinism(browser_session, icons) -> None:
    source = (
        'flowchart TB\n a@{ icon: "test:person", form: "rounded", '
        'label: "Person<br/>customer" } --> b[API]'
    )
    first = render_mermaid(browser_session, source)
    second = render_mermaid(browser_session, source)
    root = ET.fromstring(first.svg)
    assert root.tag.endswith("}svg")
    assert root.get("viewBox")
    assert "customer" in "".join(root.itertext())
    assert "<path" in first.svg
    assert first.svg == second.svg
    assert not first.warnings
    assert first.icon_packs == {"test": 1700000000}
    assert any(
        line.startswith("<foreignObject") and line.endswith("</foreignObject>")
        for line in first.svg.splitlines()
    )
    assert render_mermaid(browser_session, source, theme="dark").svg != first.svg


@pytest.mark.browser
@pytest.mark.parametrize(
    "prefix", ["", "---\ntitle: Test\n---\n", '%%{init: {"theme":"dark"}}%%\n', "%% comment\n\n"]
)
def test_parse_error_reports_original_file_line(browser_session, prefix: str) -> None:
    source = prefix + "flowchart TB\n a --> \n b -->> c ]]"
    with pytest.raises(DiagramError) as caught:
        render_mermaid(browser_session, source)
    [diagnostic] = caught.value.diagnostics
    assert diagnostic.code == "mermaid/parse"
    assert diagnostic.line == source.count("\n") + 1


@pytest.mark.browser
def test_bundled_elk_lays_out_and_an_unknown_layout_is_disclosed(browser_session) -> None:
    graph = "flowchart TB\n a --> b\n a --> c\n b --> d\n c --> d\n a --> d"
    dagre = render_mermaid(browser_session, graph)
    elk = render_mermaid(browser_session, "---\nconfig:\n  layout: elk\n---\n" + graph)
    unknown = render_mermaid(browser_session, "---\nconfig:\n  layout: nope\n---\n" + graph)
    assert not elk.warnings
    assert elk.svg != dagre.svg
    assert [d.code for d in unknown.warnings] == ["mermaid/layout-unsupported"]
    assert unknown.svg == dagre.svg


@pytest.mark.browser
def test_shared_config_sits_under_frontmatter_and_pins_the_theme(browser_session) -> None:
    config = {"theme": "base", "themeCSS": ".node > rect { stroke-width: 7.25px; }"}
    graph = "flowchart TB\n a --> b"
    shared = render_mermaid(browser_session, graph, config=config)
    assert "stroke-width:7.25px" in shared.svg
    assert render_mermaid(browser_session, graph, theme="dark", config=config).svg == shared.svg
    own = "---\nconfig:\n  themeCSS: '.node rect { stroke-width: 9.25px; }'\n---\n" + graph
    overridden = render_mermaid(browser_session, own, config=config)
    assert "stroke-width:9.25px" in overridden.svg
    assert "stroke-width:7.25px" not in overridden.svg
    assert not overridden.warnings


@pytest.mark.browser
def test_theme_css_discarded_by_mermaid_is_disclosed(browser_session) -> None:
    source = (
        "---\nconfig:\n  themeCSS: '.node > rect { stroke-width: 7.25px; }'\n---\n"
        "flowchart TB\n a --> b"
    )
    [diagnostic] = render_mermaid(browser_session, source).warnings
    assert (diagnostic.code, diagnostic.line) == ("mermaid/theme-css-dropped", 3)


@pytest.mark.browser
def test_theme_css_root_addresses_the_svg(browser_session) -> None:
    source = "---\nconfig:\n  themeCSS: ':root { --paper: 7; }'\n---\nflowchart TB\n a --> b"
    assert "#mermaid-arch{--paper:7;}" in render_mermaid(browser_session, source).svg


@pytest.mark.browser
def test_named_edge_path_carries_its_class(browser_session) -> None:
    source = "flowchart LR\n a read@--> b\n a --> c\n class read data"
    root = ET.fromstring(render_mermaid(browser_session, source).svg)
    classes = {
        path.get("data-id"): path.get("class", "").split()
        for path in root.iter("{http://www.w3.org/2000/svg}path")
        if path.get("data-edge")
    }
    assert [edge for edge, names in classes.items() if "data" in names] == ["read"]


@pytest.mark.browser
def test_pinned_pack_is_read_from_its_own_cache_file(
    browser_session, icons, tmp_path: Path
) -> None:
    pinned = {
        "prefix": "test",
        "width": 24,
        "height": 24,
        "lastModified": 1600000000,
        "icons": {"retired": {"body": '<path d="M1 1h2v2H1z"/>'}},
    }
    (tmp_path / "icons" / "test@1.2.3.json").write_text(json.dumps(pinned))
    source = 'flowchart TB\n a@{ icon: "test:retired", label: "Old" }'
    result = render_mermaid(browser_session, source, icon_packs={"test": "test@1.2.3"})
    assert "M1 1h2v2H1z" in result.svg
    assert result.icon_packs == {"test@1.2.3": 1600000000}


def test_receipt_separates_a_pack_from_its_pin() -> None:
    result = Result(Path("a.mmd"), Engine.MERMAID, (), (), {"mdi@1.2.3": 5, "logos": None})
    assert result.to_json()["icon_packs"] == [
        {"prefix": "logos", "version": None, "last_modified": None},
        {"prefix": "mdi", "version": "1.2.3", "last_modified": 5},
    ]


@pytest.mark.browser
def test_page_blocks_unlisted_and_external_requests(browser_session) -> None:
    with browser_session.page({"/": "<html></html>", "/ok.json": '{"ok":true}'}) as page:
        result = page.evaluate("""async () => {
          const read = async url => {
            try {return await (await fetch(url)).json()} catch {return 'blocked'}
          };
          return await Promise.all(['/ok.json','/missing.json','https://example.com/leak'].map(read));
        }""")
        assert result == [{"ok": True}, "blocked", "blocked"]


@pytest.mark.browser
@pytest.mark.parametrize("suffix,source", [(".d2", "a -> b"), (".mmd", "flowchart TB\n a --> b")])
def test_pipeline_converts_each_format(
    browser_session, tmp_path: Path, suffix: str, source: str
) -> None:
    path = tmp_path / f"diagram{suffix}"
    path.write_text(source)
    targets = {fmt: tmp_path / f"diagram.{fmt}" for fmt in ("svg", "png", "pdf", "html")}
    result = render_source(path, targets, RenderOptions(), browser_session)
    assert result.ok, result.diagnostics
    assert svg_size(targets["svg"].read_text())[0] > 0
    assert targets["png"].read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert targets["pdf"].read_bytes().startswith(b"%PDF-")
    assert "<!doctype html>" in targets["html"].read_text()


@pytest.mark.browser
def test_frontmatter_theme_wins_without_inheriting_cli_palette(browser_session, icons) -> None:
    source = (
        "---\nconfig:\n  theme: base\n---\nflowchart TB\n"
        ' a@{ icon: "test:account", form: "rounded", label: "Customer" } --> b'
    )
    light = render_mermaid(browser_session, source).svg
    assert render_mermaid(browser_session, source, theme="dark").svg == light
