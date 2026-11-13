"""Command line: render, check, doctor, install-browser."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import partial
from pathlib import Path
from time import sleep

from mermaid_arch import __version__
from mermaid_arch.browser import BrowserSession
from mermaid_arch.doctor import run_doctor
from mermaid_arch.models import (
    MARKDOWN_SUFFIXES,
    OUTPUT_FORMATS,
    DiagramError,
    Engine,
    OutputFormat,
    RenderOptions,
    Source,
)
from mermaid_arch.pipeline import STDIN, Result, markdown_sources, output_stem, render_source

WATCH_INTERVAL_SECONDS = 0.5

_CSS_COLOUR = re.compile(r"[A-Za-z0-9#(),.% -]+")
_ICON_PACK = re.compile(r"([a-z0-9-]+)=([a-z0-9-]+(?:@\d+\.\d+\.\d+)?)")
_SUFFIX_FORMATS: dict[str, OutputFormat] = {f".{fmt}": fmt for fmt in OUTPUT_FORMATS}

Job = tuple[Source, dict[OutputFormat, Path]]
# A SOURCE that yields no diagram, such as a missing Markdown file, plans as its failed Result.
Plan = list[Job | Result]
_Render = Callable[[Source, dict[OutputFormat, Path]], Result]


def main(argv: Sequence[str] | None = None) -> int:
    parser, subparsers = _parser()
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return _doctor(as_json=args.json)
    if args.command == "install-browser":
        return subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])
    command = subparsers[args.command]
    _check_usage(args, command)
    formats = _formats(args, command) if args.command == "render" else []
    stdin = sys.stdin.read() if STDIN in args.sources else None
    plan = _plan(args, command, formats, stdin)
    with BrowserSession() as session:
        render = partial(
            render_source,
            options=_options(args),
            session=session,
            title=args.title,
            verify=args.verify,
        )
        if args.watch:
            return _watch(args.sources, partial(_rerender, args, command, formats, render))
        results = [_run(item, render, args) for item in plan]
    ok = all(result.ok for result in results)
    if args.json:
        report = {
            "ok": ok,
            "version": __version__,
            "results": [result.to_json() for result in results],
        }
        print(json.dumps(report, indent=2))
    return 0 if ok else 1


def _parser() -> tuple[argparse.ArgumentParser, dict[str, argparse.ArgumentParser]]:
    parser = argparse.ArgumentParser(
        prog="mermaid-arch",
        description="Render Mermaid and D2 architecture diagrams to checked SVG, PNG, PDF and "
        "standalone HTML.",
    )
    parser.add_argument("--version", action="version", version=f"mermaid-arch {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    render = commands.add_parser("render", help="render sources to files")
    _sources(render)
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
    render.add_argument(
        "--verify",
        action="store_true",
        help="write nothing; fail unless every output file already holds what would be written",
    )
    render.add_argument(
        "--watch",
        action="store_true",
        help="re-render each SOURCE file whenever it changes, until Ctrl-C",
    )
    render.add_argument("--json", action="store_true", help="print one JSON report on stdout")

    check = commands.add_parser("check", help="render and check sources without writing files")
    _sources(check)
    _engine_options(check)
    check.add_argument("--json", action="store_true", help="print one JSON report on stdout")
    # render's output settings, fixed for check, so both commands share one code path
    check.set_defaults(output=None, out_dir=None, title=None, verify=False, watch=False)

    doctor = commands.add_parser("doctor", help="report the engines and browser available")
    doctor.add_argument("--json", action="store_true", help="print one JSON report on stdout")

    commands.add_parser("install-browser", help="download Playwright's headless Chromium")
    return parser, {"render": render, "check": check}


def _sources(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "sources",
        nargs="+",
        type=Path,
        metavar="SOURCE",
        help="diagram file, Markdown file whose ```mermaid and ```d2 blocks are diagrams, "
        "or - for stdin",
    )


def _engine_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--engine",
        type=Engine,
        choices=list(Engine),
        help="engine of every non-Markdown SOURCE (default: from the suffix; required for -)",
    )
    parser.add_argument("--layout", choices=("dagre", "elk"), help="D2 layout engine")
    parser.add_argument("--theme", metavar="T", help="D2 theme id or Mermaid theme name")
    parser.add_argument("--pad", type=_pad, metavar="N", help="D2 padding in pixels")
    parser.add_argument(
        "--target",
        metavar="BOARD",
        help="D2 board to render: '' for the root, layers.NAME, scenarios.NAME or steps.NAME",
    )
    parser.add_argument("--sketch", action="store_true", help="D2 hand-drawn look")
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


def _check_usage(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    stdin = args.sources.count(STDIN)
    if args.watch and args.json:
        parser.error("--watch reports each render as it happens: drop --json")
    if args.watch and args.verify:
        parser.error("--watch writes outputs and --verify writes none: pick one")
    if args.watch and stdin:
        parser.error("--watch cannot watch stdin: pass the diagram as a SOURCE file")
    if stdin > 1:
        parser.error("SOURCE - reads stdin, which can be read only once")
    if stdin and args.engine is None:
        parser.error("SOURCE - has no suffix to tell its engine: pass --engine mermaid|d2")
    if stdin and args.command == "render" and args.output is None:
        parser.error("SOURCE - has no name to derive output names from: pass -o FILE")


def _formats(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[OutputFormat]:
    formats: list[OutputFormat] = list(dict.fromkeys(args.formats or ["svg"]))
    if args.output is None:
        return formats
    output: Path = args.output
    if len(args.sources) != 1:
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
        return [inferred]
    if inferred != formats[0]:
        parser.error(f"-o {output} does not match -f {formats[0]}: use a .{formats[0]} name")
    return formats


def _plan(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    formats: list[OutputFormat],
    stdin: str | None,
) -> Plan:
    plan: Plan = []
    for path in args.sources:
        if path == STDIN:
            sources = [Source(path, args.engine, stdin)]
        elif path.suffix.lower() in MARKDOWN_SUFFIXES:
            try:
                sources = markdown_sources(path)
            except DiagramError as exc:
                plan.append(Result(path, None, (), exc.diagnostics))
                continue
        else:
            sources = [Source(path, args.engine)]
        plan += [(source, _targets(source, args, formats)) for source in sources]
    _check_targets(plan, args, parser)
    return plan


def _targets(
    source: Source, args: argparse.Namespace, formats: list[OutputFormat]
) -> dict[OutputFormat, Path]:
    if args.output is not None:
        return {formats[0]: args.output}
    directory = args.out_dir if args.out_dir is not None else source.path.parent
    return {fmt: directory / f"{output_stem(source)}.{fmt}" for fmt in formats}


def _check_targets(plan: Plan, args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    jobs = [item for item in plan if not isinstance(item, Result)]
    if args.output is not None and len(jobs) > 1:
        parser.error(
            f"-o FILE writes one diagram, but {jobs[0][0].path} has {len(jobs)} diagram blocks: "
            "use --out-dir DIR"
        )
    claimed: dict[Path, Path] = {}
    for source, targets in jobs:
        for target in targets.values():
            key = target.resolve()
            if key in claimed:
                parser.error(
                    f"{claimed[key]} and {source.path} would both write {target}; "
                    "rename one, or render them separately with -o or --out-dir"
                )
            claimed[key] = source.path


def _options(args: argparse.Namespace) -> RenderOptions:
    options = RenderOptions(
        layout=args.layout,
        theme=args.theme,
        pad=args.pad,
        target=args.target,
        sketch=args.sketch,
        mermaid_config=args.config,
        icon_packs=dict(args.icon_packs or ()),
    )
    if args.command == "check":
        return options
    return replace(
        options, dark_theme=args.dark_theme, scale=args.scale, background=args.background
    )


def _run(item: Job | Result, render: _Render, args: argparse.Namespace) -> Result:
    result = item if isinstance(item, Result) else render(*item)
    if not args.json:
        _report(result, check_only=args.command == "check", verify=args.verify)
    return result


def _rerender(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    formats: list[OutputFormat],
    render: _Render,
    path: Path,
) -> None:
    # Planned afresh, so blocks added to or removed from a Markdown file are picked up.
    for item in _plan(args, parser, formats, None):
        if (item.source if isinstance(item, Result) else item[0].path) == path:
            _run(item, render, args)


def _watch(paths: Sequence[Path], render: Callable[[Path], object]) -> int:
    """Render each of `paths`, then again whenever its file changes, until Ctrl-C."""
    # ponytail: polls the listed sources' mtimes only, not D2 imports or the --config file;
    # switch to OS file events plus d2's import graph if edits there must re-render too.
    seen: dict[Path, tuple[int, int] | None] = {}
    try:
        while True:
            for path in paths:
                stamp = _stamp(path)
                if path in seen and seen[path] == stamp:
                    continue
                seen[path] = stamp
                render(path)
            sleep(WATCH_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        return 0


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        status = path.stat()
    except OSError:
        return None
    # The size catches a rewrite within the filesystem's mtime granularity.
    return status.st_mtime_ns, status.st_size


def _report(result: Result, *, check_only: bool, verify: bool) -> None:
    name = f"{result.source}" + ("" if result.block is None else f" (block {result.block})")
    for diagnostic in result.diagnostics:
        where = name if diagnostic.line is None else f"{result.source}:{diagnostic.line}"
        print(
            f"{where}: {diagnostic.severity} {diagnostic.code}: {diagnostic.message}",
            file=sys.stderr,
        )
        if diagnostic.hint:
            print(f"  hint: {diagnostic.hint}", file=sys.stderr)
    if check_only and result.ok:
        print(f"{name}: ok")
    for output in result.outputs:
        if verify:
            print(f"{result.source} == {output.path} (up to date)")
        else:
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
