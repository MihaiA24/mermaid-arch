"""Shared contracts: engines, diagnostics, render options."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal

Severity = Literal["error", "warning"]
OutputFormat = Literal["svg", "png", "pdf", "html"]
OUTPUT_FORMATS: tuple[OutputFormat, ...] = ("svg", "png", "pdf", "html")


class Engine(StrEnum):
    MERMAID = "mermaid"
    D2 = "d2"


SOURCE_SUFFIXES: dict[str, Engine] = {
    ".mmd": Engine.MERMAID,
    ".mermaid": Engine.MERMAID,
    ".d2": Engine.D2,
}


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One finding about a source or its rendered artifact.

    `code` is a stable `<area>/<rule>` identifier. `line` is 1-based in the source file.
    `hint` names the repair, so a caller can act without reading documentation.
    """

    severity: Severity
    code: str
    message: str
    line: int | None = None
    hint: str | None = None

    def to_json(self) -> dict[str, object]:
        data: dict[str, object] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.line is not None:
            data["line"] = self.line
        if self.hint is not None:
            data["hint"] = self.hint
        return data


def error(
    code: str, message: str, *, line: int | None = None, hint: str | None = None
) -> Diagnostic:
    return Diagnostic("error", code, message, line, hint)


def warning(
    code: str, message: str, *, line: int | None = None, hint: str | None = None
) -> Diagnostic:
    return Diagnostic("warning", code, message, line, hint)


class DiagramError(Exception):
    """No trustworthy artifact can be produced; carries the error diagnostics that say why."""

    diagnostics: tuple[Diagnostic, ...]

    def __init__(self, *diagnostics: Diagnostic) -> None:
        super().__init__("; ".join(f"{d.code}: {d.message}" for d in diagnostics))
        self.diagnostics = diagnostics


def engine_for(source: Path) -> Engine:
    try:
        return SOURCE_SUFFIXES[source.suffix.lower()]
    except KeyError:
        known = ", ".join(SOURCE_SUFFIXES)
        raise DiagramError(
            error(
                "source/unknown-type",
                f"cannot tell the diagram engine from suffix {source.suffix!r}",
                hint=f"name the source with one of: {known}",
            )
        ) from None


@dataclass(frozen=True, slots=True)
class RenderOptions:
    """Explicit overrides. `None` defers to the diagram source, then to the engine default.

    D2 flags override `vars.d2-config` in the source. Mermaid frontmatter `config` overrides
    `mermaid_config` per key, and a theme set in either overrides `theme`/`dark_theme`, so a
    pinned theme renders the same in both variants.
    """

    layout: str | None = None  # D2 only: dagre | elk
    theme: str | None = None  # D2: numeric theme id; Mermaid: theme name
    dark_theme: str | None = None  # D2: SVG dark-mode theme id; both: HTML dark variant
    pad: int | None = None  # D2 only: pixels around the diagram
    scale: float = 2.0  # PNG device pixel ratio
    background: str = "white"  # CSS colour behind PNG, PDF and the HTML light canvas
    mermaid_config: Mapping[str, object] | None = None  # Mermaid only: the shared --config file
    # Mermaid only: pack name written in a source -> Iconify `prefix` or `prefix@version`
    icon_packs: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SvgResult:
    """A rendered diagram: well-formed standalone SVG XML with a viewBox, plus warnings.

    `icon_packs` maps each Iconify pack drawn from, as `prefix` or `prefix@version`, to that
    pack's `lastModified` stamp.
    """

    svg: str
    warnings: tuple[Diagnostic, ...] = ()
    icon_packs: Mapping[str, int | None] = field(default_factory=dict)
