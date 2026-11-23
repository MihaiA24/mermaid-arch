# mermaid-arch

A Python CLI and agent skill for repository-owned Mermaid and D2 diagrams. Render editable text
sources to SVG, PNG, single-page PDF or standalone HTML. Inspired by
[Archify](https://github.com/tt-a1i/archify)'s author → validate → inspect → deliver workflow;
this is not a Python port of Archify's JSON schemas, graph exploration or animation system.

Extracted from a project-specific D2 rendering script: the hardcoded board list becomes ordinary
source paths, and layout/theme settings live in the diagrams themselves.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). Everything installs from GitHub;
nothing is published to PyPI. Each `vX.Y.Z` release attaches a wheel and an sdist.

Run a release without installing it:

```sh
uvx --from https://github.com/MihaiA24/mermaid-arch/releases/download/v0.1.1/mermaid_arch-0.1.1-py3-none-any.whl mermaid-arch doctor
```

Or install it as a command-line tool:

```sh
uv tool install https://github.com/MihaiA24/mermaid-arch/releases/download/v0.1.1/mermaid_arch-0.1.1-py3-none-any.whl
uv tool install git+https://github.com/MihaiA24/mermaid-arch   # or main, built from source
mermaid-arch doctor
```

As a dependency of another project, for example a dev tool run as `uv run mermaid-arch render`:

```sh
uv add --dev "mermaid-arch @ https://github.com/MihaiA24/mermaid-arch/releases/download/v0.1.1/mermaid_arch-0.1.1-py3-none-any.whl"
```

The release wheel needs no git or build step, and pip accepts the same URL.
`uv add --dev git+https://github.com/MihaiA24/mermaid-arch --tag v0.1.1` builds the same release
from source. For development, use `uv tool install --editable .`.

D2 is bundled by `d2-python-wrapper`, which fixes its release (0.6.8); a `d2` on PATH or in
`MERMAID_ARCH_D2` takes precedence and is the only route to a newer D2. `doctor` names the
binary in use and where it came from. Mermaid 12.3.0 is bundled in this package; fontTools
subsets the fonts given to `--font`. A system Chromium/Chrome works, as does any Chromium
already in Playwright's cache; otherwise install the matching Playwright build:

```sh
mermaid-arch install-browser
```

The renderer runs locally. It does not send source text to an online diagram service. The
browser blocks external requests during rendering. D2 itself can fetch remote asset URLs;
use trusted sources and local assets. Referenced Iconify packs download once from unpkg into
the cache; no icon pack is downloaded for a diagram without icons. Only the icons a diagram
uses are sent to the render page. Packs retain their upstream licenses. A cached pack is never
refreshed: `doctor` and the JSON receipt report its Iconify `lastModified`, which explains
icons that differ between machines. `--icon-pack mdi=mdi@1.2.3` pins a pack to one npm
release, cached under its own name, so every machine draws the same icons. To work offline,
populate the cache beforehand with each pack's `icons.json`, named
`<prefix>.json` or, for a pinned pack, `<prefix>@<version>.json` (for example
`~/.cache/mermaid-arch/icons/mdi.json`). The generated HTML needs no renderer, browser
extension, server or network connection.

## Use

```sh
mermaid-arch render docs/architecture.mmd -f svg -f png -f html --json
mermaid-arch render docs/architecture.d2 --layout elk --theme 0 --dark-theme 200 --pad 40
mermaid-arch render docs/*.mmd --config docs/mermaid.json --icon-pack fas=mdi@1.2.3
mermaid-arch render docs/*.mmd --preset archify --font fonts/JetBrainsMono-Regular.ttf
mermaid-arch render docs/architecture.d2 -o /tmp/architecture.pdf
mermaid-arch render docs/overview.d2 docs/pipeline.d2 --out-dir build/diagrams
mermaid-arch render docs/boards.d2 --target layers.detail --sketch
mermaid-arch render docs/design.md --out-dir docs/diagrams
mermaid-arch render - --engine mermaid -o flow.svg < flow.txt
mermaid-arch render docs/*.mmd -f svg -f png --verify
mermaid-arch render docs/architecture.mmd -f html --watch
mermaid-arch check docs/architecture.mmd docs/architecture.d2 --json
mermaid-arch doctor --json
mermaid-arch render --help
```

Default output is SVG beside the source. Repeat `-f` for several formats. `-o` infers a format
from its suffix; `--out-dir` retains source stems. Sources that would overwrite the same output
are rejected. PNG renders at its natural diagram size with a default 2× scale, not a fixed
800-pixel viewport. Its maximum side is 16,384 pixels; diagnostics suggest a lower scale.
PDF is a single page at the diagram's natural size, not A4 pagination. Mermaid SVG is written
one element per line, so a checked-in file diffs by element. Mermaid CSS rules that match no
element are dropped and path coordinates are rounded to three decimals, which roughly halves
a small diagram's SVG.

