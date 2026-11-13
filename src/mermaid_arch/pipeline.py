"""One source in, checked artifacts out; conversions finish before output replacement."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

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
    Source,
    SvgResult,
    engine_for,
    error,
    warning,
)
from mermaid_arch.viewer import build_html

STDIN = Path("-")

# A backtick fence's info string cannot contain a backtick (CommonMark).
_FENCE = re.compile(r"(?P<indent> {0,3})(?P<fence>`{3,}(?=[^`]*$)|~{3,})(?P<info>.*)")
_SLUG_GAP = re.compile(r"[^a-z0-9]+")


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
    fonts: tuple[str, ...] = ()
    block: int | None = None

    @property
    def ok(self) -> bool:
        return all(d.severity != "error" for d in self.diagnostics)

    def to_json(self) -> dict[str, object]:
        data: dict[str, object] = {
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
            "fonts": list(self.fonts),
        }
        if self.block is not None:
            data["block"] = self.block
        return data


def render_source(
    source: Source,
    targets: Mapping[OutputFormat, Path],
    options: RenderOptions,
    session: BrowserSession,
    *,
    title: str | None = None,
    verify: bool = False,
) -> Result:
    """Render, check and convert `source` before replacing output files individually.

    Empty `targets` only checks. Compile/check/conversion errors preserve all previous outputs.
    `verify` writes nothing and instead reports each target that differs from its payload.
    """
    diagnostics: list[Diagnostic] = []
    engine = source.engine
    try:
        text = _text(source)
        engine = engine or engine_for(source.path)
        diagnostics += _ignored_options(engine, options, targets)
        main = _render(engine, source, text, options, session)
        diagnostics += main.warnings
        diagnostics += check_svg(main.svg)
        if any(d.severity == "error" for d in diagnostics):
            return _result(source, engine, diagnostics)
        payloads = [
            (fmt, path, _convert(fmt, engine, source, text, main, options, session, title))
            for fmt, path in targets.items()
        ]
        if verify:
            outputs, mismatches = _compare_all(payloads)
            diagnostics += mismatches
        else:
            outputs = _write_all(payloads)
    except DiagramError as exc:
        return _result(source, engine, (*diagnostics, *exc.diagnostics))
    except PlaywrightError as exc:
        # A crashed or closed Chromium fails this source only; the batch goes on.
        detail = exc.message.strip().splitlines()[0] if exc.message.strip() else type(exc).__name__
        failed = error(
            "browser/failed",
            f"Chromium failed while rendering: {detail}",
            hint="run the command again; if it fails again, run `mermaid-arch doctor`",
        )
        return _result(source, engine, (*diagnostics, failed))
    return _result(source, engine, diagnostics, outputs, main)


def output_stem(source: Source) -> str:
    """The default name of `source`'s outputs: `<stem>`, `<stem>-<n>` for block n, `stdin`."""
    if source.path == STDIN:
        return "stdin"
    if source.block is not None:
        return f"{source.path.stem}-{source.block}"
    return source.path.stem


