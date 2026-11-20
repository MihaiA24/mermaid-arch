from __future__ import annotations

import json
import math
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

from mermaid_arch.browser import BrowserSession, svg_to_pdf, svg_to_png
from mermaid_arch.checks import check_svg, svg_size
from mermaid_arch.mermaid import merge_config, render_mermaid
from mermaid_arch.models import DiagramError, Engine, RenderOptions, Source
from mermaid_arch.pipeline import Result, render_source


@pytest.mark.parametrize(
    ("svg", "code"),
    [
        ("broken", "svg/malformed"),
        ('<svg viewBox="0 0 20 0"><path d="M0 0"/></svg>', "svg/empty"),
        ('<svg viewBox="0 0 20 20"><defs><path d="M0 0"/></defs></svg>', "svg/empty"),
        ('<svg viewBox="0 0 500 2500"><rect width="10" height="100"/></svg>', "svg/aspect-ratio"),
    ],
)
def test_svg_checks(svg: str, code: str) -> None:
    assert [d.code for d in check_svg(svg)] == [code]


@pytest.mark.parametrize("box", ["0 0 50 228", "0 0 1264 148"])
def test_lopsided_diagram_that_fits_a_screen_is_not_flagged(box: str) -> None:
    assert check_svg(f'<svg viewBox="{box}"><rect width="10" height="10"/></svg>') == []


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
                "aliases": {"person": {"parent": "account"}, "member": {"parent": "person"}},
            }
        )
    )


@pytest.mark.parametrize(
    ("source", "suggestion"),
    [
        ('flowchart TB\n a@{ icon: "test:acount" }', "test:account"),
        ("flowchart TB\n a@{ icon: test:acount }", "test:account"),
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
def test_label_icon_keeps_its_colours_unless_monochrome(browser_session, icons) -> None:
    source = 'flowchart TB\n a["fas:fa-member Customer"]'  # member -> person -> account

    def icon_path(**options) -> ET.Element:
        svg = render_mermaid(browser_session, source, icon_packs={"fas": "test"}, **options).svg
        [icon] = [e for e in ET.fromstring(svg).iter() if e.get("class") == "label-icon"]
        return next(e for e in icon.iter() if e.get("d"))

    coloured = icon_path()
    assert coloured.get("d") == "M2 2h20v20H2z"
    assert "url(" in coloured.get("style", "")
    assert "style" not in icon_path(monochrome_icons=True).attrib


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
    on_preset = render_mermaid(browser_session, own, config=config, preset="archify")
    assert "stroke-width:9.25px" in on_preset.svg
    assert "--blue-fill" in on_preset.svg
    assert "stroke-width:7.25px" not in on_preset.svg


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
def test_svg_id_names_the_root_and_scopes_its_css(browser_session) -> None:
    source = "---\nconfig:\n  themeCSS: ':root { --paper: 7; }'\n---\nflowchart TB\n a --> b"
    light = render_mermaid(browser_session, source, svg_id="light").svg
    dark = render_mermaid(browser_session, source, svg_id="dark").svg
    assert (ET.fromstring(light).get("id"), ET.fromstring(dark).get("id")) == ("light", "dark")
    assert "#light{--paper:7;}" in light
    assert "#dark{--paper:7;}" in dark
    assert "#light" not in dark


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
def test_legend_draws_a_key_and_names_a_role_no_element_has(browser_session) -> None:
    graph = "flowchart LR\n a --> b\n class a app"
    legend = "flowchart LR\n %% legend: app Service | data Store\n" + graph.split("\n", 1)[1]
    plain = ET.fromstring(render_mermaid(browser_session, graph).svg)
    result = render_mermaid(browser_session, legend)
    keyed = ET.fromstring(result.svg)
    [key] = [e for e in keyed.iter() if e.get("class") == "legend"]
    assert "".join(key.itertext()).split() == ["Service", "Store"]
    height = float(plain.get("viewBox").split()[3])
    assert float(keyed.get("viewBox").split()[3]) > height
    [diagnostic] = result.warnings
    assert (diagnostic.code, diagnostic.line) == ("mermaid/legend-role-unused", 2)
    assert "'data'" in diagnostic.message


def test_malformed_legend_entry_is_rejected_with_its_line() -> None:
    source = "flowchart LR\n %% legend: app Service | Store\n a --> b"
    with BrowserSession() as session, pytest.raises(DiagramError) as caught:
        render_mermaid(session, source)
    [diagnostic] = caught.value.diagnostics
    assert (diagnostic.code, diagnostic.line) == ("mermaid/legend", 2)


def test_config_merges_over_a_preset_key_by_key() -> None:
    preset = {"flowchart": {"padding": 20, "nodeSpacing": 64}, "themeCSS": ".a {}"}
    config = {"flowchart": {"padding": 8}, "themeCSS": ".b {}", "theme": "dark"}
    assert merge_config(preset, config) == {
        "flowchart": {"padding": 8, "nodeSpacing": 64},
        "themeCSS": ".a {}\n.b {}",
        "theme": "dark",
    }


def _font(path: Path) -> Path:
    """A TrueType font, family TestFace, with a wide square for every letter a to z."""
    letters = [chr(code) for code in range(ord("a"), ord("z") + 1)]
    names = [".notdef", *letters]
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(names)
    builder.setupCharacterMap({ord(letter): letter for letter in letters})
    glyphs = {}
    for name in names:
        pen = TTGlyphPen(None)
        pen.moveTo((0, 0))
        pen.lineTo((0, 700))
        pen.lineTo((900, 700))
        pen.lineTo((900, 0))
        pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (1000, 0) for name in names})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "TestFace", "styleName": "Regular"})
    builder.setupOS2()
    builder.setupPost()
    builder.save(path)
    return path


