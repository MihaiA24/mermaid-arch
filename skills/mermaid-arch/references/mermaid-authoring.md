# Mermaid authoring

- **Repository source.** Use `.mmd` or `.mermaid`; export the SVG for Markdown hosts that do not
  support the exact Mermaid version or icon syntax. The tool bundles Mermaid 12.3.0.
- **Source configuration.** Put spacing, theme and `themeCSS` in YAML frontmatter `config`.
  A source that pins `theme` overrides the CLI theme and produces a single-variant HTML viewer;
  its paper remains light while viewer chrome can switch themes. Leave the theme unpinned for
  separate light and dark variants.
- **Shared look.** Several diagrams with one look keep theme, spacing and role CSS in one
  Mermaid config JSON passed as `--config FILE`; each source then carries only `class id role`.
  Frontmatter overrides the file per key, and a `theme` in the file pins the theme as
  frontmatter does. Record the full render command, flags included, in each source's
  `%% Render:` comment.
- **themeCSS.** `:root` addresses the SVG itself: declare CSS variables and the canvas
  `background` there; Mermaid nests every other selector under it. In frontmatter use
  descendant selectors: Mermaid discards a frontmatter string containing `<`, `>` or
  `url(data:`, reported as `mermaid/theme-css-dropped`; the `--config` file accepts `>`. For
  one SVG that follows the reader's theme, override the variables in a dark media query; PNG
  and PDF capture the light rules.

  ```css
  :root { --app: #2563eb; --app-fill: #eff6ff; background: #f8fafc; }
  @media (prefers-color-scheme: dark) {
    :root { --app: #93c5fd; --app-fill: #1c2d46; background: #0f172a; }
  }
  .node.app rect { fill: var(--app-fill) !important; stroke: var(--app) !important; }
  ```

- **Layout.** dagre is the default; `config.layout: elk` selects the bundled ELK, which routes
  edges orthogonally. A layout name outside the bundled set falls back to dagre and is reported
  as `mermaid/layout-unsupported`.
- **Layer boundaries.** Under dagre, connect subgraph to subgraph in a layered overview:
  cross-boundary node edges cause Mermaid to ignore a subgraph's own direction, destroying
  horizontal rows. For exact node-to-node edges across boundaries use ELK and inspect the
  result, since the linked subgraph still loses its own direction.
  A layer of disconnected peers can use `direction TB` under a top-to-bottom parent to occupy
  one rank; chains use `direction LR`. Invisible `~~~` links can enforce a peer ordering.
- **Icons.** Use `node@{ icon: "mdi:account", form: "rounded", label: "Customer", pos: "b", h: 44 }`.
  The renderer discovers and caches Iconify packs automatically; unknown names fail with
  suggestions instead of becoming a question-mark tile. Brand icons come from `logos`, role
  icons from `mdi`. First use requires network access; cached packs work offline. When renders
  must match across machines, pin a pack to its npm release: `--icon-pack mdi=mdi@1.2.3`.
- **Label icons.** An icon beside a title is written inside the label: `api["fas:fa-server API"]`.
  Mermaid accepts only the pack names `fa`, `fas`, `far` and `fab` there, with `fa-` before the
  icon name. `fa` draws Font Awesome 4; `fas`, `far` and `fab` draw Font Awesome 6 solid,
  regular and brands. `--icon-pack fas=mdi` serves another Iconify pack under that name, so
  `fas:fa-account` draws `mdi:account`.
- **Icon labels.** If labels have unwanted grey rectangles, put this in `config.themeCSS`:

  ```yaml
  themeCSS: >-
    .icon-shape p, .icon-shape .label, .icon-shape .labelBkg { background-color: transparent !important; }
    .icon-shape .label rect { fill: transparent !important; }
  ```

- **Role palette.** Use `classDef` and `class` to style one role consistently. Colour encodes
  the role (caller, service, store, external); a dashed or dotted border encodes status (new,
  planned). A part keeps its role in every diagram of a set.
- **Named edges.** Name an edge to style it: `api read@--> db`, then `class read storeLine`
  with a stroke-only `classDef`, because a `fill` floods the edge's curve. Choose ids outside
  Mermaid's keywords, such as `call` and `end`. A named edge keeps its style when edges are
  added; `linkStyle` indices follow declaration order, invisible links included, and shift.
  The class name also lands on the edge's path, so a shared look colours arrows by role:
  `.flowchart-link.store { stroke: var(--store) !important; }`, with
  `.arrowMarkerPath { fill: context-stroke; stroke: none; }` for matching arrowheads.
- **Fonts.** Keep the default font unless a specific font has been verified in the actual
  renderer. Some Mermaid theme paths discard hyphenated font-family values; label measurement
  can then disagree with the intended font.
- **Exports.** PNG uses the SVG's natural viewBox dimensions at the requested scale, not an
  arbitrary 800-pixel browser viewport. PDF is one page at the diagram's size. HTML embeds the
  compiled SVG, so the recipient needs neither Mermaid nor network access. SVG is written one
  element per line, so a checked-in file diffs by element.
- **Evidence and scope.** Name source files in `%% Evidence:` comments. Split long request paths
  into sequence diagrams or drill-downs instead of shrinking an entire system into one screen.
