from __future__ import annotations

from pathlib import Path

import pytest

from mermaid_arch.checks import svg_size
from mermaid_arch.d2 import d2_viewer_themes, find_d2, render_d2
from mermaid_arch.models import DiagramError, RenderOptions


def _write(tmp_path: Path, text: str, name: str = "diagram.d2") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _errors(source: Path, **kwargs: object) -> list[tuple[str, int | None]]:
    with pytest.raises(DiagramError) as caught:
        render_d2(source, **kwargs)  # type: ignore[arg-type]
    return [(d.code, d.line) for d in caught.value.diagnostics]


def test_compile_errors_report_each_location_with_its_line(tmp_path: Path) -> None:
    source = _write(tmp_path, "a -> b\nc: {\n  shape: oval\n}\nx ->\ny: {\n")

    assert _errors(source) == [("d2/compile", 5), ("d2/compile", 6)]


def test_compile_error_inside_an_imported_file_claims_no_line_in_the_source(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "p\nq ->\n", name="child.d2")
    source = _write(tmp_path, "a -> b\nx: @child\n")

    with pytest.raises(DiagramError) as caught:
        render_d2(source)

    [diagnostic] = caught.value.diagnostics
    assert diagnostic.code == "d2/compile"
    assert diagnostic.line is None
    assert "child.d2:2" in diagnostic.message


def test_explicit_pad_overrides_the_source_d2_config(tmp_path: Path) -> None:
    source = _write(tmp_path, "vars: {d2-config: {pad: 40}}\na -> b\n")

    declared_w, declared_h = svg_size(render_d2(source).svg)
    unpadded_w, unpadded_h = svg_size(render_d2(source, pad=0).svg)

    assert (declared_w - unpadded_w, declared_h - unpadded_h) == (80, 80)


@pytest.mark.parametrize(
    "kwargs",
    [{"theme": "dark"}, {"dark_theme": "mauve"}, {"theme": "9999"}],
)
def test_bad_usage_is_a_d2_usage_error(tmp_path: Path, kwargs: dict[str, str]) -> None:
    source = _write(tmp_path, "a -> b\n")

    assert {code for code, _ in _errors(source, **kwargs)} == {"d2/usage"}


def test_source_without_shapes_is_empty(tmp_path: Path) -> None:
    source = _write(tmp_path, "# only a comment\n")

    assert _errors(source) == [("svg/empty", None)]


def test_unusable_d2_override_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERMAID_ARCH_D2", str(tmp_path / "no-such-d2"))

    with pytest.raises(DiagramError) as caught:
        find_d2()

    assert [d.code for d in caught.value.diagnostics] == ["env/d2-missing"]


_THEMED = "vars: {d2-config: {theme-id: 4; dark-theme-id: 201; theme-overrides: {B1: red}}}\na\n"


@pytest.mark.parametrize(
    ("text", "options", "expected"),
    [
        (_THEMED, RenderOptions(theme="1", dark_theme="200"), ("1", "200")),
        (_THEMED, RenderOptions(), ("4", "201")),
        (_THEMED, RenderOptions(theme="5"), ("5", "201")),
        ("a -> b\n", RenderOptions(), ("0", "200")),
        ("vars: {d2-config: {theme-id: 3}}\na\n", RenderOptions(), ("3", "200")),
        ("vars: {d2-config: {dark-theme-id: -1}}\na\n", RenderOptions(theme="5"), ("5", "5")),
    ],
)
def test_viewer_themes_prefer_option_then_source_then_default(
    text: str, options: RenderOptions, expected: tuple[str, str]
) -> None:
    assert d2_viewer_themes(text, options) == expected