A Markdown source is rendered block by block: each fenced `mermaid` or `d2` block becomes
`<stem>-<n>.<format>`, numbered in file order, and its diagnostics carry the Markdown file's
line numbers. SOURCE `-` reads one diagram from stdin; it needs `--engine` and, for `render`,
`-o FILE`. `--engine` sets the engine of every source that is not Markdown, whatever its suffix.
`--watch` renders, then re-renders each listed source when it changes, until Ctrl-C; it does not
follow D2 imports or the `--config` file.

| Source | Layout | Outputs |
| --- | --- | --- |
| `.mmd`, `.mermaid` | Bundled Mermaid: dagre, or ELK with `config.layout: elk`; native sequence, state, ER and other Mermaid types | SVG, PNG, PDF, HTML |
| `.d2` | D2 dagre or ELK | SVG (including configured dark theme), PNG, PDF, HTML |
| `.md`, `.markdown` | Each fenced `mermaid` or `d2` block, by its own engine | One set per block, named `<stem>-<n>` |

The **source owns configuration**. With no flags, D2 honors `vars.d2-config`, then its own
defaults. Explicit D2 flags override source values. Mermaid frontmatter `config` overrides
`--theme`; a pinned theme yields a single-variant HTML page. Unpinned Mermaid sources get light
and dark variants. PNG/PDF use light media; for a dark D2 export specify both theme IDs and a
dark `--background`. The bundled Mermaid lays out with dagre or ELK (`config.layout: elk`); a
layout name it does not register falls back to dagre with a warning. A theme name it does not
register is an error, not a silent default palette.

A D2 source with `layers`, `scenarios` or `steps` holds several boards and renders one at a
time: `--target ''` is the root board, `--target layers.NAME` one layer. `--sketch` gives D2
its hand-drawn look; a Mermaid source declares `look: handDrawn` in its frontmatter instead.

Several Mermaid sources share one look through `--config FILE`: a Mermaid config in JSON, as
for `mmdc -c`, that holds the theme, spacing and `themeCSS` once. It counts as frontmatter
common to every source: a source's own frontmatter overrides it per key, and a `theme` in it
pins the theme. In its `themeCSS`, as in a source's, `:root` is the SVG itself, so the look can
paint the canvas and declare CSS variables; a `class` given to a named edge is also put on the
edge's path, so the same CSS colours arrows by role.

`--preset archify` is a built-in shared look ported from a project that rendered with mmdc: a
dotted canvas, a monospace font stack, roomy spacing, colour per role class (`actor app sec data
ext danger`) and a status class (`new later planned`) that replaces the role colour with its own
and dashes the border. Nodes take a role or status with `class id role`, named edges with
`class edgeId role`. `sequence.mirrorActors: false` omits bottom participant copies that
have no IDs for role styling. `--config` overrides the preset, and frontmatter overrides both.
Their `themeCSS` adds to the preset's. The preset pins the theme, so `--theme` and `--dark-theme`
are ignored with a warning: its dark mode follows the reader's colour scheme, in the HTML viewer
too, where the theme button switches only the page around the diagram. A source draws a key of
role colours with `%% legend: app Service | data Store` and one line under it with
`%% legend-arrows: TEXT`; `render --help` lists these directives.

`--icon-pack NAME=PREFIX[@X.Y.Z]` serves an Iconify pack under another name. Mermaid draws
`fas:fa-name` inside label text from a pack registered as `fas`, so `--icon-pack fas=mdi` puts
`mdi:account` beside a title when the label says `fas:fa-account`. Without the flag, `fa` is
Iconify's Font Awesome 4 pack and `fas`, `far` and `fab` are Font Awesome 6 solid, regular and
brands. Label icons keep their own colours, as icon shapes do; `--monochrome-icons` draws them
in the text colour, Mermaid's default.

Text is laid out with the fonts installed on the rendering machine. `--font FILE`, once per
weight or style, lays it out with that TTF, OTF, WOFF or WOFF2 file instead, and embeds a
WOFF2 subset of the glyphs used in the SVG, so it looks the same where the font is not
installed. Name the font's family in `fontFamily`. If you set `themeVariables.fontFamily`,
it takes precedence, so name the family there too. A font file Chromium does not use is
reported as `mermaid/font-unused` and left out of the SVG.

### Checked delivery

