"""Spec §8.1, §8.5, §8.9: packing a folder, laying a package out as an install finds it, and checking one."""

import json
import shutil
import sys
from pathlib import Path

import pytest

from harnessgateway import packaging
from harnessgateway.cli import main
from harnessgateway.domain.archive import read_archive
from harnessgateway.errors import Invalid
from harnessgateway.kit import hgkit

SHELL = Path(__file__).parents[2] / "packages" / "shell"


@pytest.fixture
def package(tmp_path: Path) -> Path:
    """A copy of the shell package that a test may change."""
    folder = tmp_path / "package"
    shutil.copytree(SHELL, folder)
    return folder


def test_a_folder_packs_to_the_same_bytes_every_time(package: Path) -> None:
    first = packaging.pack(package)
    (package / "run").touch()                                            # a new modification time
    assert packaging.pack(package) == first
    read = read_archive(first)
    assert read.manifest.name == "shell"
    assert [f.path for f in read.files if "/" not in f.path] == ["README.md", "harness.json", "install", "run"]


def test_what_is_not_part_of_a_package_is_left_out(package: Path) -> None:
    before = read_archive(packaging.pack(package)).digest
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "run.pyc").write_bytes(b"x")
    (package / ".git").mkdir()
    (package / ".git" / "HEAD").write_text("ref")
    (package / ".DS_Store").write_bytes(b"x")
    assert read_archive(packaging.pack(package)).digest == before


def test_a_link_in_the_folder_is_refused(package: Path) -> None:
    (package / "link").symlink_to("run")
    with pytest.raises(Invalid, match="link"):
        packaging.pack(package)


def test_a_package_is_laid_out_with_the_kit_beside_it(package: Path, tmp_path: Path) -> None:
    target = tmp_path / "harness"
    target.mkdir()
    packaging.lay_out(read_archive(packaging.pack(package)), target)
    assert (target / "harness.json").read_text() == (package / "harness.json").read_text()
    assert (target / "run").stat().st_mode & 0o777 == 0o755
    assert (target / "harness.json").stat().st_mode & 0o777 == 0o644
    assert (target / "tests" / "echo" / "request.json").exists()
    assert (target / "kit" / "hgkit.py").read_bytes() == Path(hgkit.__file__).read_bytes()


def test_the_shell_package_passes_its_own_check() -> None:
    results = packaging.check(SHELL)
    assert [case.name for case in results] == ["echo", "fails"]
    echo, fails = results
    assert echo.terminal.kind == "done"
    assert echo.output[0]["content"][0]["text"] == "hello\nto-stderr\nno newline\n"
    assert (fails.terminal.kind, fails.terminal.code) == ("failed", "command_failed")
    assert fails.output[0]["content"][0]["text"] == "before\n"


def test_the_check_holds_a_case_to_its_expected_lines(package: Path) -> None:
    expected = package / "tests" / "echo" / "expected.jsonl"
    assert expected.exists()
    lines = expected.read_text().splitlines()
    changed = json.loads(lines[-1])
    changed["type"] = "hg.incomplete"
    changed["reason"] = "content_filter"
    expected.write_text("\n".join([*lines[:-1], json.dumps(changed)]) + "\n")
    with pytest.raises(Invalid, match=r"tests/echo: line \d+ is not the expected one"):
        packaging.check(package)


@pytest.mark.parametrize(("run", "message"), [
    ("#!/bin/sh\necho plain text\n", "tests/echo: line 1: not JSON"),
    ("#!/bin/sh\necho '{\"type\": \"hg.log\", \"text\": \"x\"}'\n", "tests/echo: run printed no terminal line"),
    ("#!/bin/sh\necho oops >&2; exit 7\n", "tests/echo: run exited with code 7: oops"),
    ("#!/bin/sh\necho '{\"type\": \"response.created\"}'\n", "tests/echo: line 1: response.created is written by"),
])
def test_a_run_that_breaks_the_contract_fails_the_check(package: Path, run: str, message: str) -> None:
    (package / "run").write_text(run)
    (package / "tests" / "echo" / "expected.jsonl").unlink()
    with pytest.raises(Invalid, match=message):
        packaging.check(package)


def test_a_case_may_bring_a_handover_and_history(package: Path) -> None:
    case = package / "tests" / "handed-over"
    case.mkdir()
    (case / "request.json").write_text(json.dumps({"model": "shell", "input": (
        'echo "$HG_HANDOVER"; cat "$HG_TURN/history.json"; echo; echo "[$HG_PROVIDER_API]"')}))
    (case / "handover.txt").write_text("Handover from pi. Review /mind/.pi.\n")
    (case / "history.json").write_text('[{"id": "resp_1"}]')
    result = next(case for case in packaging.check(package) if case.name == "handed-over")
    assert result.output[0]["content"][0]["text"] == 'Handover from pi. Review /mind/.pi.\n[{"id": "resp_1"}]\n[]\n'


def test_a_case_with_a_recording_replays_it_in_place_of_the_cli(package: Path) -> None:
    case = package / "tests" / "replayed"
    case.mkdir()
    (case / "request.json").write_text(json.dumps({"model": "shell", "input": "exit 9"}))
    (case / "recording").write_text("a recorded line\nanother\n")
    result = next(case for case in packaging.check(package) if case.name == "replayed")
    assert result.terminal.kind == "done"
    assert result.output[0]["content"][0]["text"] == "a recorded line\nanother\n"


def test_recording_writes_what_each_case_printed(package: Path) -> None:
    expected = package / "tests" / "echo" / "expected.jsonl"
    expected.write_text('{"type": "hg.done"}\n')
    with pytest.raises(Invalid, match="expected"):
        packaging.check(package)
    packaging.check(package, record=True)
    assert len(expected.read_text().splitlines()) > 5
    assert packaging.check(package)


def test_a_folder_that_is_not_a_package_fails_the_check(tmp_path: Path) -> None:
    with pytest.raises(Invalid, match=r"harness\.json"):
        packaging.check(tmp_path)


def test_the_command_line_checks_a_package(package: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["package", "check", str(package)]) == 0
    out = capsys.readouterr().out
    assert "shell 1.0.0" in out
    assert "tests/echo: passed" in out

    (package / "run").write_text("#!/bin/sh\necho plain text\n")
    assert main(["package", "check", str(package)]) == 1
    assert "not JSON" in capsys.readouterr().err


def test_the_command_line_tells_its_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "4.0.0"
    assert sys.argv                                                        # main never reads the real arguments