def markdown_sources(path: Path) -> list[Source]:
    """One `Source` per fenced `mermaid` or `d2` code block of the Markdown file `path`."""
    # ponytail: top-level fences only, not ones nested in lists or blockquotes; parse with a
    # CommonMark library if diagrams there must render.
    lines = _read(path).split("\n")
    sources: list[Source] = []
    index = 0
    while index < len(lines):
        opening = _FENCE.fullmatch(lines[index])
        index += 1
        if opening is None:
            continue
        fence, indent = opening["fence"], len(opening["indent"])
        closing = re.compile(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*")
        start = index
        while index < len(lines) and not closing.fullmatch(lines[index]):
            index += 1
        body = lines[start:index]
        index += 1  # past the closing fence; an unclosed fence ends with the file
        kind = next(iter(opening["info"].split()), "").lower()
        if kind not in Engine:
            continue
        # Content lines lose up to as many leading spaces as indent the opening fence.
        text = re.sub(rf"(?m)^ {{0,{indent}}}", "", "".join(f"{line}\n" for line in body))
        sources.append(Source(path, Engine(kind), text, line=start + 1, block=len(sources) + 1))
    if not sources:
        raise DiagramError(
            error(
                "source/no-diagrams",
                "the Markdown file has no fenced ```mermaid or ```d2 code block",
                hint="fence each diagram as a ```mermaid or ```d2 code block, "
                "or remove the file from the command",
            )
        )
    return sources


def _result(
    source: Source,
    engine: Engine | None,
    diagnostics: Sequence[Diagnostic],
    outputs: tuple[Output, ...] = (),
    rendered: SvgResult | None = None,
) -> Result:
    # Engines and checks count lines from the diagram text; receipts count them in `path`.
    shift = source.line - 1
    return Result(
        source.path,
        engine,
        outputs,
        tuple(d if d.line is None else replace(d, line=d.line + shift) for d in diagnostics),
        {} if rendered is None else rendered.icon_packs,
        () if rendered is None else rendered.fonts,
        source.block,
    )


def _text(source: Source) -> str:
    text = _read(source.path) if source.text is None else source.text
    if text.strip():
        return text
    if source.block is not None:
        found = f"diagram block {source.block} is empty"
        repair = "write the diagram into the block, or remove the block"
    elif source.text is not None:
        found, repair = "the diagram text is empty", "pass the diagram's source text"
    else:
        found = "the source file is empty"
        repair = "write the diagram into the file, or remove it from the command"
    line = None if source.block is None else 1  # the block's first line, once shifted
    raise DiagramError(error("source/empty", found, line=line, hint=repair))


def _read(source: Path) -> str:
    try:
        return source.read_text(encoding="utf-8")
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
    if options.target is not None:
        ignored.append(
            warning(
                "options/ignored",
                "--target has no effect on Mermaid sources: it selects a D2 board",
                hint="drop --target for Mermaid sources",
            )
        )
    if options.sketch:
        ignored.append(
            warning(
                "options/ignored",
                "--sketch has no effect on Mermaid sources: it turns on D2's hand-drawn look",
                hint="set `look: handDrawn` in the source's frontmatter config instead",
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
    engine: Engine, source: Source, text: str, options: RenderOptions, session: BrowserSession
) -> SvgResult:
    if engine is Engine.D2:
        return _render_d2(source, options, theme=options.theme, dark_theme=options.dark_theme)
    return render_mermaid(
        session,
        text,
        theme=options.theme,
        config=options.mermaid_config,
        icon_packs=options.icon_packs,
        svg_id=_svg_id(source),
    )


def _render_d2(
    source: Source, options: RenderOptions, *, theme: str | None, dark_theme: str | None
) -> SvgResult:
    return render_d2(
        source.path,
        text=source.text,
        layout=options.layout,
        theme=theme,
        dark_theme=dark_theme,
        pad=options.pad,
        target=options.target,
        sketch=options.sketch,
    )


def _svg_id(source: Source) -> str:
    # Named after the output, not the content, so an edit does not rewrite every id in the SVG,
    # yet two diagrams inlined into one page keep their styles apart.
    slug = _SLUG_GAP.sub("-", output_stem(source).lower()).strip("-")
    return f"mermaid-arch-{slug}" if slug else "mermaid-arch"


def _convert(
    fmt: OutputFormat,
    engine: Engine,
    source: Source,
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
        title=title or output_stem(source),
        engine=engine,
        source=text,
        light_svg=light,
        dark_svg=None if dark == light else dark,
        background=options.background,
    )
    return html.encode()


def _viewer_variants(
    engine: Engine,
    source: Source,
    text: str,
    main: SvgResult,
    options: RenderOptions,
    session: BrowserSession,
) -> tuple[str, str]:
    if engine is Engine.D2:
        light_id, dark_id = d2_viewer_themes(text, options)
        svgs = {
            theme: _render_d2(source, options, theme=theme, dark_theme=theme).svg
            for theme in dict.fromkeys((light_id, dark_id))
        }
        return svgs[light_id], svgs[dark_id]
    dark = render_mermaid(
        session,
        text,
        theme=options.dark_theme or "dark",
        config=options.mermaid_config,
        icon_packs=options.icon_packs,
        svg_id=_svg_id(source),
    ).svg
    return main.svg, dark


def _compare_all(
    payloads: list[tuple[OutputFormat, Path, bytes]],
) -> tuple[tuple[Output, ...], list[Diagnostic]]:
    """Receipts for the targets already holding their payload, errors for the others."""
    matching: list[Output] = []
    mismatches: list[Diagnostic] = []
    for fmt, target, data in payloads:
        try:
            current = target.read_bytes()
        except OSError as exc:
            mismatches.append(
                error(
                    "output/missing",
                    f"cannot read {target}: {exc.strerror or exc}",
                    hint="render again without --verify to write it",
                )
            )
            continue
        if current == data:
            matching.append(_receipt(fmt, target, data))
        else:
            mismatches.append(
                error(
                    "output/stale",
                    f"{target} differs from what the source renders to now",
                    hint="render again without --verify to update it",
                )
            )
    return tuple(matching), mismatches


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
    return tuple(_receipt(fmt, path, data) for fmt, path, data in payloads)


def _receipt(fmt: OutputFormat, path: Path, data: bytes) -> Output:
    return Output(fmt, path, len(data), hashlib.sha256(data).hexdigest())
