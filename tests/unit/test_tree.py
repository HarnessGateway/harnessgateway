"""Spec §12.2, §12.6: walking the mind and the work, and measuring them."""

import os
import socket
from pathlib import Path

import pytest

from harnessgateway.steps.tree import measure, walk


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    roots = {"mind": tmp_path / "m", "work": tmp_path / "w"}
    for root in roots.values():
        root.mkdir()
    return roots


def test_every_path_is_found_once_parents_first_in_name_order(roots: dict[str, Path]) -> None:
    (roots["work"] / "src").mkdir()
    (roots["work"] / "src" / "b.py").write_bytes(b"12345")
    (roots["work"] / "src" / "a.py").write_bytes(b"")
    (roots["work"] / "README").write_bytes(b"hi")
    (roots["mind"] / ".claude").mkdir()
    found = list(walk(roots))
    assert [(f.path, f.kind, f.size) for f in found] == [
        ("mind/.claude", "d", 0),
        ("work/README", "f", 2),
        ("work/src", "d", 0),
        ("work/src/a.py", "f", 0),
        ("work/src/b.py", "f", 5),
    ]
    assert found[1].location == roots["work"] / "README"


def test_modes_and_times_are_reported(roots: dict[str, Path]) -> None:
    script = roots["work"] / "run.sh"
    script.write_bytes(b"#!/bin/sh\n")
    script.chmod(0o750)
    os.utime(script, ns=(1_700_000_000_123_456_789, 1_700_000_000_123_456_789))
    [found] = walk(roots)
    assert (found.mode, found.mtime) == (0o750, 1_700_000_000_123_456_789)


def test_a_link_is_reported_and_never_followed(roots: dict[str, Path], tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"x" * 100)
    (roots["work"] / "out").symlink_to(outside)
    (roots["work"] / "dangling").symlink_to("nowhere")
    assert [(f.path, f.kind) for f in walk(roots)] == [("work/dangling", "l"), ("work/out", "l")]
    assert measure(roots) == 0


def test_sockets_and_pipes_are_reported_as_other(roots: dict[str, Path]) -> None:
    os.mkfifo(roots["work"] / "pipe")
    with socket.socket(socket.AF_UNIX) as s:
        s.bind(str(roots["work"] / "sock"))
        assert [(f.path, f.kind) for f in walk(roots)] == [("work/pipe", "other"), ("work/sock", "other")]


def test_a_very_deep_tree_is_walked(roots: dict[str, Path]) -> None:
    folder = str(roots["work"])
    for _ in range(1200):
        folder += "/d"
        os.mkdir(folder)
    assert sum(1 for _ in walk(roots)) == 1200


def test_the_measure_is_the_sum_of_file_sizes(roots: dict[str, Path]) -> None:
    (roots["work"] / "a").write_bytes(b"x" * 1000)
    (roots["mind"] / "d").mkdir()
    (roots["mind"] / "d" / "b").write_bytes(b"x" * 500)
    assert measure(roots) == 1500


def test_a_root_that_is_not_there_holds_nothing(tmp_path: Path) -> None:
    assert list(walk({"work": tmp_path / "absent"})) == []
