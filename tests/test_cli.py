from __future__ import annotations

import hashlib
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
