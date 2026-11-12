"""Build a portable HTML viewer. Diagram sources and SVGs remain inert until selected."""

from __future__ import annotations

import base64
import html
import json
import re
from importlib.resources import files

from mermaid_arch.models import Engine


def build_html(
    *,
    title: str,
    engine: Engine,
    source: str,
    light_svg: str,
    dark_svg: str | None,
    background: str = "white",
) -> str:
    template = (files("mermaid_arch") / "assets" / "viewer.html").read_text(encoding="utf-8")

    def encode(text: str) -> str:
        return base64.b64encode(text.encode("utf-8")).decode("ascii")

    def svg_payload(svg: str) -> str:
        return encode(re.sub(r"\A\s*<\?xml[^>]*\?>\s*", "", svg))

    data = json.dumps(
        {
            "title": title,
            "engine": engine.value,
            "source": encode(source),
            "background": background,
        },
        ensure_ascii=True,
    ).replace("<", "\\u003c")
    replacements = {
        "@@TITLE@@": html.escape(title),
        "@@DATA@@": data,
        "@@LIGHT@@": svg_payload(light_svg),
        "@@DARK@@": "" if dark_svg is None else svg_payload(dark_svg),
    }
    # One substitution pass: a title containing a placeholder cannot alter another field.
    return re.sub(r"@@(?:TITLE|DATA|LIGHT|DARK)@@", lambda m: replacements[m[0]], template)
