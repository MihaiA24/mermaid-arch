"""Pinned, local Mermaid renderer with checked Iconify references."""

from __future__ import annotations

import base64
import difflib
import io
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from fontTools import subset
from fontTools.ttLib import TTFont
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
    r"""(?:^|[{,])\s*icon\s*:\s*["']?(?P<shape>[a-z0-9-]+:[a-z0-9-]+)"""
    r"|^\s*(?:service|group)\s+[\w-]+\s*\((?P<arch>[a-z0-9-]+:[a-z0-9-]+)\)"
    r"|\b(?P<label>fa[bklrs]?:fa-[\w-]+)"
)
# Node shape data, `id@{ ... }`, the only place an `icon:` key means an icon. A brace inside
# one of its quoted strings does not close it, and an `icon:` inside one is label text.
_SHAPE_DATA = re.compile(r"""@\{(?:"[^"]*"|'[^']*'|[^"'}])*\}""")
_QUOTED = re.compile(r""""[^"]*"|'[^']*'""")
_FRONTMATTER = re.compile(r"\A\s*---[^\S\n]*\n.*?\n---[^\S\n]*(?:\n|$)", re.S)
_DIRECTIVE = re.compile(r"%%\{.*?\}%%", re.S)
_COMMENT = re.compile(r"^[ \t]*%%[^\n]*(?:\n|$)", re.M)
_THEME_CSS = re.compile(r"""(?:^|[\s{,"'])themeCSS["']?\s*:""", re.M)
_LEGEND = re.compile(r"^[ \t]*%%[ \t]*legend:(.*)$", re.M)
_LEGEND_ARROWS = re.compile(r"^[ \t]*%%[ \t]*legend-arrows:(.*)$", re.M)
_ROLE = re.compile(r"[A-Za-z_][\w-]*")
_PRESETS = files("mermaid_arch") / "presets"
# Built-in looks for --preset: `<name>.json` is Mermaid config, `<name>.css` its themeCSS.
PRESETS = tuple(sorted(p.name[:-5] for p in _PRESETS.iterdir() if p.name.endswith(".json")))
# One element per line keeps a checked-in SVG diffable. Labels stay whole: whitespace between
# their inline elements would be drawn.
_ELEMENT_BREAK = re.compile(r"(<(foreignObject|text)\b.*?</\2(?=>))|>(?=<)", re.S)
_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>{faces}</style></head>
<body><div id="container"></div></body></html>"""
_FONT_TAG = "data-mermaid-arch-font"
_RENDER = r"""args => {
  // Start the render after this call returns, so that Python can wait for it with a deadline
  // that holds even while a layout never yields the main thread.
  setTimeout(async () => { window.mermaidArchResult = await render(args); }, 0);

  async function render({source, id, theme, themes, site, presetCSS, packs, fontTag, brandIcons,
                         legend}) {
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
      // Mermaid measures labels as it lays them out: --font faces must be loaded before that.
      await Promise.all([...document.fonts].map(face => face.load()));
      const parsed = await mermaid.parse(source);
      const named = parsed.config?.theme ?? requested;
      if (!themes.includes(named)) return {ok: false, theme: String(named)};
      const effective = mermaid.mermaidAPI.getConfig();
      const layout = effective.layout;
      // Frontmatter can replace the requested theme, but initialize() has already expanded
      // that theme's palette. Reinitialize from the effective theme to avoid mixed palettes.
      let changed = effective.theme !== (config.theme || 'default');
      if (changed) config.theme = effective.theme;
      // Frontmatter themeCSS replaces the shared CSS. Put the preset's back under it, and mark
      // the key secure so the render does not replace the merged CSS again.
      if (presetCSS !== null && effective.themeCSS !== config.themeCSS) {
        config.themeCSS = presetCSS + '\n' + effective.themeCSS;
        config.secure = [...effective.secure, 'themeCSS'];
        changed = true;
      }
      if (changed) mermaid.initialize(config);
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
      // Use stable diagram-scoped IDs so inlined diagrams cannot share paint servers.
      const ids = new Map([...root.querySelectorAll('[id^="IconifyId"]')]
        .map((element, index) => [element.id, `${root.id}_iconify-${index}`]));
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
      if (brandIcons) keepIconColours(root);
      await document.fonts.ready;
      const unusedRoles = legend ? addLegend(root, legend) : [];
      pruneStyles(root);
      roundNumbers(root);
      const text = [...root.querySelectorAll('text, foreignObject')]
        .map(element => element.textContent).join('');
      const serialized = new XMLSerializer().serializeToString(root);
      const sheet = document.head.querySelector('style');
      let usedFaces = [];
      if (sheet.textContent) {
        // Reset the eagerly loaded faces so only this SVG's layout can load them again.
        const previous = new Set(document.fonts);
        sheet.textContent = sheet.textContent;
        const supplied = [...document.fonts].filter(face => !previous.has(face));
        await document.fonts.ready;
        usedFaces = supplied.map(face => face.status === 'loaded');
      }
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
      return {
        ok: true, svg: serialized, layout, themeCSS: effective.themeCSS ?? null,
        unusedRoles, text, stacks: [...stacks], usedFaces
      };
    } catch (e) {
      return {ok: false, message: String(e.message || e), line: e.hash?.loc?.first_line};
    }
  }

  // Mermaid's `.node circle, .node rect…` and neo `.node path` rules repaint label-icon
  // artwork. Restore the authored paint, or SVG's initial paint where none is authored.
  function keepIconColours(root) {
    const initial = {fill: 'currentColor', stroke: 'none', 'stroke-width': '1'};
    const shapes = 'path, circle, ellipse, rect, polygon, polyline, line';
    for (const icon of root.querySelectorAll('svg.label-icon')) {
      for (const shape of icon.querySelectorAll(shapes)) {
        for (const [name, unpainted] of Object.entries(initial)) {
          const painted = shape.closest(`[${name}]`);
          const value = icon.contains(painted) ? painted.getAttribute(name) : unpainted;
          shape.style.setProperty(name, value);
        }
      }
    }
  }

  // A key of role squares in the top-left corner, beside the title when there is room above
  // the diagram; otherwise the canvas grows upwards. Returns the roles that style nothing.
  function addLegend(root, {items, arrows}) {
    const make = (name, attributes, parent) => {
      const element = document.createElementNS('http://www.w3.org/2000/svg', name);
      for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
      return parent.appendChild(element);
    };
    const shapes = 'rect, path, polygon, circle, ellipse';
    const samples = items.map(([role]) => {
      const selector = '.' + CSS.escape(role);
      const styled = root.querySelector('.node' + selector) ?? root.querySelector(selector);
      return styled?.matches(shapes) ? styled : styled?.querySelector(shapes) ?? null;
    });
    const unused = items.filter((_, index) => !samples[index]).map(([role]) => role);
    const square = 11, gap = 6, row = 16, margin = 12, inset = 16;
    const legend = make('g', {class: 'legend'}, root);
    const entries = items.map(([role, label], index) => {
      // The square sits in a group of the role's class, so classDef rules (`.role > *`) and
      // role variables reach it; the colours of the role's first shape are its fallback.
      const group = make('g', {class: role}, legend);
      const swatch = make('rect', {width: square, height: square, rx: 2}, group);
      if (samples[index]) {
        const style = getComputedStyle(samples[index]);
        for (const name of ['fill', 'stroke', 'stroke-dasharray']) {
          swatch.setAttribute(name, style.getPropertyValue(name));
        }
      }
      const text = make('text', {'font-size': 11}, legend);
      text.textContent = label;
      return [swatch, text];
    });
    const note = arrows ? make('text', {'font-size': 11}, legend) : null;
    if (note) note.textContent = arrows;
    const columns = items.length > 4 ? 2 : 1;
    const rows = Math.ceil(items.length / columns);
    const column = square + gap + inset
      + Math.max(...entries.map(([, text]) => text.getComputedTextLength()));
    const width = Math.max(columns * column - inset, note?.getComputedTextLength() ?? 0);
    const height = row * (rows + (note ? 1 : 0));
    const box = root.viewBox.baseVal;
    const {x, y} = box;
    let {width: w, height: h} = box;
    const user = root.getScreenCTM().inverse();
    const corner = element => {
      const rect = element.getBoundingClientRect();
      return new DOMPoint(rect.left, rect.top).matrixTransform(user);
    };
    const title = root.querySelector(':scope > text, .flowchartTitleText');
    const tops = [...root.children]
      .filter(element => element instanceof SVGGraphicsElement && element !== legend
        && element !== title && element.getBoundingClientRect().height)
      .map(element => corner(element).y);
    const band = (tops.length ? Math.min(...tops) : y) - y - margin;
    const beside = x + inset + width < (title ? corner(title).x : x + w) - 2 * margin;
    const extra = beside ? Math.max(0, height + margin - band) : height + margin;
    const top = y - extra + margin;
    const round = value => Math.round(value * 1000) / 1000;
    entries.forEach(([swatch, text], index) => {
      const left = x + inset + Math.floor(index / rows) * column;
      const offset = top + (index % rows) * row;
      swatch.setAttribute('x', round(left));
      swatch.setAttribute('y', round(offset));
      text.setAttribute('x', round(left + square + gap));
      text.setAttribute('y', round(offset + 9.5));
    });
    if (note) {
      note.setAttribute('x', round(x + inset));
      note.setAttribute('y', round(top + rows * row + 9.5));
    }
    w = Math.max(w, 2 * inset + width);
    h += extra;
    root.setAttribute('viewBox', [x, y - extra, w, h].map(round).join(' '));
    if (root.style.maxWidth) root.style.maxWidth = `${round(w)}px`;
    for (const [name, value] of [['width', w], ['height', h]]) {
      if (/^[\d.]+(px)?$/.test(root.getAttribute(name) ?? '')) {
        root.setAttribute(name, round(value));
      }
    }
    return unused;
  }

  // Mermaid ships the CSS of everything its diagram type can draw. Keep the rules that match
  // an element here, counting a :hover or ::before rule as its element's; keep at-rules.
  function pruneStyles(root) {
    const pseudos = ['hover', 'active', 'focus', 'focus-visible', 'focus-within', 'visited',
                     'target', 'before', 'after', 'first-line', 'first-letter', 'selection',
                     'placeholder', 'marker'];
    const state = new RegExp('::?(?:' + pseudos.join('|') + ')(?![\\w-])', 'g');
    const live = selectors => {
      const plain = selectors.replace(state, '');
      try {
        return root.matches(plain) || root.querySelector(plain) !== null;
      } catch {
        return true;
      }
    };
    // The CSS without dead rules, or null when it does not parse as balanced blocks.
    const prune = css => {
      let kept = '', start = 0, open = 0, depth = 0, quote = '';
      for (let i = 0; i < css.length; i++) {
        const c = css[i];
        if (c === '\\') { i++; continue; }
        if (quote) { if (c === quote) quote = ''; continue; }
        if (c === '"' || c === "'") quote = c;
        else if (c === '{') { if (depth++ === 0) open = i; }
        else if (c === '}') {
          if (--depth < 0) return null;
          if (depth > 0) continue;
          const prelude = css.slice(start, open).trim();
          if (/^@(?:media|supports)\b/.test(prelude)) {
            const inner = prune(css.slice(open + 1, i));
            if (inner === null) return null;
            if (inner.trim()) kept += css.slice(start, open + 1) + inner + '}';
          } else if (prelude.startsWith('@') || live(prelude)) {
            kept += css.slice(start, i + 1);
          }
          start = i + 1;
        } else if (c === ';' && depth === 0) {
          kept += css.slice(start, i + 1);
          start = i + 1;
        }
      }
      return depth === 0 && !quote ? kept + css.slice(start) : null;
    };
    for (const style of root.querySelectorAll('style')) {
      const kept = prune(style.textContent);
      if (kept !== null) style.textContent = kept;
    }
  }

  // Layout writes coordinates with up to 17 digits; a thousandth of a unit is invisible.
  // Icon artwork, in nested <svg> elements, keeps its own numbers and compact path syntax.
  function roundNumbers(root) {
    for (const element of root.querySelectorAll('[d], [points], [transform]')) {
      if (element.ownerSVGElement !== root) continue;
      for (const name of ['d', 'points', 'transform']) {
        const value = element.getAttribute(name);
        if (!value) continue;
        // Never part of a number: in compact path data `1.5.25` is two numbers, as is `1-0.5`.
        const rounded = value.replace(/(?<![\d.])-?\d+\.\d{4,}(?:e[-+]?\d+)?(?![\d.])/g,
          number => String(Math.round(number * 1000) / 1000));
        if (rounded !== value) element.setAttribute(name, rounded);
      }
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
    preset: str | None = None,
    monochrome_icons: bool = False,
    font_files: Sequence[Path] = (),
) -> SvgResult:
    """Render `source`; `config` is shared Mermaid config that frontmatter overrides per key.

    `icon_packs` maps a pack name used in the source to the Iconify pack that serves it,
    written `prefix` or, pinned to a release, `prefix@version`. `svg_id`, matching
    `[a-z][a-z0-9-]*`, is the SVG root's id, which scopes its CSS: SVGs that share a page
    need distinct ones. `preset`, one of `PRESETS`, is config that `config` and frontmatter
    override per key; their themeCSS adds to its.
    `monochrome_icons` leaves label icons in the text colour, as Mermaid draws them.
    `font_files` lay the text out and are embedded, cut down to the characters drawn.
    """
    base = load_preset(preset) if preset else {}
    site = merge_config(base, config or {})
    legend, legend_line = _legend(source)
    served = {**FONT_AWESOME_PACKS, **(icon_packs or {})}
    references: dict[str, list[tuple[str, str, int]]] = {}
    shape_data = [match.span() for match in _SHAPE_DATA.finditer(source)]
    quoted = [q.span() for a, b in shape_data for q in _QUOTED.finditer(source, a, b)]
    end = 0
    for number, line in enumerate(source.splitlines(keepends=True), 1):
        start, end = end, end + len(line)
        if line.lstrip().startswith("%%"):
            continue
        for match in _ICON.finditer(line):
            at = start + match.start()
            if match["shape"] and (
                not any(a < at < b for a, b in shape_data) or any(a < at < b for a, b in quoted)
            ):
                continue  # `, icon: x:y` in label text is words
            written = match["shape"] or match["arch"] or match["label"]
            name, icon = written.split(":", 1)
            if match["label"]:
                icon = icon.removeprefix("fa-")
            references.setdefault(name, []).append((written, icon, number))

    problems: list[Diagnostic] = []
    routes: dict[str, bytes | str | Path] = {}
    faces: list[tuple[Path, bytes, str, str, str]] = []
    declared: list[str] = []
    for path in font_files:
        try:
            data, face = read_font(path)
        except ValueError as exc:
            problems.append(error("mermaid/font-unreadable", f"cannot read {path}: {exc}"))
            continue
        url = f"/fonts/{len(faces)}{path.suffix.lower()}"
        routes[url] = data
        declared.append(_font_face(*face, f'src:url("{url}")'))
        faces.append((path, data, *face))
    routes["/"] = _HTML.format(faces="".join(declared))
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
        # Only the icons drawn: a whole pack is megabytes for Chromium to fetch and parse.
        used = {icon for _, icon, _ in uses}
        routes[f"/icons/{name}.json"] = json.dumps(_subset(pack, used))
        stamp = pack.get("lastModified")
        modified[spec] = stamp if isinstance(stamp, int) else None
    if problems:
        raise DiagramError(*problems)

    # A new document per render: Mermaid keeps id counters in module state that page code cannot
    # reset, so in a reused one a diagram's ids would depend on what rendered before it.
    try:
        with session.kept_page(routes, script=_bundle()) as page:
            page.evaluate(
                _RENDER,
                {
                    "source": source,
                    "id": svg_id,
                    "theme": theme,
                    "themes": list(BUNDLED_THEMES),
                    "site": site,
                    "presetCSS": base.get("themeCSS"),
                    "packs": list(references),
                    "fontTag": _FONT_TAG,
                    "brandIcons": not monochrome_icons,
                    "legend": legend,
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
    if css_line is not None and result["themeCSS"] == site.get("themeCSS"):
        diagnostics.append(
            warning(
                "mermaid/theme-css-dropped",
                "Mermaid discarded this themeCSS; the diagram is rendered without it",
                line=css_line,
                hint="Mermaid drops source config strings containing `<`, `>` or `url(data:`: "
                "write `.a > .b` as `.a .b`, or move the CSS into the --config file",
            )
        )
    for role in result["unusedRoles"]:
        diagnostics.append(
            warning(
                "mermaid/legend-role-unused",
                f"the legend lists role {role!r}, but no element has that class",
                line=legend_line,
                hint=f"give a node the role with `class ID {role}`, or drop it from the legend",
            )
        )
    svg = _ELEMENT_BREAK.sub(lambda m: m[1] or ">\n", result["svg"])
    named = {
        family.strip().strip("\"'").lower()
        for stack in result["stacks"]
        for family in stack.split(",")
    }
    embedded: list[str] = []
    for (path, data, family, weight, style), used in zip(faces, result["usedFaces"], strict=True):
        # shortcut: each face is cut to all the drawn text, not its own glyphs; split per face
        # if embedded fonts dominate SVG size.
        if used and result["text"]:
            embedded.append(_embedded_face(data, family, weight, style, result["text"]))
        elif family.lower() not in named:
            diagnostics.append(
                warning(
                    "mermaid/font-unused",
                    f"no text names font family {family!r}, so {path} is not embedded",
                    hint=f'start fontFamily with "{family}" in the --config file or frontmatter; '
                    "if themeVariables.fontFamily is set, update it too",
                )
            )
        else:
            diagnostics.append(
                warning(
                    "mermaid/font-unused",
                    f"no text uses {path} ({family!r}, weight {weight}, {style}), "
                    "so it is not embedded",
                )
            )
    if embedded:
        # The root element is the first line; the faces go before Mermaid's own <style>.
        head, _, rest = svg.partition("\n")
        svg = f"{head}\n<style>{escape(''.join(embedded))}</style>\n{rest}"
    return SvgResult(svg, tuple(diagnostics), modified, fonts)


def load_preset(name: str) -> dict[str, object]:
    """The Mermaid config of built-in preset `name`, one of `PRESETS`."""
    if name not in PRESETS:
        raise ValueError(f"unknown preset {name!r}; choose from: {', '.join(PRESETS)}")
    config = json.loads((_PRESETS / f"{name}.json").read_text(encoding="utf-8"))
    css = _PRESETS / f"{name}.css"
    if css.is_file():
        config["themeCSS"] = css.read_text(encoding="utf-8")
    return config


def merge_config(base: Mapping[str, object], over: Mapping[str, object]) -> dict[str, object]:
    """`over` on top of `base`: objects merge key by key, and themeCSS adds to the base's."""
    merged = dict(base)
    for key, value in over.items():
        below = merged.get(key)
        if isinstance(below, Mapping) and isinstance(value, Mapping):
            merged[key] = merge_config(below, value)
        elif key == "themeCSS" and isinstance(below, str) and isinstance(value, str):
            merged[key] = f"{below}\n{value}"
        else:
            merged[key] = value
    return merged


def read_font(path: Path) -> tuple[bytes, tuple[str, str, str]]:
    """Read the font bytes and its CSS font-family, font-weight and font-style."""
    try:
        data = path.read_bytes()
        with TTFont(io.BytesIO(data), lazy=True) as font:
            family = font["name"].getBestFamilyName()
            os2 = font["OS/2"]
            axes = {axis.axisTag: axis for axis in font["fvar"].axes} if "fvar" in font else {}
            weight = (
                f"{axes['wght'].minValue:g} {axes['wght'].maxValue:g}"
                if "wght" in axes
                else str(os2.usWeightClass)
            )
            style = "italic" if os2.fsSelection & 1 else "normal"
    except OSError as exc:
        raise ValueError(exc.strerror or str(exc)) from None
    except Exception as exc:  # fontTools raises many types for a file that is not a font
        raise ValueError(f"not a single TTF, OTF, WOFF or WOFF2 font ({exc})") from None
    if not family:
        raise ValueError("the font names no family")
    return data, (family, weight, style)


def _font_face(family: str, weight: str, style: str, source: str) -> str:
    # CSS escapes, not JSON's \uXXXX (CSS reads `\u` as `u`); `<` stops a `</style>` in a
    # crafted font's name closing the render page's style element.
    name = re.sub(r'[\\"<\x00-\x1f\x7f]', lambda m: f"\\{ord(m[0]):x} ", family)
    return f'@font-face{{font-family:"{name}";font-weight:{weight};font-style:{style};{source}}}'


def _embedded_face(body: bytes, family: str, weight: str, style: str, text: str) -> str:
    """An @font-face carrying the glyphs of `text` from the supplied font bytes as WOFF2."""
    subsetter = subset.Subsetter(subset.Options())
    subsetter.populate(text=text)
    # A fresh timestamp would change every render.
    data = io.BytesIO()
    with TTFont(io.BytesIO(body), recalcTimestamp=False) as font:
        subsetter.subset(font)
        font.flavor = "woff2"
        font.save(data)
    codes = sorted({ord(char) for char in text})
    runs: list[list[int]] = []
    for code in codes:
        if runs and runs[-1][1] == code - 1:
            runs[-1][1] = code
        else:
            runs.append([code, code])
    # Faces of one family from several inlined SVGs combine: each draws the characters it has.
    ranges = ",".join(f"U+{a:X}" if a == b else f"U+{a:X}-{b:X}" for a, b in runs)
    encoded = base64.b64encode(data.getvalue()).decode()
    source = f'unicode-range:{ranges};src:url(data:font/woff2;base64,{encoded}) format("woff2")'
    return _font_face(family, weight, style, source)


def _legend(source: str) -> tuple[dict[str, object] | None, int | None]:
    """The `%% legend:` directive as render arguments, and its line."""
    found = _LEGEND.search(source)
    arrows = _LEGEND_ARROWS.search(source)
    for first, pattern, directive in (
        (found, _LEGEND, "legend"),
        (arrows, _LEGEND_ARROWS, "legend-arrows"),
    ):
        if first and (duplicate := pattern.search(source, first.end())):
            raise DiagramError(
                error(
                    "mermaid/legend",
                    f"second `%% {directive}:` directive",
                    line=source.count("\n", 0, duplicate.start()) + 1,
                    hint=f"keep all entries in one `%% {directive}:` directive",
                )
            )
    if found is None:
        return None, None
    line = source.count("\n", 0, found.start()) + 1
    items: list[list[str]] = []
    for entry in found[1].split("|"):
        role, _, label = entry.strip().partition(" ")
        if not _ROLE.fullmatch(role) or not label.strip():
            raise DiagramError(
                error(
                    "mermaid/legend",
                    f"legend entry {entry.strip()!r} is not a role followed by its label",
                    line=line,
                    hint="write `%% legend: role Label | role Label`, each role a class name",
                )
            )
        items.append([role, label.strip()])
    return {"items": items, "arrows": arrows[1].strip() if arrows else None}, line


def _subset(pack: Mapping[str, Any], names: set[str]) -> dict[str, Any]:
    """`pack` with only the icons and aliases `names` draw on; an alias's parent may be one."""
    icons, aliases = pack["icons"], pack.get("aliases", {})
    kept_icons: dict[str, Any] = {}
    kept_aliases: dict[str, Any] = {}
    pending = sorted(names)
    while pending:
        name = pending.pop()
        if name in icons:
            kept_icons[name] = icons[name]
        elif name in aliases and name not in kept_aliases:
            kept_aliases[name] = aliases[name]
            parent = aliases[name].get("parent") if isinstance(aliases[name], dict) else None
            if isinstance(parent, str):
                pending.append(parent)
    return {**pack, "icons": kept_icons, "aliases": kept_aliases}


@cache
def _bundle() -> str:
    return (files("mermaid_arch") / "assets" / "mermaid.min.js").read_text(encoding="utf-8")


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
