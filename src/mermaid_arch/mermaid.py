"""Pinned, local Mermaid renderer with checked Iconify references."""

from __future__ import annotations

import difflib
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from mermaid_arch import __version__
from mermaid_arch.browser import BrowserSession
from mermaid_arch.models import Diagnostic, DiagramError, SvgResult, error, warning

MERMAID_VERSION = "12.3.0"
# A layout can spin Chromium's main thread for good; the deadline stops a batch hanging on it.
MERMAID_TIMEOUT_SECONDS = 180
# Layouts the bundled build registers; Mermaid lays any other name out with dagre.
BUNDLED_LAYOUTS = (
    "dagre",
    "elk",
    "elk.stress",
    "elk.force",
    "elk.mrtree",
    "elk.sporeOverlap",
    "elk.box",
    "elk.rectpacking",
    "swimlane",
    "cose-bilkent",
)
# Themes the bundled build registers, plus `null`, which Mermaid documents as "no theme".
# Mermaid renders any other name with the default palette.
BUNDLED_THEMES = (
    "default",
    "base",
    "dark",
    "forest",
    "neutral",
    "neo",
    "neo-dark",
    "redux",
    "redux-color",
    "redux-dark",
    "redux-dark-color",
    "null",
)
# Mermaid draws `fas:fa-name` in label text from a pack registered as `fas`. These Iconify
# packs stand behind Font Awesome's style prefixes; `fa` is an Iconify pack in its own right.
FONT_AWESOME_PACKS = {"fas": "fa6-solid", "far": "fa6-regular", "fab": "fa6-brands"}
_ICON = re.compile(
    r"""\bicon\s*:\s*["'](?P<shape>[a-z0-9-]+:[a-z0-9-]+)["']"""
    r"|^\s*(?:service|group)\s+[\w-]+\((?P<arch>[a-z0-9-]+:[a-z0-9-]+)\)"
    r"|\b(?P<label>fa[bklrs]?:fa-[\w-]+)"
)
_FRONTMATTER = re.compile(r"\A\s*---[^\S\n]*\n.*?\n---[^\S\n]*(?:\n|$)", re.S)
_DIRECTIVE = re.compile(r"%%\{.*?\}%%", re.S)
_COMMENT = re.compile(r"^[ \t]*%%[^\n]*(?:\n|$)", re.M)
_THEME_CSS = re.compile(r"""(?:^|[\s{,"'])themeCSS["']?\s*:""", re.M)
# One element per line keeps a checked-in SVG diffable. Labels stay whole: whitespace between
# their inline elements would be drawn.
_ELEMENT_BREAK = re.compile(r"(<(foreignObject|text)\b.*?</\2(?=>))|>(?=<)", re.S)
_HTML = """<!doctype html><html><head><meta charset="utf-8"></head>
<body><div id="container"></div><script src="/mermaid.js"></script></body></html>"""
_FONT_TAG = "data-mermaid-arch-font"
_RENDER = """args => {
  // Start the render after this call returns, so that Python can wait for it with a deadline
  // that holds even while a layout never yields the main thread.
  setTimeout(async () => { window.mermaidArchResult = await render(args); }, 0);

  async function render({source, id, theme, themes, site, packs, fontTag}) {
    try {
      // gitGraph commit ids and some layouts draw on Math.random: seed it for repeatable output.
      let seed = 1;
      Math.random = () => {
        seed = (seed + 0x6d2b79f5) | 0;
        let t = Math.imul(seed ^ (seed >>> 15), seed | 1);
        t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
      };
      // The shared --config file overrides the defaults above it, never the settings below it.
      const config = {
        handDrawnSeed: 1, layout: 'dagre', ...site,
        startOnLoad: false, securityLevel: 'strict', suppressErrorRendering: true,
        deterministicIds: true, deterministicIDSeed: 'mermaid-arch'
      };
      if (theme !== null && site.theme === undefined) config.theme = theme;
      // initialize() swaps an unknown theme for the default, so check the name asked for;
      // a theme from frontmatter or a directive reaches the config unchecked.
      const requested = config.theme ?? 'default';
      mermaid.initialize(config);
      mermaid.registerIconPacks(packs.map(name => ({
        name, loader: () => fetch('/icons/' + name + '.json').then(r => r.json())
      })));
      const parsed = await mermaid.parse(source);
      const named = parsed.config?.theme ?? requested;
      if (!themes.includes(named)) return {ok: false, theme: String(named)};
      const effective = mermaid.mermaidAPI.getConfig();
      const layout = effective.layout;
      // Frontmatter can replace the requested theme, but initialize() has already expanded
      // that theme's palette. Reinitialize from the effective theme to avoid mixed palettes.
      if (effective.theme !== (config.theme || 'default')) {
        config.theme = effective.theme;
        mermaid.initialize(config);
      }
      const {svg} = await mermaid.render(id, source);
      const container = document.getElementById('container');
      container.innerHTML = svg;
      const root = container.querySelector(':scope > svg');
      if (!root || root.querySelector('.error-icon, .error-text')) {
        return {ok: false, message: 'Mermaid returned an error graphic'};
      }
      // Over maxTextSize, render() draws a placeholder where parse() raised nothing. Mermaid
      // measures the source without frontmatter, directives and comments, which only shortens
      // it: a source within the limit is never swapped, whatever its labels say.
      const limit = effective.maxTextSize ?? 50000;
      const labels = [...root.querySelectorAll('.node')].map(node => node.textContent);
      if (source.length > limit && labels.join() === 'Maximum text size in diagram exceeded') {
        return {ok: false, limit};
      }
      // Iconify randomizes gradient/clip IDs independently of Mermaid's deterministic seed.
      // Canonicalize only those IDs and their references, preserving the authored geometry.
      const ids = new Map([...root.querySelectorAll('[id^="IconifyId"]')]
        .map((element, index) => [element.id, `iconify-${index}`]));
      const canonical = value => value.replace(/IconifyId[0-9a-zA-Z_-]+/g, k => ids.get(k) || k);
      for (const element of root.querySelectorAll('*')) {
        for (const attribute of [...element.attributes]) {
          const value = canonical(attribute.value);
          if (value !== attribute.value) attribute.value = value;
        }
        // Mermaid nests themeCSS under the root's id, which leaves `:root` matching nothing.
        // Point it at the root, so a theme can paint the canvas and declare variables there.
        if (element.localName === 'style') {
          element.textContent = canonical(element.textContent)
            .replaceAll(`#${id} :root`, `#${id}`);
        }
      }
      // Mermaid applies a classDef to a named edge but leaves the class name off its path;
      // with the name there, shared CSS can colour edges by role.
      const diagram = await mermaid.mermaidAPI.getDiagramFromText(source);
      for (const edge of diagram.db.getEdges?.() ?? []) {
        if (!edge.id || !edge.classes?.length) continue;
        const path = root.querySelector(`path[data-edge="true"][data-id="${CSS.escape(edge.id)}"]`);
        if (path) path.classList.add(...edge.classes);
      }
      await document.fonts.ready;
      const serialized = new XMLSerializer().serializeToString(root);
      // Mark one text element per font stack, after serializing: Python asks Chromium which
      // installed fonts drew each one.
      const stacks = new Set();
      for (const element of root.querySelectorAll('text, text *, foreignObject *')) {
        const family = getComputedStyle(element).fontFamily;
        const text = [...element.childNodes]
          .some(node => node.nodeType === Node.TEXT_NODE && node.data.trim());
        if (text && !stacks.has(family)) {
          stacks.add(family);
          element.setAttribute(fontTag, '');
        }
      }
      return {ok: true, svg: serialized, layout, themeCSS: effective.themeCSS ?? null};
    } catch (e) {
      return {ok: false, message: String(e.message || e), line: e.hash?.loc?.first_line};
    }
  }
}"""


