# D2 authoring

- **Reproducible source.** Declare rendering settings inside the diagram:

  ```d2
  vars: {
    d2-config: {
      layout-engine: elk
      pad: 40
      theme-id: 0
      dark-theme-id: 200
    }
  }
  ```

  Source config makes a render independent of a project-specific board list. CLI flags are
  explicit overrides; no flags means source settings, then D2 defaults.
- **Engine baseline.** The Python wrapper bundles D2 v0.6.8. A `d2` on `PATH`, or the explicit
  `MERMAID_ARCH_D2` executable, takes precedence. Syntax must work with the chosen engine:
  `mermaid-arch doctor` names the binary in use and its origin; check it before depending on
  a newer feature.
- **Ranking.** Start with ELK for layered architecture stacks. Dagre can stretch nested stacks
  into unreadable aspect ratios; inspect before switching a proven layout.
- **Containment.** Each layer is a container. Set `direction: right` within horizontal layers
  and `direction: down` for the overall read path. Connect containers to containers; keep
  child-level edges inside their own layer. Cross-boundary child edges distort container ranks.
- **Shared classes.** Declare one `classes` entry per semantic role and reuse it with `class:`.
  Cylinder means persistence, person means caller, document means input, package means module,
  queue means buffered work, and step means transformation. Shapes communicate without a legend
  of arbitrary colors. `style.multiple: true` represents several instances, not a sequence.
- **Theme compatibility.** Prefer semantic themes to explicit fills and font colors. An explicit
  style wins over both light and dark themes and can make the dark variant unreadable. If a
  custom palette is necessary, inspect both variants; theme toggling cannot repair fixed colors.
- **Title.** A markdown text shape with `near: top-center` can carry a heading and short scope
  note without entering the graph's edge topology. Keep long caveats in prose beside the diagram.
- **Assets.** Use local icons or embedded assets; D2 can fetch remote asset URLs during compilation.
  Local sources keep rendering reproducible and avoid leaking the reader's network metadata.
- **Overview and detail.** Keep the component map separate from stage-by-stage pipeline and
  request-sequence files. Label the drill-down in the overview instead of duplicating its edges.
  A file with `layers`, `scenarios` or `steps` renders one board per command: `--target ''` is
  the root board, `--target layers.NAME` one layer; record the flag in the `# Render:` comment.
- **Evidence.** Put supporting repository file paths in `# Evidence:` comments. Compiling a
  diagram checks syntax and SVG structure, not whether the architecture matches the code.
- **Outputs.** SVG includes its declared dark theme. PNG and PDF raster/print the light media
  scheme; use `--theme 200 --dark-theme 200 --background '#1e1e2e'` for a dark export. HTML
  compiles separate light and dark variants and retains its editable source. `--sketch`
  renders the hand-drawn look.
- **Markdown.** A fenced `d2` block in a Markdown file renders like a file beside it: imports
  resolve relative to the Markdown file.
