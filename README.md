# mermaid-arch

A Python CLI and agent skill for repository-owned Mermaid and D2 diagrams. Render editable text
sources to SVG, PNG, single-page PDF or standalone HTML. Inspired by
[Archify](https://github.com/tt-a1i/archify)'s author → validate → inspect → deliver workflow;
this is not a Python port of Archify's JSON schemas, graph exploration or animation system.

Extracted from a project-specific D2 rendering script: the hardcoded board list becomes ordinary
source paths, and layout/theme settings live in the diagrams themselves.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install /path/to/mermaid-arch
mermaid-arch doctor
```

For a hosted repository, use `uv tool install 'git+<repository-url>'`. This checkout has no
published package or configured remote. For development, use `uv tool install --editable .`.

D2 is bundled by `d2-python-wrapper`; a `d2` on PATH takes precedence. Mermaid 12.3.0 is bundled
in this package. A system Chromium/Chrome works; otherwise install the matching Playwright build:

```sh
mermaid-arch install-browser
```

The renderer runs locally. It does not send source text to an online diagram service. The
browser blocks external requests during rendering. D2 itself can fetch remote asset URLs;
use trusted sources and local assets. Referenced Iconify packs download once from unpkg into
the cache; no icon pack is downloaded for a diagram without icons. Packs retain their upstream
licenses. A cached pack is never refreshed: `doctor` and the JSON receipt report its Iconify
`lastModified`, which explains icons that differ between machines. `--icon-pack mdi=mdi@1.2.3`
pins a pack to one npm release, cached under its own name, so every machine draws the same
icons. To work offline, populate the cache beforehand with each pack's `icons.json`, named
`<prefix>.json` or, for a pinned pack, `<prefix>@<version>.json` (for example
`~/.cache/mermaid-arch/icons/mdi.json`). The generated HTML needs no renderer, browser
extension, server or network connection.

## Use

```sh
mermaid-arch render docs/architecture.mmd -f svg -f png -f html --json
mermaid-arch render docs/architecture.d2 --layout elk --theme 0 --dark-theme 200 --pad 40
mermaid-arch render docs/*.mmd --config docs/mermaid.json --icon-pack fas=mdi@1.2.3
mermaid-arch render docs/architecture.d2 -o /tmp/architecture.pdf
mermaid-arch render docs/overview.d2 docs/pipeline.d2 --out-dir build/diagrams
mermaid-arch check docs/architecture.mmd docs/architecture.d2 --json
mermaid-arch doctor --json
mermaid-arch render --help
```

Default output is SVG beside the source. Repeat `-f` for several formats. `-o` infers a format
from its suffix; `--out-dir` retains source stems. Sources that would overwrite the same output
are rejected. PNG renders at its natural diagram size with a default 2× scale, not a fixed
800-pixel viewport. Its maximum side is 16,384 pixels; diagnostics suggest a lower scale.
PDF is a single page at the diagram's natural size, not A4 pagination. Mermaid SVG is written
one element per line, so a checked-in file diffs by element.

| Source | Layout | Outputs |
| --- | --- | --- |
| `.mmd`, `.mermaid` | Bundled Mermaid: dagre, or ELK with `config.layout: elk`; native sequence, state, ER and other Mermaid types | SVG, PNG, PDF, HTML |
| `.d2` | D2 dagre or ELK | SVG (including configured dark theme), PNG, PDF, HTML |

The **source owns configuration**. With no flags, D2 honors `vars.d2-config`, then its own
defaults. Explicit D2 flags override source values. Mermaid frontmatter `config` overrides
`--theme`; a pinned theme yields a single-variant HTML page. Unpinned Mermaid sources get light
and dark variants. PNG/PDF use light media; for a dark D2 export specify both theme IDs and a
dark `--background`. The bundled Mermaid lays out with dagre or ELK (`config.layout: elk`); a
layout name it does not register falls back to dagre with a warning.

Several Mermaid sources share one look through `--config FILE`: a Mermaid config in JSON, as
for `mmdc -c`, that holds the theme, spacing and `themeCSS` once. It counts as frontmatter
common to every source: a source's own frontmatter overrides it per key, and a `theme` in it
pins the theme. In its `themeCSS`, as in a source's, `:root` is the SVG itself, so the look can
paint the canvas and declare CSS variables; a `class` given to a named edge is also put on the
edge's path, so the same CSS colours arrows by role.

`--icon-pack NAME=PREFIX[@X.Y.Z]` serves an Iconify pack under another name. Mermaid draws
`fas:fa-name` inside label text from a pack registered as `fas`, so `--icon-pack fas=mdi` puts
`mdi:account` beside a title when the label says `fas:fa-account`. Without the flag, `fa` is
Iconify's Font Awesome 4 pack and `fas`, `far` and `fab` are Font Awesome 6 solid, regular and
brands.

### Checked delivery

Compilation, Iconify-name validation and SVG structure checks precede delivery. A frontmatter
`themeCSS` that Mermaid discards is reported. Parse errors include source line numbers;
diagnostics have stable codes and repair hints. Extreme aspect ratios produce warnings. These
are not overlap detection or architectural correctness checks: inspect the PNG before calling a
diagram visually reviewed.

All conversions for a source finish before any output is written. A compile, check or conversion
failure leaves its previous outputs untouched; other sources in the batch still run. Output
files are staged beside their destinations and each replacement is atomic. Several files are
not a filesystem transaction: an I/O failure during replacement can leave a partially updated
set. Exit codes: `0` success (warnings allowed), `1` rendering failure, `2` invalid invocation.

`--json` emits one object on stdout, including output sizes, SHA-256 values and the icon packs
drawn from. An error example:

```json
{
  "ok": false,
  "version": "0.1.0",
  "results": [{
    "source": "docs/architecture.d2",
    "engine": "d2",
    "ok": false,
    "outputs": [],
    "diagnostics": [{
      "severity": "error",
      "code": "d2/compile",
      "message": "connection missing destination (column 1)",
      "line": 3,
      "hint": "end the connection with a target shape (`a -> b`) or delete the dangling arrow"
    }],
    "icon_packs": []
  }]
}
```

### HTML viewer

One portable file embeds the SVG variants and original source. It offers system/default or
`?theme=dark|light` themes, drag-to-pan, cursor-centered wheel zoom, touch pinch, fit and actual
size, SVG/PNG/source downloads, keyboard shortcuts and Print → PDF. A single-theme diagram
keeps its original paper color when the surrounding viewer goes dark. Browser PNG export can
be restricted by SVG HTML labels or canvas limits; the page shows the CLI export command when
that happens. CLI PNG rendering handles HTML labels directly in Chromium.

## Install the skill

From the checkout (links keep edits in one place):

```sh
mkdir -p ~/.agents/skills ~/.claude/skills
ln -s "$(pwd)/skills/mermaid-arch" ~/.agents/skills/mermaid-arch
ln -s ../../.agents/skills/mermaid-arch ~/.claude/skills/mermaid-arch
```

Use a new name/location if those paths already exist; do not overwrite another installation.
Alternatively use `npx skills add <repository-url> --skill mermaid-arch` after hosting the repo.
The skill and its references/examples live under [`skills/mermaid-arch`](skills/mermaid-arch/SKILL.md).
Ask the agent to “use mermaid-arch to diagram the request path and keep the source in docs”.

## Environment

| Variable | Meaning |
| --- | --- |
| `MERMAID_ARCH_CHROMIUM` | Explicit Chromium/Chrome executable; an invalid override fails rather than falling back |
| `MERMAID_ARCH_D2` | Explicit D2 executable, ahead of PATH and the bundled binary |
| `MERMAID_ARCH_CACHE` | Cache root; icon packs are stored in its `icons/` child |
| `XDG_CACHE_HOME` | Default cache base when `MERMAID_ARCH_CACHE` is unset; otherwise `~/.cache` |

## Develop

```sh
uv sync --frozen
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Browser tests skip when no Chromium can launch. They use a small seeded icon pack and need no
network. To update Mermaid: download the chosen npm release's `dist/mermaid.min.js` into
`src/mermaid_arch/assets/mermaid.min.js`, update `MERMAID_VERSION` and `BUNDLED_LAYOUTS` in
`mermaid.py`, and replace the version, checksum and license in `THIRD_PARTY_NOTICES.md`.
Re-render the examples and check both themes; SVG markup and parser behavior can change between
releases.

MIT; see [LICENSE](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md).
