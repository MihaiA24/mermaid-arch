"""D2 rendering through the d2 executable, honouring the source's `vars.d2-config`."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from d2_python import D2

from mermaid_arch.models import Diagnostic, DiagramError, RenderOptions, SvgResult, error

LAYOUTS = ("dagre", "elk")
DEFAULT_LIGHT_THEME = "0"
DEFAULT_DARK_THEME = "200"
D2_TIMEOUT_SECONDS = 180

# d2 reads these as flag defaults, which would silently override the source's d2-config.
_OVERRIDING_ENV = ("D2_LAYOUT", "D2_THEME", "D2_DARK_THEME", "D2_PAD")

_THEME_ID = re.compile(r"-?\d+")
_LOCATION = re.compile(r"(?P<path>.+?):(?P<line>\d+):(?P<column>\d+): (?P<message>.*)")
_COMPILE_PREFIX = re.compile(r"failed to compile .+?: (?=.+?:\d+:\d+: |bad usage: )")
_D2_CONFIG = re.compile(r"d2-config\s*:\s*\{(?P<body>[^{}]*(?:\{[^{}]*\}[^{}]*)*)\}")
_CONFIG_THEME = re.compile(r"(?<![\w-])theme-id\s*:\s*(?P<id>-?\d+)")
_CONFIG_DARK_THEME = re.compile(r"(?<![\w-])dark-theme-id\s*:\s*(?P<id>-?\d+)")
_USAGE_NOISE = ("Run with --help to see usage.", "For more information on setup")

_THEME_IDS = "0, 1, 3-8, 100-105 and 300-302 (light), 200 and 201 (dark)"
_HINTS = (
    (
        re.compile(r"connection missing destination"),
        "end the connection with a target shape (`a -> b`) or delete the dangling arrow",
    ),
    (re.compile(r"maps must be terminated with \}"), "close the open `{` block with `}`"),
    (re.compile(r"is not a valid theme ID"), f"use one of d2's theme ids: {_THEME_IDS}"),
    (
        re.compile(r"failed to import"),
        "fix the import path; it resolves relative to the importing file",
    ),
)


def find_d2() -> Path:
    """`$MERMAID_ARCH_D2`, else `d2` on PATH, else the binary bundled with d2-python-wrapper."""
    configured = os.environ.get("MERMAID_ARCH_D2")
    if configured:
        found = shutil.which(configured)
        if found is None:
            raise DiagramError(
                error(
                    "env/d2-missing",
                    f"MERMAID_ARCH_D2={configured!r} is not an executable file",
                    hint="point MERMAID_ARCH_D2 at a d2 binary, or unset it to use the bundled one",
                )
            )
        return Path(found)
    on_path = shutil.which("d2")
    if on_path is not None:
        return Path(on_path)
    try:
        return Path(D2().binary_path)
    except RuntimeError as exc:
        raise DiagramError(
            error(
                "env/d2-missing",
                f"no d2 executable found: {exc}",
                hint="install d2 (https://d2lang.com) on PATH, or set MERMAID_ARCH_D2 to its path",
            )
        ) from None


def d2_version() -> str:
    binary = find_d2()
    try:
        completed = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = str(exc)
    else:
        version = completed.stdout.strip().removeprefix("v")
        if completed.returncode == 0 and version:
            return version
        detail = completed.stderr.strip() or f"exit status {completed.returncode}"
    raise DiagramError(
        error(
            "env/d2-missing",
            f"cannot run {binary} --version: {detail}",
            hint="point MERMAID_ARCH_D2 at a working d2 binary",
        )
    )


def render_d2(
    source: Path,
    *,
    layout: str | None = None,
    theme: str | None = None,
    dark_theme: str | None = None,
    pad: int | None = None,
) -> SvgResult:
    """Render `source` to SVG; only non-`None` arguments become flags, so d2-config applies."""
    args = _flags(layout=layout, theme=theme, dark_theme=dark_theme, pad=pad)
    binary = find_d2()
    env = {key: value for key, value in os.environ.items() if key not in _OVERRIDING_ENV}
    try:
        completed = subprocess.run(
            [str(binary), *args, str(source), "-"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            stdin=subprocess.DEVNULL,
            env=env,
            timeout=D2_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise DiagramError(
            error(
                "d2/compile",
                f"d2 did not finish within {D2_TIMEOUT_SECONDS} s",
                hint="split the diagram into an overview plus drill-down diagrams, "
                "or try the other layout engine",
            )
        ) from None
    except OSError as exc:
        raise DiagramError(
            error(
                "env/d2-missing",
                f"cannot run {binary}: {exc}",
                hint="point MERMAID_ARCH_D2 at a working d2 binary",
            )
        ) from None
    if completed.returncode != 0:
        raise DiagramError(*_errors(completed.stderr, source))
    if not completed.stdout.strip():
        raise DiagramError(
            error(
                "svg/empty",
                "d2 rendered nothing: the source declares no shapes or connections",
                hint="add at least one shape, for example `client -> server`",
            )
        )
    return SvgResult(completed.stdout)


def d2_viewer_themes(source_text: str, options: RenderOptions) -> tuple[str, str]:
    """`(light_id, dark_id)` for the HTML variants: option, else source d2-config, else default.

    A dark id of -1 means "same as light" in d2, so it resolves to the light id.
    """
    declared_light = declared_dark = None
    config = _D2_CONFIG.search(source_text)
    if config is not None:
        if match := _CONFIG_THEME.search(config["body"]):
            declared_light = match["id"]
        if match := _CONFIG_DARK_THEME.search(config["body"]):
            declared_dark = match["id"]
    light = options.theme if options.theme is not None else declared_light or DEFAULT_LIGHT_THEME
    dark = options.dark_theme if options.dark_theme is not None else declared_dark
    if dark is None:
        dark = DEFAULT_DARK_THEME
    return light, light if dark == "-1" else dark


def _flags(
    *, layout: str | None, theme: str | None, dark_theme: str | None, pad: int | None
) -> list[str]:
    problems: list[Diagnostic] = []
    args: list[str] = []
    if layout is not None:
        if layout in LAYOUTS:
            args += ["--layout", layout]
        else:
            problems.append(
                error(
                    "d2/usage",
                    f"unknown D2 layout {layout!r}",
                    hint="use --layout dagre or --layout elk",
                )
            )
    for flag, value in (("--theme", theme), ("--dark-theme", dark_theme)):
        if value is None:
            continue
        if _THEME_ID.fullmatch(value):
            args += [flag, value]
        else:
            problems.append(
                error(
                    "d2/usage",
                    f"{flag} {value!r} is not a D2 theme id: D2 themes are numbered",
                    hint=f"pass a numeric theme id: {_THEME_IDS}",
                )
            )
    if pad is not None:
        if pad >= 0:
            args += ["--pad", str(pad)]
        else:
            problems.append(
                error(
                    "d2/usage", f"--pad must be 0 or more, got {pad}", hint="pass --pad 0 or more"
                )
            )
    if problems:
        raise DiagramError(*problems)
    return args


def _errors(stderr: str, source: Path) -> list[Diagnostic]:
    """One diagnostic per `err:` location line; a usage failure becomes one `d2/usage`."""
    lines = [
        line.removeprefix("err:").strip() for line in stderr.splitlines() if line.startswith("err:")
    ]
    lines = [line for line in lines if line and not line.startswith(_USAGE_NOISE)]
    if not lines:
        detail = stderr.strip() or "no error output"
        return [error("d2/compile", f"d2 failed: {detail}")]
    lines[0] = _COMPILE_PREFIX.sub("", lines[0], count=1)
    if lines[0].startswith("bad usage: "):
        return [error("d2/usage", " ".join(lines).removeprefix("bad usage: "))]

    source_path = source.resolve()
    diagnostics: list[Diagnostic] = []
    for text in lines:
        location = _LOCATION.fullmatch(text)
        if location is None:
            diagnostics.append(error("d2/compile", text, hint=_hint(text)))
            continue
        message = location["message"]
        if Path(location["path"]).resolve() == source_path:
            diagnostics.append(
                error(
                    "d2/compile",
                    f"{message} (column {location['column']})",
                    line=int(location["line"]),
                    hint=_hint(message),
                )
            )
        else:
            # The location is inside an imported file: its line number is not one of ours.
            diagnostics.append(error("d2/compile", text, hint=_hint(message)))
    return diagnostics


def _hint(message: str) -> str | None:
    return next((hint for pattern, hint in _HINTS if pattern.search(message)), None)