@pytest.mark.browser
def test_font_lays_text_out_and_travels_inside_the_svg(browser_session, tmp_path: Path) -> None:
    font = _font(tmp_path / "test.ttf")
    graph = "flowchart LR\n a[abcdefghijklmnopqrstuvwxyz] --> b[label]"  # wider than the minimum
    config = {"fontFamily": '"TestFace"', "themeVariables": {"fontFamily": '"TestFace"'}}
    first = render_mermaid(browser_session, graph, config=config, font_files=[font])
    assert '@font-face{font-family:"TestFace"' in first.svg
    assert "data:font/woff2;base64," in first.svg
    assert not first.warnings
    assert render_mermaid(browser_session, graph, config=config, font_files=[font]) == first
    fallback = render_mermaid(browser_session, graph, config=config)
    assert ET.fromstring(first.svg).get("viewBox") != ET.fromstring(fallback.svg).get("viewBox")
    unnamed = render_mermaid(browser_session, graph, font_files=[font])
    assert [warning.code for warning in unnamed.warnings] == ["mermaid/font-unused"]
    assert "@font-face" not in unnamed.svg


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


# One small diagram of every type the bundled Mermaid detects.
EVERY_TYPE = {
    "flowchart": "flowchart TB\n a --> b\n b --> c",
    "sequence": "sequenceDiagram\n Alice->>Bob: hi\n Bob-->>Alice: yo",
    "class": "classDiagram\n Animal <|-- Duck",
    "state": "stateDiagram-v2\n [*] --> Idle\n Idle --> Busy\n Busy --> [*]",
    "er": "erDiagram\n CUSTOMER ||--o{ ORDER : places",
    "journey": "journey\n title Day\n section Work\n  Code: 5: Me",
    "gantt": "gantt\n dateFormat YYYY-MM-DD\n todayMarker off\n Task :a1, 2024-01-01, 3d",
    "pie": 'pie\n "Dogs" : 3\n "Cats" : 1',
    "quadrant": "quadrantChart\n x-axis Low --> High\n y-axis Low --> High\n A: [0.3, 0.6]",
    "requirement": (
        "requirementDiagram\n requirement r1 {\n id: 1\n text: must\n risk: high\n"
        " verifymethod: test\n }\n element e1 {\n type: sim\n }\n e1 - satisfies -> r1"
    ),
    "gitGraph": "gitGraph\n commit\n branch develop\n commit\n checkout main\n merge develop",
    "c4": 'C4Context\n Person(a, "User")\n System(b, "System")\n Rel(a, b, "Uses")',
    "mindmap": "mindmap\n root((Root))\n  A\n  B",
    "timeline": "timeline\n title History\n 2020 : A\n 2021 : B",
    "sankey": "sankey-beta\nA,B,10\nA,C,5",
    "xychart": 'xychart-beta\n x-axis [a, b, c]\n y-axis "v" 0 --> 10\n bar [1, 5, 9]',
    "block": "block-beta\n columns 2\n a b\n a --> b",
    "packet": 'packet-beta\n 0-15: "Source"\n 16-31: "Destination"',
    "kanban": "kanban\n todo[Todo]\n  t1[Task]\n done[Done]\n  t2[Other]",
    "architecture": (
        "architecture-beta\n group api(cloud)[API]\n service db(database)[Database] in api\n"
        " service server(server)[Server] in api\n db:L -- R:server"
    ),
    "radar": 'radar-beta\n axis a["A"], b["B"], c["C"]\n curve x["X"]{1, 2, 3}',
    "treemap": 'treemap-beta\n"Root"\n    "A": 10\n    "B": 20',
    "info": "info",
    "ishikawa": "ishikawa-beta\n    Late\n    Process\n        Queue\n    People\n        Training",
    "usecase": 'usecase-beta\nactor User("User")\nLogin("Log in")\nUser --> Login',
    "eventmodeling": (
        "eventmodeling\n\ntf 01 ui ShopUI\ntf 02 cmd AddItem\ntf 03 evt ItemAdded\n"
        "tf 04 rmo CartView ->> 03"
    ),
    "venn": 'venn-beta\n set A\n set B\n union A,B["Both"]',
    "treeView": (
        'treeView-beta\n    "Drive"\n        "Reports"\n            "Q1.pdf"\n        "Notes"'
    ),
    "wardley": (
        "wardley-beta\ntitle Tea\nanchor Business [0.95, 0.63]\ncomponent Cup [0.73, 0.78]\n"
        "component Kettle [0.43, 0.35]\nBusiness -> Cup\nCup -> Kettle\nevolve Kettle 0.62"
    ),
    "cynefin": (
        'cynefin-beta\n title Incidents\n complex\n  "Investigate"\n clear\n  "Restart"\n'
        ' complex --> clear : "Known"'
    ),
    "railroad": 'railroad-beta\n digit = choice(terminal("0"), terminal("1")) ;',
    "railroad-ebnf": 'railroad-ebnf-beta\n number = digit+ ;\n digit = "0" | "1" ;',
    "railroad-abnf": "railroad-abnf-beta\n label = 1*( ALPHA / DIGIT ) ;",
    "railroad-peg": 'railroad-peg-beta\n Number <- Digit+ ;\n Digit <- "0" / "1" ;',
    "swimlane": (
        "swimlane-beta LR\n subgraph Customer\n  request[Request]\n end\n subgraph Support\n"
        "  answer[Answer]\n end\n request --> answer"
    ),
    "agentflow": (
        'agentflow-beta TB\n flow reviewer["Review"]\n  changes["Gather"]@{ shape: input }\n'
        '  analyse["Analyse"]@{ shape: task }\n  changes --> analyse\n end'
    ),
}