def icon_cache_dir() -> Path:
    root = os.environ.get("MERMAID_ARCH_CACHE")
    if root is None:
        root = str(Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "mermaid-arch")
    return Path(root).expanduser() / "icons"


def render_mermaid(
    session: BrowserSession,
    source: str,
    *,
    theme: str | None = None,
    config: Mapping[str, object] | None = None,
    icon_packs: Mapping[str, str] | None = None,
    svg_id: str = "mermaid-arch",
) -> SvgResult:
    """Render `source`; `config` is shared Mermaid config that frontmatter overrides per key.

    `icon_packs` maps a pack name used in the source to the Iconify pack that serves it,
    written `prefix` or, pinned to a release, `prefix@version`. `svg_id`, matching
    `[a-z][a-z0-9-]*`, is the SVG root's id, which scopes its CSS: SVGs that share a page
    need distinct ones.
    """
    served = {**FONT_AWESOME_PACKS, **(icon_packs or {})}
    references: dict[str, list[tuple[str, str, int]]] = {}
    for number, line in enumerate(source.splitlines(), 1):
        if line.lstrip().startswith("%%"):
            continue
        for match in _ICON.finditer(line):
            written = match["shape"] or match["arch"] or match["label"]
            name, icon = written.split(":", 1)
            if match["label"]:
                icon = icon.removeprefix("fa-")
            references.setdefault(name, []).append((written, icon, number))

    routes: dict[str, bytes | str | Path] = {
        "/": _HTML,
        "/mermaid.js": Path(str(files("mermaid_arch") / "assets" / "mermaid.min.js")),
    }
    problems: list[Diagnostic] = []
    modified: dict[str, int | None] = {}
    for name, uses in references.items():
        spec = served.get(name, name)
        try:
            path, pack = _icon_pack(spec)
        except DiagramError as exc:
            problems.extend(
                error(d.code, d.message, line=uses[0][2], hint=d.hint) for d in exc.diagnostics
            )
            continue
        names = set(pack.get("icons", {})) | set(pack.get("aliases", {}))
        for written, icon, line in uses:
            if icon not in names:
                close = difflib.get_close_matches(icon, sorted(names), n=3)
                stem = written[: len(written) - len(icon)]
                hint = (
                    "try " + ", ".join(stem + candidate for candidate in close)
                    if close
                    else f"choose an icon listed in {path}"
                )
                problems.append(
                    error("mermaid/icon-unknown", f"unknown icon {written}", line=line, hint=hint)
                )
        routes[f"/icons/{name}.json"] = path
        stamp = pack.get("lastModified")
        modified[spec] = stamp if isinstance(stamp, int) else None
    if problems:
        raise DiagramError(*problems)

    # A fresh page per render: Mermaid keeps id counters in module state that page code cannot
    # reset, so on a reused page a diagram's ids would depend on what rendered before it.
    try:
        with session.page(routes) as page:
            page.evaluate(
                _RENDER,
                {
                    "source": source,
                    "id": svg_id,
                    "theme": theme,
                    "themes": list(BUNDLED_THEMES),
                    "site": dict(config or {}),
                    "packs": list(references),
                    "fontTag": _FONT_TAG,
                },
            )
            result = page.wait_for_function(
                "() => window.mermaidArchResult", timeout=MERMAID_TIMEOUT_SECONDS * 1000
            ).json_value()
            fonts = _fonts(page) if result["ok"] else ()
    except PlaywrightTimeoutError:
        raise DiagramError(
            error(
                "mermaid/timeout",
                f"Mermaid did not finish within {MERMAID_TIMEOUT_SECONDS} s",
                hint="split the diagram into an overview plus drill-down diagrams, "
                "or try another config.layout",
            )
        ) from None
    except PlaywrightError as exc:
        raise DiagramError(error("mermaid/render", str(exc))) from None
    if "theme" in result:
        raise DiagramError(
            error(
                "mermaid/theme-unknown",
                f"theme {result['theme']!r} is not in the bundled Mermaid {MERMAID_VERSION}",
                hint="set the theme to one of: " + ", ".join(BUNDLED_THEMES),
            )
        )
    if "limit" in result:
        raise DiagramError(
            error(
                "mermaid/too-large",
                f"the source has {len(source)} characters; without frontmatter, directives and "
                f"comments it is still over Mermaid's maxTextSize of {result['limit']}",
                hint="split it into an overview plus drill-down diagrams, or raise maxTextSize "
                "in the --config file (Mermaid ignores it in frontmatter)",
            )
        )
    if not result["ok"]:
        message = result["message"]
        parsed = re.search(r"Parse error on line (\d+)", message)
        line = result.get("line")
        if line is None and parsed is not None:
            line = int(parsed[1])
        if line is not None:
            raise DiagramError(
                error(
                    "mermaid/parse",
                    message,
                    line=_file_line(source, line),
                    hint="repair the syntax at this source line; the excerpt marks the token",
                )
            )
        raise DiagramError(error("mermaid/render", message))
    diagnostics: list[Diagnostic] = []
    layout = result.get("layout")
    if layout and layout not in BUNDLED_LAYOUTS:
        diagnostics.append(
            warning(
                "mermaid/layout-unsupported",
                f"layout {layout!r} is not in the bundled Mermaid {MERMAID_VERSION}; "
                "it was laid out with dagre",
                hint="set config.layout to one of: " + ", ".join(BUNDLED_LAYOUTS),
            )
        )
    css_line = _theme_css_line(source)
    if css_line is not None and result["themeCSS"] == (config or {}).get("themeCSS"):
        diagnostics.append(
            warning(
                "mermaid/theme-css-dropped",
                "Mermaid discarded this themeCSS; the diagram is rendered without it",
                line=css_line,
                hint="Mermaid drops source config strings containing `<`, `>` or `url(data:`: "
                "write `.a > .b` as `.a .b`, or move the CSS into the --config file",
            )
        )
    svg = _ELEMENT_BREAK.sub(lambda m: m[1] or ">\n", result["svg"])
    return SvgResult(svg, tuple(diagnostics), modified, fonts)