Compilation, Iconify-name validation and SVG structure checks precede delivery. A frontmatter
`themeCSS` that Mermaid discards is reported. So is a source over Mermaid's text limit, which
Mermaid alone would draw as a one-box placeholder, and a render that has not finished after
180 s is stopped. Parse errors include source line numbers; diagnostics have stable codes and
repair hints. A diagram more than 4× longer than it is wide, or the reverse, produces a warning
once its long side passes 1,600 px. These are not overlap detection or architectural
correctness checks: inspect the PNG before calling a diagram visually reviewed.

All conversions for a source finish before any output is written. A compile, check or conversion
failure leaves its previous outputs untouched; other sources in the batch still run. Output
files are staged beside their destinations and each replacement is atomic. Several files are
not a filesystem transaction: an I/O failure during replacement can leave a partially updated
set. Exit codes: `0` success (warnings allowed), `1` rendering failure, `2` invalid invocation.

On one machine a source renders to the same bytes every time: Mermaid's random ids are seeded
and PDFs carry no timestamps. `render --verify` relies on that: it renders in memory, writes
nothing, and fails with `output/stale` or `output/missing` when a target file is not what its
source renders now, which lets CI prove that committed diagrams are current. Across machines,
Mermaid text is laid out with the fonts installed there, so `--verify` holds where the fonts,
Chromium and icon packs match those of the machine that rendered; the receipt and `doctor`
name the fonts. A Mermaid SVG takes its root id from its output name (`mermaid-arch-<stem>`, or
`mermaid-arch-<stem>-<n>` for a Markdown block), so several can be inlined in one page without
their styles colliding.

`--json` emits one object on stdout, including output sizes, SHA-256 values, the fonts Mermaid
text was laid out with and the icon packs drawn from; the result of a Markdown block also has
its `block` number. An error example:

```json
{
  "ok": false,
  "version": "0.1.1",
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
    "icon_packs": [],
    "fonts": []
  }]
}
```

### HTML viewer

One portable file embeds the SVG variants and original source. It offers system/default or
`?theme=dark|light` themes, drag or arrow-key panning, cursor-centered wheel zoom, touch pinch,
fit and actual size, SVG/PNG/source downloads, keyboard shortcuts and Print → PDF. A
single-theme diagram keeps its original paper color when the surrounding viewer goes dark.
Browser PNG export can be restricted by SVG HTML labels or canvas limits; the page shows the
CLI export command when that happens. CLI PNG rendering handles HTML labels directly in
Chromium.

## Install the skill

The skill runs the CLI through `uvx` from the release it documents, so only uv needs installing.

Claude Code, as a plugin from this repository's marketplace:

```sh
claude plugin marketplace add MihaiA24/mermaid-arch   # append #v0.1.1 to stay on a release
claude plugin install mermaid-arch@mermaid-arch
```

Inside a session, the same commands start with `/plugin`. Without a tag, the plugin follows
`main`. On a machine without a GitHub SSH key, set `CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1`.

Other agents, through the [skills](https://github.com/vercel-labs/skills) installer:

```sh
npx skills add MihaiA24/mermaid-arch --skill mermaid-arch
```

From a checkout, links keep edits in one place:

```sh
mkdir -p ~/.agents/skills ~/.claude/skills
ln -s "$(pwd)/skills/mermaid-arch" ~/.agents/skills/mermaid-arch
ln -s ../../.agents/skills/mermaid-arch ~/.claude/skills/mermaid-arch
```

Use a new name/location if those paths already exist; do not overwrite another installation.
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

Browser tests skip when no Chromium can launch; with `MERMAID_ARCH_REQUIRE_BROWSER=1`, as in
CI, they fail instead. CI runs the suite on Linux and macOS with Python 3.12 and 3.14. The
tests use a small seeded icon pack and need no network. To update Mermaid: download the chosen
npm release's `dist/mermaid.min.js` into `src/mermaid_arch/assets/mermaid.min.js`, update
`MERMAID_VERSION`, `BUNDLED_LAYOUTS` and `BUNDLED_THEMES` in `mermaid.py`, and replace the
version, checksum and license in `THIRD_PARTY_NOTICES.md`. Re-render the examples and check
both themes; SVG markup and parser behavior can change between releases.

To release, set `version` in `pyproject.toml` and every pinned release in this README and
`SKILL.md` to it, commit, then tag and push `vX.Y.Z` right away: until the release job finishes,
the pinned wheel URLs return 404. CI checks that the pins name the project version, confirms the
tag names it too, builds, and publishes a GitHub release with the wheel and sdist.

MIT; see [LICENSE](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md).
