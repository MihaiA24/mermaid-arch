"""Structural checks on rendered SVG before it is delivered."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from mermaid_arch.models import Diagnostic, error, warning

MAX_ASPECT_RATIO = 4.0
# A lopsided diagram is only hard to read once its long side outgrows a screen: a two-node
# chain or a Gantt chart at Mermaid's 1,264 px page width reads fine at full size.
MIN_FLAGGED_SIDE = 1600.0

_DRAWABLE = frozenset(
    {
        "path",
        "rect",
        "circle",
        "ellipse",
        "polygon",
        "polyline",
        "line",
        "text",
        "image",
        "foreignObject",
        "use",
    }
)
# Content of these elements is only drawn when referenced from elsewhere.
_NON_RENDERED = frozenset(
    {
        "defs",
        "marker",
        "symbol",
        "clipPath",
        "mask",
        "pattern",
        "linearGradient",
        "radialGradient",
        "filter",
        "style",
        "script",
        "title",
        "desc",
        "metadata",
    }
)
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_LENGTH = re.compile(rf"\s*({_NUMBER.pattern})\s*(?:px)?\s*")


def svg_size(svg: str) -> tuple[float, float]:
    """(width, height) from the root viewBox, else numeric width/height; ValueError otherwise."""
    return _size(_root(svg))


def check_svg(svg: str) -> list[Diagnostic]:
    try:
        root = _root(svg)
        width, height = _size(root)
    except ValueError as exc:
        return [error("svg/malformed", str(exc))]
    if width <= 0 or height <= 0:
        return [error("svg/empty", f"the diagram has no area ({_fmt(width)}×{_fmt(height)})")]
    if not _has_drawable(root):
        return [
            error(
                "svg/empty",
                "the SVG contains nothing drawable",
                hint="add at least one node or connection to the diagram",
            )
        ]
    ratio = max(width, height) / min(width, height)
    if ratio > MAX_ASPECT_RATIO and max(width, height) > MIN_FLAGGED_SIDE:
        shape = "taller than wide" if height > width else "wider than tall"
        return [
            warning(
                "svg/aspect-ratio",
                f"the diagram is {_fmt(width)}×{_fmt(height)}, {ratio:.1f}× {shape}; "
                "it will be hard to read on one page or screen",
                hint="flip the flow direction (D2 `direction: right`/`down`, Mermaid "
                "`flowchart LR`/`TB`), switch D2 to the elk layout (`--layout elk`), or split "
                "it into an overview plus drill-down diagrams",
            )
        ]
    return []


def _root(svg: str) -> ET.Element:
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        raise ValueError(f"the SVG is not well-formed XML: {exc}") from None
    if _local(root.tag) != "svg":
        raise ValueError(f"the root element is <{_local(root.tag)}>, not <svg>")
    return root


def _size(root: ET.Element) -> tuple[float, float]:
    view_box = root.get("viewBox")
    if view_box is not None:
        numbers = view_box.replace(",", " ").split()
        if len(numbers) == 4 and all(_NUMBER.fullmatch(n) for n in numbers):
            return float(numbers[2]), float(numbers[3])
    width, height = root.get("width", ""), root.get("height", "")
    width_match, height_match = _LENGTH.fullmatch(width), _LENGTH.fullmatch(height)
    if width_match and height_match:
        return float(width_match[1]), float(height_match[1])
    raise ValueError("the root <svg> has no viewBox and no numeric width and height")


def _has_drawable(element: ET.Element) -> bool:
    for child in element:
        name = _local(child.tag)
        if name in _NON_RENDERED:
            continue
        if name in _DRAWABLE or _has_drawable(child):
            return True
    return False


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _fmt(value: float) -> str:
    return f"{value:g}"
