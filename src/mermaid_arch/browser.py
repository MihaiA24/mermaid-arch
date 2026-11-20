"""Headless Chromium for Mermaid rendering and SVG rasterisation, sealed off from the network."""

from __future__ import annotations

import math
import os
import re
import shutil
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path, PurePosixPath
from typing import Literal, Self
from urllib.parse import urlsplit

from playwright.sync_api import Browser, BrowserType, Page, Playwright, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from mermaid_arch.checks import svg_size
from mermaid_arch.models import DiagramError, error

INTERNAL_ORIGIN = "https://mermaid-arch.invalid"
MAX_PNG_SIDE = 16384

_PATH_BROWSERS = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "microsoft-edge",
)
_MACOS_BROWSERS = (
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
# Playwright installs each Chromium build into a `chromium-<revision>` directory of its cache.
_REVISION = re.compile(r"chromium-\d+")
_LAUNCH_TIMEOUT_MS = 30_000
_HTML = "text/html; charset=utf-8"
_CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".css": "text/css; charset=utf-8",
    ".html": _HTML,
}
_INSTALL_HINT = (
    "run `mermaid-arch install-browser`, or set MERMAID_ARCH_CHROMIUM to a Chromium/Chrome "
    "executable"
)

# Skia stamps the Info dictionary with the time of printing; nothing else varies between runs.
_PDF_TIMESTAMP = re.compile(rb"/(?:CreationDate|ModDate) \(D:[^)]*\)")

# Replaces the page body with the SVG parsed as XML, at exactly its viewBox size: Mermaid roots
# carry width="100%" and a max-width style that would otherwise shrink or stretch them.
_MOUNT_SVG = """async ({svg, width, height}) => {
  const parsed = new DOMParser().parseFromString(svg, 'image/svg+xml');
  const root = document.importNode(parsed.documentElement, true);
  Object.assign(root.style, {
    display: 'block', width: `${width}px`, height: `${height}px`,
    maxWidth: 'none', maxHeight: 'none', margin: '0',
  });
  document.body.replaceChildren(root);
  await Promise.all(Array.from(root.querySelectorAll('image'),
                               (image) => image.decode?.().catch(() => {})));
  await document.fonts.ready;
}"""

# Playwright's default viewport, restored before printing so a PDF never depends on the PNG
# rasterised before it.
_VIEWPORT = {"width": 1280, "height": 720}
_CANVAS = (
    '<!doctype html><html><head><meta charset="utf-8"><style>'
    "html, body { margin: 0; padding: 0; overflow: hidden; }"
    "</style></head><body></body></html>"
)
# An invalid colour leaves a style property as it was, so clear it first.
_SET_BACKGROUND = (
    "bg => { const s = document.documentElement.style; s.background = ''; s.background = bg; }"
)


