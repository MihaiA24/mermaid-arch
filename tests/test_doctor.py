from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from mermaid_arch.d2 import find_d2
from mermaid_arch.doctor import REQUIRED, Check, run_doctor


@pytest.fixture
def no_chromium(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Chromium cannot launch, so doctor runs fast and the same on every machine."""
    monkeypatch.setenv("MERMAID_ARCH_CHROMIUM", str(tmp_path / "no-chromium"))


def _doctor() -> dict[str, Check]:
    checks, _ = run_doctor()
    return {check.name: check for check in checks}


@pytest.mark.usefixtures("no_chromium")
def test_doctor_names_a_configured_d2_as_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary, _ = find_d2()
    monkeypatch.setenv("MERMAID_ARCH_D2", str(binary))

    d2 = _doctor()["d2"]

    assert d2.status == "ok"
    assert d2.detail.endswith(f" at {binary} (env)")
    assert d2.hint is None


@pytest.mark.usefixtures("no_chromium")
def test_doctor_says_how_to_replace_the_bundled_d2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MERMAID_ARCH_D2", raising=False)
    directories = os.environ.get("PATH", "").split(os.pathsep)
    without_d2 = [d for d in directories if shutil.which("d2", path=d) is None]
    monkeypatch.setenv("PATH", os.pathsep.join(without_d2))

    d2 = _doctor()["d2"]

    assert d2.status == "ok"
    assert d2.detail.endswith(" (bundled)")
    assert d2.hint is not None
    assert "d2-python-wrapper" in d2.hint
    assert "MERMAID_ARCH_D2" in d2.hint


@pytest.mark.usefixtures("no_chromium")
def test_doctor_without_chromium_reports_fonts_as_undetermined() -> None:
    checks = _doctor()

    assert checks["chromium"].status == "missing"
    assert checks["fonts"].status == "missing"
    assert "could not be determined: Chromium did not launch" in checks["fonts"].detail


@pytest.mark.browser
def test_doctor_names_the_fonts_mermaid_text_is_laid_out_with(in_own_thread) -> None:
    fonts = in_own_thread(_doctor)["fonts"]

    assert fonts.status == "ok"
    families, _, _ = fonts.detail.partition(": ")
    assert families
    assert "fonts" not in REQUIRED