def _fonts(page: Page) -> tuple[str, ...]:
    """Installed font families Chromium drew the tagged text of the mounted diagram with."""
    cdp = page.context.new_cdp_session(page)
    try:
        cdp.send("DOM.enable")
        cdp.send("CSS.enable")
        root = cdp.send("DOM.getDocument")["root"]["nodeId"]
        nodes = cdp.send("DOM.querySelectorAll", {"nodeId": root, "selector": f"[{_FONT_TAG}]"})
        return tuple(
            sorted(
                {
                    font["familyName"]
                    for node in nodes["nodeIds"]
                    for font in cdp.send("CSS.getPlatformFontsForNode", {"nodeId": node})["fonts"]
                }
            )
        )
    finally:
        cdp.detach()


def _icon_pack(spec: str) -> tuple[Path, dict[str, Any]]:
    """Load Iconify pack `prefix` or pinned `prefix@version`, downloading it on first use."""
    prefix = spec.partition("@")[0]
    path = icon_cache_dir() / f"{spec}.json"
    try:
        cached = path.is_file()
        if cached:
            data = path.read_bytes()
        else:
            request = urllib.request.Request(
                f"https://unpkg.com/@iconify-json/{spec}/icons.json",
                headers={"User-Agent": f"mermaid-arch/{__version__}"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read()
        pack = json.loads(data)
        _validate_pack(pack, prefix)
        if not cached:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            try:
                temporary.write_bytes(data)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise DiagramError(
            error(
                "mermaid/icon-pack-unavailable",
                f"cannot load Iconify pack {spec!r}: {exc}",
                hint=f"check the pack name and network access, or place its icons.json at {path}",
            )
        ) from None
    return path, pack


def _validate_pack(pack: object, prefix: str) -> None:
    if (
        not isinstance(pack, dict)
        or pack.get("prefix") != prefix
        or not isinstance(pack.get("icons"), dict)
        or not isinstance(pack.get("aliases", {}), dict)
    ):
        raise ValueError("not an Iconify icon set with the expected prefix and icons map")


def _theme_css_line(source: str) -> int | None:
    """File line of a `themeCSS` key in the frontmatter or an init directive, if there is one."""
    for block in (*_FRONTMATTER.finditer(source), *_DIRECTIVE.finditer(source)):
        if key := _THEME_CSS.search(block[0]):
            return source.count("\n", 0, block.start() + key.end()) + 1
    return None


def _file_line(source: str, parser_line: int) -> int:
    """Map Mermaid's stripped parser input back to file lines, including removed comments."""
    text = source
    positions = list(range(len(source)))
    for pattern in (_FRONTMATTER, _DIRECTIVE, _COMMENT):
        for match in reversed(list(pattern.finditer(text))):
            text = text[: match.start()] + text[match.end() :]
            del positions[match.start() : match.end()]
    start = len(text) - len(text.lstrip())
    text, positions = text[start:], positions[start:]
    offset = 0
    for _ in range(parser_line - 1):
        newline = text.find("\n", offset)
        if newline < 0:
            break
        offset = newline + 1
    if not positions:
        return 1
    original = positions[min(offset, len(positions) - 1)]
    return source.count("\n", 0, original) + 1
