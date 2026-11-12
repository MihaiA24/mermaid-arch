"""Command line: render, check, doctor, install-browser."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from mermaid_arch import __version__
from mermaid_arch.browser import BrowserSession
from mermaid_arch.doctor import run_doctor
from mermaid_arch.models import OUTPUT_FORMATS, OutputFormat, RenderOptions
from mermaid_arch.pipeline import Result, render_source

_CSS_COLOUR = re.compile(r"[A-Za-z0-9#(),.% -]+")
_ICON_PACK = re.compile(r"([a-z0-9-]+)=([a-z0-9-]+(?:@\d+\.\d+\.\d+)?)")
_SUFFIX_FORMATS: dict[str, OutputFormat] = {f".{fmt}": fmt for fmt in OUTPUT_FORMATS}

Job = tuple[Path, dict[OutputFormat, Path]]


def main(argv: Sequence[str] | None = None) -> int:
    parser, subparsers = _parser()
    args = parser.parse_args(argv)
    if args.command == "render":
        jobs = _render_jobs(args, subparsers["render"])
        options = RenderOptions(
            layout=args.layout,
            theme=args.theme,
            dark_theme=args.dark_theme,
            pad=args.pad,
            scale=args.scale,
            background=args.background,
            mermaid_config=args.config,
            icon_packs=dict(args.icon_packs or ()),
        )
        return _run(jobs, options, title=args.title, as_json=args.json, check_only=False)
    if args.command == "check":
        jobs = [(source, {}) for source in args.sources]
        options = RenderOptions(
            layout=args.layout,
            theme=args.theme,
            pad=args.pad,
            mermaid_config=args.config,
            icon_packs=dict(args.icon_packs or ()),
        )
        return _run(jobs, options, title=None, as_json=args.json, check_only=True)
    if args.command == "doctor":
        return _doctor(as_json=args.json)
    return subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])


def _parser() -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]:
    parser = argparse.ArgumentParser(
        prog="mermaid-arch",
        description="Render Mermaid and D2 architecture diagrams to checked SVG, PNG, PDF and "
        "standalone HTML.",
    )
    parser.add_argument("--version", action="version", version=f"mermaid-arch {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    render = commands.add_parser("render", help="render sources to files")
    render.add_argument("sources", nargs="+", type=Path, metavar="SOURCE")
    render.add_argument(
        "-f",
        dest="formats",
        action="append",
        choices=OUTPUT_FORMATS,
        help="output format; repeat for several (default: svg)",
    )
    destination = render.add_mutually_exclusive_group()
    destination.add_argument(
        "-o", dest="output", type=Path, metavar="FILE", help="output file (one source only)"
    )
    destination.add_argument(
        "--out-dir",
        type=Path,
        metavar="DIR",
        help="write outputs into DIR (default: beside source)",
    )
    _engine_options(render)
    render.add_argument("--dark-theme", metavar="T", help="D2 dark theme id / HTML dark variant")
    render.add_argument("--scale", type=_scale, default=2.0, metavar="X", help="PNG pixel ratio")
    render.add_argument(
        "--background",
        type=_background,
        default="white",
        metavar="CSS",
        help="CSS colour behind PNG, PDF and the HTML light canvas (default: white)",
    )
    render.add_argument("--title", metavar="TEXT", help="HTML viewer title (default: file stem)")
    render.add_argument("--json", action="store_true", help="print one JSON report on stdout")

    check = commands.add_parser("check", help="render and check sources without writing files")
    check.add_argument("sources", nargs="+", type=Path, metavar="SOURCE")
    _engine_options(check)
    check.add_argument("--json", action="store_true", help="print one JSON report on stdout")

    doctor = commands.add_parser("doctor", help="report the engines and browser available")
    doctor.add_argument("--json", action="store_true", help="print one JSON report on stdout")

    commands.add_parser("install-browser", help="download Playwright's headless Chromium")
    return parser, {"render": render, "check": check}


def _engine_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--layout", choices=("dagre", "elk"), help="D2 layout engine")
    parser.add_argument("--theme", metavar="T", help="D2 theme id or Mermaid theme name")
    parser.add_argument("--pad", type=_pad, metavar="N", help="D2 padding in pixels")
    parser.add_argument(
        "--config",
        type=_mermaid_config,
        metavar="FILE",
        help="Mermaid config JSON shared by every source; a source's frontmatter overrides it",
    )
    parser.add_argument(
        "--icon-pack",
        dest="icon_packs",
        action="append",
        type=_icon_pack,
        metavar="NAME=PREFIX[@X.Y.Z]",
        help="serve Iconify pack PREFIX, optionally pinned to release X.Y.Z, as Mermaid pack "
        "NAME; fas=mdi draws fas:fa-account in a label from mdi:account",
    )


def _scale(value: str) -> float:
    try:
        scale = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
    if not math.isfinite(scale) or scale <= 0:
        raise argparse.ArgumentTypeError(f"must be a number greater than 0, got {value!r}")
    return scale


def _pad(value: str) -> int:
    try:
        pad = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number") from None
    if pad < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {pad}")
    return pad


def _background(value: str) -> str:
    if not _CSS_COLOUR.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a CSS colour: use a name, #hex, rgb(...)/hsl(...) or transparent"
        )
    return value


def _mermaid_config(value: str) -> dict[str, object]:
    try:
        config = json.loads(Path(value).read_text(encoding="utf-8"))
    except OSError as exc:
        raise argparse.ArgumentTypeError(f"cannot read {value}: {exc.strerror or exc}") from None
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value} is not UTF-8 JSON: {exc}") from None
    if not isinstance(config, dict):
        raise argparse.ArgumentTypeError(f"{value} must hold one JSON object of Mermaid config")
    return config


def _icon_pack(value: str) -> tuple[str, str]:
    match = _ICON_PACK.fullmatch(value)
    if match is None:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not NAME=PREFIX or NAME=PREFIX@X.Y.Z, for example fas=mdi@1.2.3"
        )
    return match[1], match[2]


def _render_jobs(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[Job]:
    sources: list[Path] = args.sources
    formats: list[OutputFormat] = list(dict.fromkeys(args.formats or ["svg"]))
    if args.output is not None:
        output: Path = args.output
        if len(sources) != 1:
            parser.error("-o FILE takes exactly one SOURCE; use --out-dir DIR for several")
        if len(formats) > 1:
            parser.error("-o FILE writes one file; pass at most one -f")
        inferred = _SUFFIX_FORMATS.get(output.suffix.lower())
        if args.formats is None:
            if inferred is None:
                parser.error(
                    f"cannot infer the format of {output}: name it .svg, .png, .pdf or .html, "
                    "or pass -f"
                )
            formats = [inferred]
        elif inferred != formats[0]:
            parser.error(f"-o {output} does not match -f {formats[0]}: use a .{formats[0]} name")
        return [(sources[0], {formats[0]: output})]

    jobs: list[Job] = []
    claimed: dict[Path, Path] = {}
    for source in sources:
        directory = args.out_dir if args.out_dir is not None else source.parent
        targets: dict[OutputFormat, Path] = {}
        for fmt in formats:
            target = directory / f"{source.stem}.{fmt}"
            key = target.resolve()
            if key in claimed:
                parser.error(
                    f"{claimed[key]} and {source} would both write {target}; "
                    "rename one, or render them separately with -o or --out-dir"
                )
            claimed[key] = source
            targets[fmt] = target
        jobs.append((source, targets))
    return jobs


def _run(
    jobs: list[Job],
    options: RenderOptions,
    *,
    title: str | None,
    as_json: bool,
    check_only: bool,
) -> int:
    results: list[Result] = []
    with BrowserSession() as session:
        for source, targets in jobs:
            result = render_source(source, targets, options, session, title=title)
            results.append(result)
            if not as_json:
                _report(result, check_only=check_only)
    ok = all(result.ok for result in results)
    if as_json:
        report = {
            "ok": ok,
            "version": __version__,
            "results": [result.to_json() for result in results],
        }
        print(json.dumps(report, indent=2))
    return 0 if ok else 1


def _report(result: Result, *, check_only: bool) -> None:
    for diagnostic in result.diagnostics:
        where = result.source if diagnostic.line is None else f"{result.source}:{diagnostic.line}"
        print(
            f"{where}: {diagnostic.severity} {diagnostic.code}: {diagnostic.message}",
            file=sys.stderr,
        )
        if diagnostic.hint:
            print(f"  hint: {diagnostic.hint}", file=sys.stderr)
    if check_only and result.ok:
        print(f"{result.source}: ok")
    for output in result.outputs:
        print(f"{result.source} -> {output.path} ({output.size:,} bytes)")


def _doctor(*, as_json: bool) -> int:
    checks, ok = run_doctor()
    if as_json:
        print(json.dumps({"ok": ok, "checks": [check.to_json() for check in checks]}, indent=2))
    else:
        width = max(len(check.name) for check in checks)
        for check in checks:
            print(f"{check.name:<{width}}  {check.status:<7}  {check.detail}")
            if check.hint:
                print(f"{'':<{width}}  hint: {check.hint}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
