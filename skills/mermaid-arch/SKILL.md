---
name: mermaid-arch
description: Renders repository-owned Mermaid and D2 diagrams to checked SVG, PNG, PDF and standalone HTML using a Python CLI. Use when authoring architecture or workflow diagrams as editable text sources, rendering or repairing .mmd or .d2 files, or exporting diagrams for Markdown documentation. Use archify instead for JSON-authored interactive explainers.
license: MIT
---

# Mermaid Arch

Keep the editable diagram beside its documentation. The renderer is `mermaid-arch`, installed
as a uv tool; commands run from the project working directory.

## Workflow

1. **Choose the engine.** Keep existing sources in their current language. For new sources:
   Mermaid fits Markdown hosts, sequence/state/ER diagrams, Iconify icons and one look shared
   by several diagrams; D2 fits nested architecture containers and built-in dual-theme SVG.
   Both lay out with ELK. Done when one engine and the diagram's scope are chosen.
2. **Establish evidence.** For a code-backed diagram, trace the entry points, dependencies and
   boundaries in the repository. Record the supporting file paths in source comments. Mark
   hypothetical components and illustrative timings explicitly. Done when every architectural
   claim has source evidence or an illustrative label.
3. **Author the source.** For Mermaid, read [Mermaid authoring](references/mermaid-authoring.md)
   and start from [layered architecture](examples/layered-architecture.mmd) or
   [request sequence](examples/request-sequence.mmd). For D2, read
   [D2 authoring](references/d2-authoring.md) and start from
   [layered architecture](examples/layered-architecture.d2) or
   [data pipeline](examples/data-pipeline.d2). Adapt the topology rather than copying facts.
   Keep an overview and drill-downs separate. Done when the source covers the requested scope.
4. **Render.** Run the gate below. Add `-f html` for a shareable pan/zoom viewer or `-f pdf`
   for print. Source config owns the styling: explicit D2 flags override `vars.d2-config`;
   Mermaid frontmatter wins over `--theme` and over a shared `--config` file. Reuse the flags
   in the source's `%% Render:` comment. Consult `mermaid-arch render --help` for options.

   ```sh
   mermaid-arch render docs/architecture.mmd -f svg -f png --json
   ```

   A render/check/conversion failure writes no outputs for that source, preserving the last
   good files. Batch sources are independent. Done when the receipt reports `ok: true`.
5. **Repair.** Apply each diagnostic's `hint`, preserving the source meaning, and rerun the
   complete render command. Done when every error is resolved and each warning is fixed or
   explicitly justified.
6. **Inspect.** Open/read the actual PNG. Check labels, arrow endpoints, crossings, contrast,
   clipping and text size at the intended viewing width. Fix the source and rerender if needed.
   This image inspection is the perceptual check; successful compilation is not visual proof.
   Done when the reader can follow the requested story without deciphering the layout.
7. **Deliver.** Link the requested outputs and name the editable source. Mention deliberate
   omissions or unresolved warnings; distinguish automated checks from visual inspection.
   Done when the user can open the artifact and regenerate it from the source.

## Setup branch

When the command is missing, install from the local checkout:

```sh
uv tool install /path/to/mermaid-arch
mermaid-arch doctor
```

For a hosted checkout, use `uv tool install 'git+<repository-url>'` instead. If doctor reports
no browser, run `mermaid-arch install-browser`. D2 ships with its Python wrapper; a working
`d2` on `PATH` takes precedence. Mermaid is bundled; rendering never sends diagram content to
an online renderer. Referenced Iconify packs download once into the local cache. For offline
use, populate the cache beforehand as described in the repository README.