@pytest.mark.browser
def test_render_is_the_same_whatever_rendered_before(
    browser_session, icons, tmp_path: Path
) -> None:
    pinned = {"prefix": "test", "icons": {"account": {"body": '<path d="M1 1h2v2H1z"/>'}}}
    (tmp_path / "icons" / "test@1.2.3.json").write_text(json.dumps(pinned))
    graph = "flowchart TB\n a --> b"
    icon = 'flowchart TB\n a@{ icon: "test:account", label: "A" }'
    shared = {"theme": "forest", "layout": "elk", "themeCSS": ".node rect { stroke-width: 7px; }"}
    face = {"fontFamily": '"TestFace"', "themeVariables": {"fontFamily": '"TestFace"'}}
    renders: dict[str, tuple[str, dict]] = {
        **{name: (source, {}) for name, source in EVERY_TYPE.items()},
        "dark theme": (graph, {"theme": "dark"}),
        "plain": (graph, {}),
        "shared config": (graph, {"config": shared}),
        "frontmatter": ("---\nconfig:\n  theme: neutral\n  look: handDrawn\n---\n" + graph, {}),
        "icon": (icon, {}),
        "same icon from a pinned pack": (icon, {"icon_packs": {"test": "test@1.2.3"}}),
        "preset": (graph, {"preset": "archify"}),
        "legend": ("flowchart TB\n %% legend: app Service\n a --> b\n class a app", {}),
        "font": (graph, {"font_files": [_font(tmp_path / "test.ttf")], "config": face}),
    }

    def sweep(names) -> dict[str, str]:
        return {
            name: render_mermaid(browser_session, renders[name][0], **renders[name][1]).svg
            for name in names
        }

    first = sweep(renders)
    second = sweep(reversed(renders))
    assert [name for name in renders if first[name] != second[name]] == []


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
    result = render_source(Source(path), targets, RenderOptions(), browser_session)
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


