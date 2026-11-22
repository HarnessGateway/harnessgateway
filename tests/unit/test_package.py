"""A harness package as an archive."""

import gzip
import io
import json
import tarfile

import pytest

from harnessgateway.domain.package import PackageFile, read_archive, write_archive
from harnessgateway.errors import Invalid
from tests.helpers import MANIFEST, package


def tar(*members: tuple[str, bytes, int], kind: bytes = tarfile.REGTYPE) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        for name, content, mode in members:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.type = (len(content), mode, kind)
            archive.addfile(info, io.BytesIO(content))
    return out.getvalue()


GOOD = (("./harness.json", json.dumps(MANIFEST).encode(), 0o644), ("./install", b"#!/bin/sh\n", 0o755), ("./run", b"#!/bin/sh\n", 0o755))


def test_what_tar_makes_is_a_package_and_the_same_files_are_the_same_package():
    read = read_archive(tar(*GOOD))
    assert read.manifest.name == "echo" and [file.path for file in read.files] == ["harness.json", "install", "run"]
    assert read.digest == read_archive(tar(*reversed(GOOD))).digest == read_archive(package()).digest
    assert read.digest != read_archive(package(**{"README.md": b"x"})).digest
    assert write_archive(read.files) == write_archive(reversed(read.files))            # the same bytes, whenever
    assert read_archive(write_archive([*read.files, PackageFile("lib/a.py", b"x")])).files[2].path == "lib/a.py"


@pytest.mark.parametrize(("archive", "why"), [
    (b"not an archive", "gzip-compressed tar"),
    (gzip.compress(b"not a tar"), "gzip-compressed tar"),
    (tar(*GOOD[1:]), "no harness.json"),
    (tar(GOOD[0], GOOD[1]), "no executable run"),
    (tar(GOOD[0], GOOD[1], ("./run", b"x", 0o644)), "no executable run"),
    (tar(("./harness.json", b"{", 0o644), *GOOD[1:]), "not JSON"),
    (tar(("./harness.json", json.dumps({**MANIFEST, "contract": 2}).encode(), 0o644), *GOOD[1:]), "harness.json: contract"),
    (tar(*GOOD, ("../outside", b"x", 0o644)), "not inside it"),
    (tar(*GOOD, ("/etc/passwd", b"x", 0o644)), "not inside it"),
    (tar(*GOOD, ("link", b"", 0o644), kind=tarfile.SYMTYPE), "plain files"),
])
def test_an_archive_that_is_not_a_whole_safe_package_is_refused(archive: bytes, why: str):
    with pytest.raises(Invalid, match=why):
        read_archive(archive)


def test_what_a_working_folder_or_a_macs_tar_adds_is_no_part_of_a_package():
    stray = (("._.", b"x", 0o644), ("./._run", b"x", 0o755), ("./.DS_Store", b"x", 0o644), ("./lib/._a.py", b"x", 0o644),
             ("./__pycache__/run.pyc", b"x", 0o644), ("./.git/config", b"x", 0o644))
    carried = read_archive(tar(*GOOD, ("./lib/a.py", b"a", 0o644), *stray))
    assert [file.path for file in carried.files] == ["harness.json", "install", "lib/a.py", "run"]
    assert carried.digest == read_archive(tar(*GOOD, ("./lib/a.py", b"a", 0o644))).digest       # the same package either way
