"""Environment report: which engines and browser mermaid-arch can use on this machine."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Literal

from playwright.sync_api import Error as PlaywrightError

from mermaid_arch import __version__
from mermaid_arch.browser import BrowserSession
from mermaid_arch.d2 import d2_version, find_d2
from mermaid_arch.mermaid import MERMAID_VERSION, icon_cache_dir
from mermaid_arch.models import DiagramError


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: Literal["ok", "missing"]
    detail: str
    hint: str | None = None

    def to_json(self) -> dict[str, object]:
        data: dict[str, object] = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.hint is not None:
            data["hint"] = self.hint
        return data


REQUIRED = ("d2", "mermaid", "chromium")


def run_doctor() -> tuple[tuple[Check, ...], bool]:
    """All checks plus overall ok, which needs d2, Mermaid and Chromium."""
    checks = (
        Check("mermaid-arch", "ok", __version__),
        _d2(),
        _mermaid(),
        _chromium(),
        _icon_cache(),
    )
    ok = all(check.status == "ok" for check in checks if check.name in REQUIRED)
    return checks, ok


def _d2() -> Check:
    try:
        binary = find_d2()
        version = d2_version()
    except DiagramError as exc:
        return _missing("d2", exc)
    return Check("d2", "ok", f"{version} at {binary}")


def _mermaid() -> Check:
    asset = files("mermaid_arch") / "assets" / "mermaid.min.js"
    if not asset.is_file():
        return Check(
            "mermaid",
            "missing",
            f"the bundled mermaid.min.js ({MERMAID_VERSION}) is not in the installed package",
            hint="reinstall: uv tool install --reinstall mermaid-arch",
        )
    return Check("mermaid", "ok", f"{MERMAID_VERSION} (bundled)")


def _chromium() -> Check:
    try:
        with BrowserSession() as session:
            info = session.describe()
    except DiagramError as exc:
        return _missing("chromium", exc)
    except PlaywrightError as exc:
        return Check(
            "chromium",
            "missing",
            f"Chromium failed to start: {exc.message}",
            hint="run `mermaid-arch install-browser`, or set MERMAID_ARCH_CHROMIUM to a working "
            "Chromium or Chrome executable",
        )
    return Check("chromium", "ok", f"{info['version']} at {info['executable']} ({info['origin']})")


def _icon_cache() -> Check:
    directory = icon_cache_dir()
    if not directory.is_dir():
        return Check(
            "icon-cache",
            "missing",
            f"{directory} does not exist yet",
            hint="icon packs download on first use; offline, copy Iconify `<prefix>.json` packs "
            "into that directory",
        )
    packs = ", ".join(
        f"{path.stem} ({_modified(path)})" for path in sorted(directory.glob("*.json"))
    )
    return Check("icon-cache", "ok", f"{directory}: {packs or 'no packs cached yet'}")


def _modified(path: Path) -> str:
    """The pack's Iconify release date: differing dates explain icons that differ by machine."""
    try:
        stamp = json.loads(path.read_text(encoding="utf-8"))["lastModified"]
        return datetime.fromtimestamp(stamp, UTC).date().isoformat()
    except (OSError, ValueError, LookupError, TypeError, OverflowError):
        return "undated"


def _missing(name: str, exc: DiagramError) -> Check:
    diagnostic = exc.diagnostics[0]
    return Check(name, "missing", diagnostic.message, diagnostic.hint)