@pytest.mark.browser
def test_text_over_mermaid_limit_is_refused(browser_session) -> None:
    line = "  Alice->>Bob: " + "payload " * 10 + "\n"
    under = "sequenceDiagram\n" + line * 520  # 49936 characters
    assert render_mermaid(browser_session, under).svg
    with pytest.raises(DiagramError) as caught:
        render_mermaid(browser_session, under + line)
    [diagnostic] = caught.value.diagnostics
    assert diagnostic.code == "mermaid/too-large"
    assert "50032" in diagnostic.message
    assert "50000" in diagnostic.message


@pytest.mark.browser
def test_only_a_bundled_theme_renders(browser_session) -> None:
    graph = "flowchart TB\n a --> b"
    assert render_mermaid(browser_session, graph, theme="forest").svg
    for source, theme in [(graph, "nope"), ("---\nconfig:\n  theme: nope\n---\n" + graph, None)]:
        with pytest.raises(DiagramError) as caught:
            render_mermaid(browser_session, source, theme=theme)
        [diagnostic] = caught.value.diagnostics
        assert (diagnostic.code, diagnostic.severity) == ("mermaid/theme-unknown", "error")
        assert "'nope'" in diagnostic.message
        assert "forest" in diagnostic.hint


@pytest.mark.browser
def test_git_graph_renders_the_same_every_time(browser_session) -> None:
    source = "gitGraph\n commit\n commit\n branch develop\n commit\n checkout main\n merge develop"
    first = render_mermaid(browser_session, source).svg
    assert render_mermaid(browser_session, source).svg == first


@pytest.mark.browser
def test_render_past_the_deadline_times_out_and_the_session_recovers(
    browser_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = "flowchart TB\n a --> b"
    monkeypatch.setattr("mermaid_arch.mermaid.MERMAID_TIMEOUT_SECONDS", 0.001)
    with pytest.raises(DiagramError) as caught:
        render_mermaid(browser_session, graph)
    assert [d.code for d in caught.value.diagnostics] == ["mermaid/timeout"]
    monkeypatch.undo()
    assert render_mermaid(browser_session, graph).svg


@pytest.mark.browser
def test_fonts_name_what_drew_the_labels(browser_session) -> None:
    result = render_mermaid(browser_session, "flowchart TB\n a[Start] --> b[Stop]")
    assert isinstance(result.fonts, tuple)
    assert result.fonts
    assert all(isinstance(font, str) and font for font in result.fonts)
    assert list(result.fonts) == sorted(set(result.fonts))
    assert "data-mermaid-arch-font" not in result.svg
