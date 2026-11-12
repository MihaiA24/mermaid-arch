"""One source in, checked artifacts out; conversions finish before output replacement."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from mermaid_arch.browser import BrowserSession, svg_to_pdf, svg_to_png
from mermaid_arch.checks import check_svg
from mermaid_arch.d2 import d2_viewer_themes, render_d2
from mermaid_arch.mermaid import render_mermaid
from mermaid_arch.models import (
    Diagnostic,
    DiagramError,
    Engine,
    OutputFormat,
    RenderOptions,
    SvgResult,
    engine_for,
    error,
    warning,
)
from mermaid_arch.viewer import build_html


@dataclass(frozen=True, slots=True)
class Output:
    format: OutputFormat
    path: Path
    size: int
    sha256: str

    def to_json(self) -> dict[str, object]:
        return {
            "format": self.format,
            "path": str(self.path),
            "bytes": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class Result:
    source: Path
    engine: Engine | None
    outputs: tuple[Output, ...]
    diagnostics: tuple[Diagnostic, ...]
    icon_packs: Mapping[str, int | None] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(d.severity != "error" for d in self.diagnostics)

    def to_json(self) -> dict[str, object]:
        return {
            "source": str(self.source),
            "engine": None if self.engine is None else self.engine.value,
            "ok": self.ok,
            "outputs": [output.to_json() for output in self.outputs],
            "diagnostics": [diagnostic.to_json() for diagnostic in self.diagnostics],
            "icon_packs": [
                {"prefix": prefix, "version": version or None, "last_modified": stamp}
                for spec, stamp in sorted(self.icon_packs.items())
                for prefix, _, version in [spec.partition("@")]
            ],
        }


def render_source(
    source: Path,
    targets: Mapping[OutputFormat, Path],
    options: RenderOptions,
    session: BrowserSession,
    *,
    title: str | None = None,
) -> Result:
    """Render, check and convert `source` before replacing output files individually.

    Empty `targets` only checks. Compile/check/conversion errors preserve all previous outputs.
    """
    diagnostics: list[Diagnostic] = []
    engine: Engine | None = None
    try:
        text = _read(source)
        engine = engine_for(source)
        diagnostics += _ignored_options(engine, options, targets)
        main = _render(engine, source, text, options, session)
        diagnostics += main.warnings
        diagnostics += check_svg(main.svg)
        if any(d.severity == "error" for d in diagnostics):
            return Result(source, engine, (), tuple(diagnostics))
        payloads = [
            (fmt, path, _convert(fmt, engine, source, text, main, options, session, title))
            for fmt, path in targets.items()
        ]
        outputs = _write_all(payloads)
    except DiagramError as exc:
        return Result(source, engine, (), (*diagnostics, *exc.diagnostics))
    return Result(source, engine, outputs, tuple(diagnostics), main.icon_packs)


def _read(source: Path) -> str:
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DiagramError(error("source/missing", f"no such file: {source}")) from None
    except UnicodeDecodeError as exc:
        raise DiagramError(
            error(
                "source/encoding",
                f"the source is not UTF-8 text ({exc.reason} at byte {exc.start})",
                hint="re-save the file as UTF-8",
            )
        ) from None
    except OSError as exc:
        raise DiagramError(
            error("source/missing", f"cannot read {source}: {exc.strerror or exc}")
        ) from None
    if not text.strip():
        raise DiagramError(
            error(
                "source/empty",
                "the source file is empty",
                hint="write the diagram into the file, or remove it from the command",
            )
        )
    return text


def _ignored_options(
    engine: Engine, options: RenderOptions, targets: Mapping[OutputFormat, Path]
) -> list[Diagnostic]:
    if engine is Engine.D2:
        given = {"--config": options.mermaid_config is not None, "--icon-pack": options.icon_packs}
        return [
            warning(
                "options/ignored",
                f"{flag} has no effect on D2 sources: it configures Mermaid",
                hint=f"drop {flag} for D2 sources",
            )
            for flag, used in given.items()
            if used
        ]
    ignored: list[Diagnostic] = []
    if options.layout is not None:
        ignored.append(
            warning(
                "options/ignored",
                "--layout has no effect on Mermaid sources: it selects the D2 layout engine",
                hint="drop --layout; a Mermaid diagram declares its layout in its frontmatter",
            )
        )
    if options.pad is not None:
        ignored.append(
            warning(
                "options/ignored",
                "--pad has no effect on Mermaid sources: it sets D2's padding",
                hint="drop --pad for Mermaid sources",
            )
        )
    if options.dark_theme is not None and "html" not in targets:
        ignored.append(
            warning(
                "options/ignored",
                "--dark-theme has no effect here: for Mermaid it only themes the HTML viewer's "
                "dark variant, and no html output was requested",
                hint="add -f html, or drop --dark-theme",
            )
        )
    return ignored


def _render(
    engine: Engine, source: Path, text: str, options: RenderOptions, session: BrowserSession
) -> SvgResult:
    if engine is Engine.D2:
        return render_d2(
            source,
            layout=options.layout,
            theme=options.theme,
            dark_theme=options.dark_theme,
            pad=options.pad,
        )
    return render_mermaid(
        session,
        text,
        theme=options.theme,
        config=options.mermaid_config,
        icon_packs=options.icon_packs,
    )


def _convert(
    fmt: OutputFormat,
    engine: Engine,
    source: Path,
    text: str,
    main: SvgResult,
    options: RenderOptions,
    session: BrowserSession,
    title: str | None,
) -> bytes:
    if fmt == "svg":
        return main.svg.encode()
    if fmt == "png":
        return svg_to_png(session, main.svg, scale=options.scale, background=options.background)
    if fmt == "pdf":
        return svg_to_pdf(session, main.svg, background=options.background)
    light, dark = _viewer_variants(engine, source, text, main, options, session)
    html = build_html(
        title=title or source.stem,
        engine=engine,
        source=text,
        light_svg=light,
        dark_svg=None if dark == light else dark,
        background=options.background,
    )
    return html.encode()


def _viewer_variants(
    engine: Engine,
    source: Path,
    text: str,
    main: SvgResult,
    options: RenderOptions,
    session: BrowserSession,
) -> tuple[str, str]:
    if engine is Engine.D2:
        light_id, dark_id = d2_viewer_themes(text, options)
        light = render_d2(
            source, layout=options.layout, theme=light_id, dark_theme=light_id, pad=options.pad
        ).svg
        if dark_id == light_id:
            return light, light
        dark = render_d2(
            source, layout=options.layout, theme=dark_id, dark_theme=dark_id, pad=options.pad
        ).svg
        return light, dark
    dark = render_mermaid(
        session,
        text,
        theme=options.dark_theme or "dark",
        config=options.mermaid_config,
        icon_packs=options.icon_packs,
    ).svg
    return main.svg, dark


def _write_all(payloads: list[tuple[OutputFormat, Path, bytes]]) -> tuple[Output, ...]:
    """Stage all payloads first; each replacement is atomic, not a multi-file transaction."""
    staged: list[Path] = []
    target = Path()
    try:
        for _, target, data in payloads:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Created through open(), so the output gets the umask's mode; mkstemp forces 0600.
            temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            staged.append(temp)
            temp.write_bytes(data)
        for temp, (_, target, _) in zip(staged, payloads, strict=True):
            os.replace(temp, target)
    except OSError as exc:
        for temp in staged:
            temp.unlink(missing_ok=True)
        raise DiagramError(
            error(
                "output/write",
                f"cannot write {target}: {exc.strerror or exc}",
                hint="choose a writable output location with -o or --out-dir",
            )
        ) from None
    return tuple(
        Output(fmt, path, len(data), hashlib.sha256(data).hexdigest())
        for fmt, path, data in payloads
    )
