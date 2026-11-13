from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from mermaid_arch.cli import main


def test_render_receipt_and_last_good_file(tmp_path: Path, capsys) -> None:
    source = tmp_path / "diagram.d2"
    source.write_text("a -> b")
    assert main(["render", str(source), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    [output] = report["results"][0]["outputs"]
    target = source.with_suffix(".svg")
    before = target.read_bytes()
    assert output["sha256"] == hashlib.sha256(before).hexdigest()
    assert output["bytes"] == len(before)
    source.write_text("a ->\n")
    assert main(["render", str(source), "--json"]) == 1
    failure = json.loads(capsys.readouterr().out)
    assert not failure["ok"]
    assert failure["results"][0]["outputs"] == []
    assert failure["results"][0]["diagnostics"][0]["code"] == "d2/compile"
    assert target.read_bytes() == before


def test_batch_continues_after_missing_source_and_out_dir(tmp_path: Path, capsys) -> None:
    source = tmp_path / "good.d2"
    source.write_text("a -> b")
    directory = tmp_path / "out"
    assert (
        main(
            [
                "render",
                str(tmp_path / "missing.d2"),
                str(source),
                "--out-dir",
                str(directory),
                "--json",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert [r["ok"] for r in report["results"]] == [False, True]
    assert report["results"][0]["diagnostics"][0]["code"] == "source/missing"
    assert ET.parse(directory / "good.svg").getroot().tag == "{http://www.w3.org/2000/svg}svg"


def test_output_inference_and_check_writes_nothing(tmp_path: Path, capsys) -> None:
    source = tmp_path / "diagram.d2"
    source.write_text("a -> b")
    assert main(["check", str(source), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["outputs"] == []
    assert not source.with_suffix(".svg").exists()
    destination = tmp_path / "new" / "custom.svg"
    assert main(["render", str(source), "-o", str(destination)]) == 0
    assert ET.parse(destination).getroot().tag == "{http://www.w3.org/2000/svg}svg"


@pytest.mark.parametrize(
    "extra",
    [
        ["-f", "svg", "-o", "x.png"],
        ["--scale", "nan"],
        ["--scale", "0"],
        ["--pad", "-1"],
        ["--background", "white;display:none"],
        ["--config", "missing.json"],
        ["--icon-pack", "fas"],
        ["--icon-pack", "fas=mdi@1"],
        ["-"],
        ["-", "-", "--engine", "d2"],
        ["-", "--engine", "d2"],
        ["--watch", "--json"],
        ["--watch", "--verify"],
        ["-", "--watch", "--engine", "d2", "-o", "x.svg"],
    ],
)
def test_usage_errors_are_exit_two(extra: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["render", "a.d2", *extra])
    assert caught.value.code == 2


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_outputs_take_their_mode_from_the_umask(tmp_path: Path) -> None:
    source = tmp_path / "diagram.d2"
    source.write_text("a -> b")
    previous = os.umask(0o022)
    try:
        assert main(["render", str(source)]) == 0
    finally:
        os.umask(previous)
    assert stat.S_IMODE(source.with_suffix(".svg").stat().st_mode) == 0o644


def test_colliding_output_paths_are_rejected() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["render", "a.d2", "a.mmd"])
    assert caught.value.code == 2


def test_unknown_source_suffix_is_diagnostic(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source.txt"
    source.write_text("a -> b")
    assert main(["check", str(source), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["results"][0]["diagnostics"][0]["code"] == "source/unknown-type"


def test_stdin_renders_to_the_named_output(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("a -> b\n"))
    target = tmp_path / "out.svg"
    assert main(["render", "-", "--engine", "d2", "-o", str(target), "--json"]) == 0
    [result] = json.loads(capsys.readouterr().out)["results"]
    assert result["source"] == "-"
    assert result["outputs"][0]["path"] == str(target)
    assert ET.parse(target).getroot().tag == "{http://www.w3.org/2000/svg}svg"


def test_markdown_renders_each_diagram_block(tmp_path: Path, capsys) -> None:
    doc = tmp_path / "notes.md"
    doc.write_text(
        "# Notes\n"
        "\n"
        "```d2\n"
        "a -> b\n"
        "```\n"
        "\n"
        "````markdown\n"
        "```d2\n"
        "not -> a diagram\n"
        "```\n"
        "````\n"
        "~~~ D2 title\n"
        "c -> d\n"
        "~~~\n"
    )
    assert main(["render", str(doc), "--json"]) == 0
    results = json.loads(capsys.readouterr().out)["results"]
    assert [(r["block"], r["outputs"][0]["path"]) for r in results] == [
        (1, str(tmp_path / "notes-1.svg")),
        (2, str(tmp_path / "notes-2.svg")),
    ]
    assert ET.parse(tmp_path / "notes-2.svg").getroot().tag == "{http://www.w3.org/2000/svg}svg"

    doc.write_text(doc.read_text().replace("c -> d", "c ->"))
    assert main(["check", str(doc), "--json"]) == 1
    second = json.loads(capsys.readouterr().out)["results"][1]
    assert (second["source"], second["block"]) == (str(doc), 2)
    assert [(d["code"], d["line"]) for d in second["diagnostics"]] == [("d2/compile", 13)]

    with pytest.raises(SystemExit) as caught:
        main(["render", str(doc), "-o", str(tmp_path / "one.svg")])
    assert caught.value.code == 2


def test_markdown_without_diagram_blocks_fails(tmp_path: Path, capsys) -> None:
    doc = tmp_path / "prose.md"
    doc.write_text("# Prose\n\n```python\nprint('no diagram')\n```\n")
    assert main(["check", str(doc), "--json"]) == 1
    [result] = json.loads(capsys.readouterr().out)["results"]
    assert [d["code"] for d in result["diagnostics"]] == ["source/no-diagrams"]


def test_verify_compares_outputs_without_writing(tmp_path: Path, capsys) -> None:
    source = tmp_path / "diagram.d2"
    source.write_text("a -> b")
    target = tmp_path / "diagram.svg"
    assert main(["render", str(source)]) == 0
    assert main(["render", str(source), "--verify"]) == 0
    assert f"{source} == {target} (up to date)" in capsys.readouterr().out

    before = target.read_bytes()
    source.write_text("a -> c")
    assert main(["render", str(source), "--verify", "--json"]) == 1
    [stale] = json.loads(capsys.readouterr().out)["results"]
    assert [d["code"] for d in stale["diagnostics"]] == ["output/stale"]
    assert target.read_bytes() == before

    target.unlink()
    assert main(["render", str(source), "--verify", "--json"]) == 1
    [missing] = json.loads(capsys.readouterr().out)["results"]
    assert [d["code"] for d in missing["diagnostics"]] == ["output/missing"]
    assert not target.exists()


def test_target_and_sketch_reach_d2(tmp_path: Path, capsys) -> None:
    source = tmp_path / "layered.d2"
    source.write_text("a -> b\nlayers: {\n  detail: {\n    c -> d\n  }\n}\n")
    assert main(["check", str(source), "--json"]) == 1
    [diagnostic] = json.loads(capsys.readouterr().out)["results"][0]["diagnostics"]
    assert "--target" in diagnostic["hint"]

    target = tmp_path / "detail.svg"
    argv = ["render", str(source), "--target", "layers.detail", "--sketch", "-o", str(target)]
    assert main(argv) == 0
    svg = target.read_text()
    assert ">c<" in svg and ">a<" not in svg
    assert 'id="streaks-' in svg  # d2 draws its sketch texture with these patterns


def test_watch_rerenders_a_changed_source_until_interrupted(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    source = tmp_path / "diagram.d2"
    source.write_text("a -> b")
    naps: list[float] = []

    def nap(seconds: float) -> None:
        naps.append(seconds)
        if len(naps) > 1:
            raise KeyboardInterrupt
        source.write_text("a -> changed")

    monkeypatch.setattr("mermaid_arch.cli.sleep", nap)
    assert main(["render", str(source), "--watch"]) == 0
    assert capsys.readouterr().out.count(f"{source} -> ") == 2
    assert ">changed<" in source.with_suffix(".svg").read_text()