class BrowserSession:
    """Lazily launched headless Chromium shared by Mermaid rendering and rasterisation."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._executable = ""
        self._origin = ""
        self._kept: tuple[str, Page] | None = None
        self._kept_files: Mapping[str, bytes | str | Path] = {}
        self._canvas: Page | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self._kept = self._canvas = None
        if self._browser is not None:
            with suppress(PlaywrightError):
                self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def describe(self) -> dict[str, str]:
        browser = self._launched()
        return {"executable": self._executable, "version": browser.version, "origin": self._origin}

    @contextmanager
    def page(
        self,
        files: Mapping[str, bytes | str | Path],
        *,
        scale: float = 1.0,
        color_scheme: Literal["light", "dark"] = "light",
    ) -> Iterator[Page]:
        """A fresh page at `INTERNAL_ORIGIN + "/"` that can load `files` (URL path → body) only."""
        page = self._open(lambda route: _serve(route, files), scale, color_scheme)
        try:
            page.goto(INTERNAL_ORIGIN + "/")
            yield page
        finally:
            # A browser that died has taken the context along; raising here would mask why.
            with suppress(PlaywrightError):
                page.context.close()

    @contextmanager
    def kept_page(self, files: Mapping[str, bytes | str | Path], *, script: str) -> Iterator[Page]:
        """A page like `page()`'s that has run `script` first, in a tab kept from call to call.

        Chromium holds `script` with the tab, so a large library is sent once, not once per
        page. Each call still gets a new document, which shares no JavaScript state with the
        last one. A call that raises closes the tab, and the next call opens another.
        """
        if self._kept is None or self._kept[0] != script or self._kept[1].is_closed():
            self._drop_kept()
            page = self._open(lambda route: _serve(route, self._kept_files), script=script)
            self._kept = (script, page)
        self._kept_files = files
        page = self._kept[1]
        try:
            page.goto(INTERNAL_ORIGIN + "/")
            yield page
        except BaseException:
            self._drop_kept()
            raise

    def _drop_kept(self) -> None:
        kept, self._kept = self._kept, None
        if kept is not None:
            with suppress(PlaywrightError):
                kept[1].context.close()

    @contextmanager
    def canvas(self, background: str) -> Iterator[Page]:
        """A blank page with `background` behind it, in a tab kept from call to call.

        Callers replace the body and set the viewport they need. A call that raises closes
        the tab, and the next call opens another.
        """
        page = self._canvas
        if page is None or page.is_closed():
            self._canvas = None
            page = self._open(lambda route: _serve(route, {"/": _CANVAS}))
            self._canvas = page
        try:
            if page.url != INTERNAL_ORIGIN + "/":
                page.goto(INTERNAL_ORIGIN + "/")
            page.evaluate(_SET_BACKGROUND, background)
            yield page
        except BaseException:
            self._canvas = None
            with suppress(PlaywrightError):
                page.context.close()
            raise

    def _open(
        self,
        serve: Callable[[Route], None],
        scale: float = 1.0,
        color_scheme: Literal["light", "dark"] = "light",
        script: str | None = None,
    ) -> Page:
        """A blank page in a context of its own, where `serve` answers every request."""
        context = self._launched().new_context(
            device_scale_factor=scale, color_scheme=color_scheme, service_workers="block"
        )
        try:
            context.route("**/*", serve)
            context.route_web_socket("**/*", lambda socket: socket.close())
            page = context.new_page()
            if script is not None:
                # Chromium runs this at the start of every document while the CDP session has
                # Page enabled. Playwright's add_init_script would wrap the script in a
                # function, where a library's top-level `var` is no longer a global.
                cdp = context.new_cdp_session(page)
                cdp.send("Page.enable")
                cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": script})
        except BaseException:
            with suppress(PlaywrightError):
                context.close()
            raise
        return page

    def _launched(self) -> Browser:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        self._browser = None
        playwright = self._playwright or sync_playwright().start()
        try:
            self._browser, self._executable, self._origin = _launch(playwright.chromium)
        except BaseException:
            if self._playwright is None:
                playwright.stop()
            raise
        self._playwright = playwright
        return self._browser


def svg_to_png(
    session: BrowserSession, svg: str, *, scale: float = 2.0, background: str = "white"
) -> bytes:
    width, height = _svg_size(svg)
    pixel_width, pixel_height = math.ceil(width * scale), math.ceil(height * scale)
    if max(pixel_width, pixel_height) > MAX_PNG_SIDE:
        fit = math.floor(MAX_PNG_SIDE / max(width, height) * 100) / 100
        repair = f"render the PNG with --scale {fit:g} or lower, or" if fit > 0 else "instead"
        raise DiagramError(
            error(
                "png/too-large",
                f"a PNG at scale {scale:g} would be {pixel_width}x{pixel_height} pixels; "
                f"each side is limited to {MAX_PNG_SIDE}",
                hint=f"{repair} deliver the SVG, which has no pixel limit",
            )
        )
    # Rasterise at final pixel dimensions. Playwright rounds fractional CSS clips before
    # applying the device ratio, which can lose the final row of a Mermaid diagram.
    with session.canvas(background) as page:
        page.set_viewport_size({"width": pixel_width, "height": pixel_height})
        page.evaluate(_MOUNT_SVG, {"svg": svg, "width": width * scale, "height": height * scale})
        return page.screenshot(
            omit_background=True,
            animations="disabled",
            type="png",
        )


def svg_to_pdf(session: BrowserSession, svg: str, *, background: str = "white") -> bytes:
    width, height = _svg_size(svg)
    with session.canvas(background) as page:
        page.set_viewport_size(_VIEWPORT)
        page.evaluate(_MOUNT_SVG, {"svg": svg, "width": width, "height": height})
        pdf = page.pdf(
            width=f"{width}px",
            height=f"{height}px",
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            print_background=True,
            page_ranges="1",
        )
    # Blank the timestamps byte for byte so every xref offset stays valid.
    return _PDF_TIMESTAMP.sub(lambda stamp: b" " * len(stamp[0]), pdf, count=2)


def _launch(chromium: BrowserType) -> tuple[Browser, str, str]:
    configured = os.environ.get("MERMAID_ARCH_CHROMIUM")
    if configured:
        try:
            browser = chromium.launch(executable_path=configured, timeout=_LAUNCH_TIMEOUT_MS)
        except PlaywrightError as exc:
            raise DiagramError(
                error(
                    "env/browser-missing",
                    f"MERMAID_ARCH_CHROMIUM={configured} cannot be launched: {_first_line(exc)}",
                    hint="point MERMAID_ARCH_CHROMIUM at a Chromium/Chrome executable, or unset "
                    "it to use `mermaid-arch install-browser` or a browser on PATH",
                )
            ) from None
        return browser, configured, "env"
    failures: list[str] = []
    for executable, origin in _candidates(chromium):
        try:
            browser = chromium.launch(executable_path=executable, timeout=_LAUNCH_TIMEOUT_MS)
        except PlaywrightError as exc:
            failures.append(f"{executable}: {_first_line(exc)}")
            continue
        return browser, executable, origin
    tried = "; ".join(failures) if failures else "no Chromium, Chrome or Edge executable found"
    raise DiagramError(
        error(
            "env/browser-missing",
            f"no headless Chromium could be launched ({tried})",
            hint=_INSTALL_HINT,
        )
    )


def _candidates(chromium: BrowserType) -> Iterator[tuple[str, str]]:
    expected = Path(chromium.executable_path)
    if expected.is_file():
        yield chromium.executable_path, "playwright"
    # Another Playwright version may have installed a different revision that runs just as well.
    # ponytail: tries only the expected revision's relative executable path, so a revision laid
    # out differently is missed; read each revision's own layout if that ever happens.
    revision = next((path for path in expected.parents if _REVISION.fullmatch(path.name)), None)
    if revision is not None:
        relative = expected.relative_to(revision)
        others = [
            other
            for other in revision.parent.glob("chromium-*")
            if _REVISION.fullmatch(other.name) and other != revision
        ]
        others.sort(key=lambda other: int(other.name.removeprefix("chromium-")), reverse=True)
        for other in others:
            if (other / relative).is_file():
                yield str(other / relative), "playwright"
    for name in _PATH_BROWSERS:
        if found := shutil.which(name):
            yield found, "system"
    for bundle in _MACOS_BROWSERS:
        if Path(bundle).is_file():
            yield bundle, "system"


def _first_line(exc: PlaywrightError) -> str:
    return exc.message.strip().splitlines()[0] if exc.message.strip() else type(exc).__name__


def _serve(route: Route, files: Mapping[str, bytes | str | Path]) -> None:
    url = urlsplit(route.request.url)
    body = files.get(url.path) if f"{url.scheme}://{url.netloc}" == INTERNAL_ORIGIN else None
    if body is None:
        route.abort("blockedbyclient")
        return
    if url.path == "/":
        content_type = _HTML
    else:
        suffix = PurePosixPath(url.path).suffix.lower()
        content_type = _CONTENT_TYPES.get(suffix, "application/octet-stream")
    if isinstance(body, Path):
        route.fulfill(path=body, content_type=content_type)
    else:
        route.fulfill(body=body, content_type=content_type)


def _svg_size(svg: str) -> tuple[float, float]:
    try:
        width, height = svg_size(svg)
        if not all(math.isfinite(side) and side > 0 for side in (width, height)):
            raise ValueError("the SVG dimensions must be finite and positive")
        return width, height
    except ValueError as exc:
        raise DiagramError(error("svg/malformed", str(exc))) from None
